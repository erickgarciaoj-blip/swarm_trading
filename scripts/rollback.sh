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

if [ $# -lt 1 ] || [ $# -gt 2 ]; then
    echo "uso: $0 <sha completo, 40 hex> [actor]" >&2
    exit 1
fi
SHA="$1"
if [[ ! "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
    echo "$COMPONENT: SHA inválido: '$SHA'" >&2
    exit 1
fi
ACTOR="${2:-${SUDO_USER:-$(id -un)}}"
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
    TAG_REF="${IMAGE_REPO}:sha-${SHA}"
    docker pull "$TAG_REF" || die "no se pudo obtener $TAG_REF"
    DEPLOY_IMAGE_REF="$(docker inspect --format '{{index .RepoDigests 0}}' "$TAG_REF" 2>/dev/null || true)"
    [ -n "$DEPLOY_IMAGE_REF" ] || die "no se pudo resolver el digest de $TAG_REF"
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
    die "rollback a $SHA falló el health check tras levantar el stack — intervención manual urgente, no se reintenta automáticamente"
fi

ln -sfn "$RELEASE_DIR" "$SWARM_ROOT/current"
log "rollback manual completado — current apunta ahora a $SHA (actor=$ACTOR)"
