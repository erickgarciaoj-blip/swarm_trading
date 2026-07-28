#!/usr/bin/env bash
#
# Rollback manual explícito a una release ya presente en disco. El SHA
# objetivo siempre se recibe como argumento — este script nunca infiere
# "el anterior" por su cuenta, para que quede claro en el log a qué versión
# exacta se volvió y quién lo pidió.
#
# No reconstruye ni re-descarga nada: si releases/<sha>/ no existe en disco,
# falla — el rollback debe ser rápido y no depender de que GHCR esté
# disponible en ese momento. Nunca toca backup ni migraciones (ADR-0011:
# el rollback nunca revierte esquema, solo código/imagen).
#
# Uso exclusivamente manual — el camino automático (fallo de health check
# tras un deploy) NO invoca este script; usa lógica inline en deploy.sh
# sobre el propio código recién validado, no el rollback.sh de otra release
# (ver ADR-0011 y la discusión de diseño del PR 2).
#
# Uso: rollback.sh <sha completo, 40 hex> [actor]
set -euo pipefail

COMPONENT="rollback"

# --- SWARM_ROOT: mismo contrato en los tres scripts de esta fase.
SWARM_ROOT="${SWARM_ROOT-/opt/swarm-trading}"
case "$SWARM_ROOT" in
    "") echo "$COMPONENT: SWARM_ROOT vacío no permitido" >&2; exit 1 ;;
    /) echo "$COMPONENT: SWARM_ROOT '/' no permitido" >&2; exit 1 ;;
    /*) : ;;
    *) echo "$COMPONENT: SWARM_ROOT debe ser una ruta absoluta, recibido '$SWARM_ROOT'" >&2; exit 1 ;;
esac
case "$SWARM_ROOT" in
    *..*) echo "$COMPONENT: SWARM_ROOT no puede contener '..', recibido '$SWARM_ROOT'" >&2; exit 1 ;;
esac

readonly IMAGE_REPO="ghcr.io/erickgarciaoj-blip/swarm_trading"
readonly COMPOSE_PROJECT_NAME="swarm_trading_staging"
readonly HEALTH_CHECK_ATTEMPTS=20
readonly HEALTH_CHECK_INTERVAL_SEC=3
readonly HEALTH_URL="http://127.0.0.1:8000/health/ready"
# Servicios objetivo de "up -d --wait" — deliberadamente sin "migrate": el
# rollback nunca corre migraciones (ver cabecera del archivo), pero
# "migrate" sigue declarado en el compose sin política de reinicio; un "up
# --wait" sin filtro lo incluiría igual y Compose reportaría fallo pese a
# que todo termine bien, porque un contenedor de un solo uso ya no está
# "corriendo" cuando --wait lo revisa — bug conocido de Compose (ver
# https://github.com/docker/compose/issues/10596 y
# https://github.com/docker/compose/issues/13069).
CUTOVER_SERVICES=(postgres redis swarm)

LOG_FILE="$SWARM_ROOT/logs/deploy.log"

log() {
    mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
    printf '%s|actor=%s|sha=%s|component=%s|%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${ACTOR:-unknown}" "${SHA:-none}" "$COMPONENT" "$*" \
        >> "$LOG_FILE" 2>/dev/null || true
}

die() {
    log "ERROR: $*"
    echo "$COMPONENT: error — $*" >&2
    exit 1
}

# Quita bytes de control (incluye \n, \r) de un valor que no pasó por una
# regex propia (p. ej. LABEL_SHA, leído del label OCI de una imagen Docker —
# controlado por quien tenga push al registry) antes de interpolarlo en una
# línea de log — sin esto, un valor con saltos de línea podría forjar
# entradas falsas en deploy.log (auditoría del PR 2, hallazgo L1).
sanitize_log_value() {
    printf '%s' "$1" | tr -d '\000-\037\177'
}

if [ $# -lt 1 ] || [ $# -gt 2 ]; then
    echo "uso: $0 <sha completo, 40 hex> [actor]" >&2
    exit 1
fi
SHA="$1"
if [[ ! "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
    echo "$COMPONENT: SHA inválido: '$SHA'" >&2
    exit 1
fi
ACTOR="$(sanitize_log_value "${2:-${SUDO_USER:-$(id -un)}}")"
if [[ ! "$ACTOR" =~ ^[A-Za-z0-9_.-]{1,100}$ ]]; then
    ACTOR="unknown"
fi

RELEASE_DIR="$SWARM_ROOT/releases/$SHA"
if [ ! -d "$RELEASE_DIR" ]; then
    die "no existe un release extraído para $SHA — rollback no reconstruye releases, solo cambia a uno ya presente"
fi
# docker-compose.yml viaja por trazabilidad; Compose se invoca solo con
# docker-compose.staging.yml (autocontenido, ver ADR-0011).
[ -f "$RELEASE_DIR/docker-compose.yml" ] || die "falta docker-compose.yml en $RELEASE_DIR"
[ -f "$RELEASE_DIR/docker-compose.staging.yml" ] || die "falta docker-compose.staging.yml en $RELEASE_DIR"
COMPOSE_FILES=(-f "$RELEASE_DIR/docker-compose.staging.yml")

# --- flock: mismo lockfile que deploy.sh — deploy y rollback nunca corren
# concurrentemente entre sí tampoco.
LOCK_FILE="$SWARM_ROOT/.deploy.lock"
mkdir -p "$SWARM_ROOT" 2>/dev/null || true
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "REJECTED: otro deploy o rollback en curso, lock no adquirido"
    echo "$COMPONENT: otro deploy o rollback está en curso — abortando" >&2
    exit 1
fi
log "lock adquirido, iniciando rollback manual a $SHA"

DIGEST_FILE="$RELEASE_DIR/.image-digest"
if [ -f "$DIGEST_FILE" ]; then
    DEPLOY_IMAGE_REF="$(cat "$DIGEST_FILE")"
else
    # Invocación directa (sin pasar por ci-deploy-entrypoint.sh) — nadie
    # verificó todavía el label OCI ni resolvió un digest real para este SHA.
    # Misma verificación que el entrypoint hace en su paso 5 (ver ADR-0011),
    # repetida aquí ANTES de tocar el stack: sin esto, un tag `sha-<sha>` mal
    # etiquetado en el registry se desplegaría sin que nada lo note
    # (auditoría del PR 2, hallazgo H1).
    TAG_REF="${IMAGE_REPO}:sha-${SHA}"
    docker pull "$TAG_REF" || die "no se pudo obtener $TAG_REF"

    LABEL_SHA="$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$TAG_REF" 2>/dev/null || true)"
    if [ "$LABEL_SHA" != "$SHA" ]; then
        die "label OCI org.opencontainers.image.revision ('$(sanitize_log_value "$LABEL_SHA")') no coincide con el SHA solicitado ($SHA) — $TAG_REF rechazada antes de tocar el stack"
    fi

    # Filtra por $IMAGE_REPO en vez de tomar el índice 0 de RepoDigests a
    # ciegas — una imagen local puede acumular más de un RepoDigest si
    # alguna vez se etiquetó/pulleó bajo otro registry o repo distinto. Sin
    # fallback al primer RepoDigest disponible: si ninguno pertenece a
    # $IMAGE_REPO, se rechaza — el `|| true` solo evita que un grep sin
    # coincidencia mate el script en silencio bajo `pipefail` (sin él,
    # `DEPLOY_IMAGE_REF="$(pipeline)"` hereda el código de salida de la
    # pipeline completa bajo `set -e` y el script muere sin ningún mensaje).
    DEPLOY_IMAGE_REF="$(
        docker inspect --format '{{range .RepoDigests}}{{.}}{{"\n"}}{{end}}' "$TAG_REF" 2>/dev/null \
            | grep -F "${IMAGE_REPO}@" \
            | head -1 || true
    )"
    [ -n "$DEPLOY_IMAGE_REF" ] || die "no se encontró un RepoDigest para el repositorio esperado: $IMAGE_REPO"
    printf '%s\n' "$DEPLOY_IMAGE_REF" > "$DIGEST_FILE"
fi
export DEPLOY_IMAGE_REF
log "imagen objetivo del rollback: $DEPLOY_IMAGE_REF"

if ! docker compose -p "$COMPOSE_PROJECT_NAME" "${COMPOSE_FILES[@]}" up -d --wait "${CUTOVER_SERVICES[@]}"; then
    die "rollback a $SHA falló al levantar el stack — intervención manual requerida"
fi

wait_for_ready() {
    local attempt
    for attempt in $(seq 1 "$HEALTH_CHECK_ATTEMPTS"); do
        if curl -sf -o /dev/null "$HEALTH_URL"; then
            log "health check intento $attempt/$HEALTH_CHECK_ATTEMPTS: OK ($HEALTH_URL)"
            return 0
        fi
        log "health check intento $attempt/$HEALTH_CHECK_ATTEMPTS: no listo"
        sleep "$HEALTH_CHECK_INTERVAL_SEC"
    done
    log "health check agotó $HEALTH_CHECK_ATTEMPTS intentos sin éxito"
    return 1
}

if ! wait_for_ready; then
    # No recursa en otro rollback — evita loops. Queda para intervención
    # manual explícita.
    #
    # "current" nunca se movió (ver más abajo, solo se actualiza tras
    # health OK) — pero el `up -d --wait` de arriba SÍ reemplazó los
    # contenedores en marcha por los de $SHA, así que en este punto
    # "current" y lo que realmente está corriendo YA NO COINCIDEN: current
    # sigue señalando la release previa, mientras postgres/redis/swarm ya
    # corren la imagen de $SHA (no saludable). No hay reversión automática
    # a partir de aquí — se deja constancia explícita del estado real para
    # que quien intervenga no confíe ciegamente en "current" (auditoría del
    # PR 2, hallazgo M5; ver también runbook, sección 20).
    CURRENT_SHA_AFTER=""
    if [ -L "$SWARM_ROOT/current" ]; then
        CURRENT_SHA_AFTER="$(basename "$(readlink -f "$SWARM_ROOT/current" 2>/dev/null || true)" 2>/dev/null || true)"
    fi
    SWARM_CID_AFTER="$(docker compose -p "$COMPOSE_PROJECT_NAME" "${COMPOSE_FILES[@]}" ps -q swarm 2>/dev/null || true)"
    log "ADVERTENCIA: current puede NO representar lo que está corriendo — objetivo del rollback=$SHA, current sigue apuntando a=${CURRENT_SHA_AFTER:-<ninguna>}, contenedor 'swarm' en ejecución=${SWARM_CID_AFTER:-<ninguno>} (probablemente ya la imagen de $SHA, aunque no saludable)"
    die "rollback a $SHA falló el health check tras levantar el stack — intervención manual urgente, no se reintenta automáticamente"
fi

ln -sfn "$RELEASE_DIR" "$SWARM_ROOT/current"
log "rollback manual completado — current apunta ahora a $SHA (actor=$ACTOR)"
