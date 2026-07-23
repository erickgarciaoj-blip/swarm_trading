#!/usr/bin/env bash
#
# Deploy real de una release ya extraída (por ci-deploy-entrypoint.sh, o
# copiada a mano para el primer deploy manual — ver runbook, paso 18).
#
# Dos llamadores legítimos con confianza distinta: el entrypoint (ya validó
# bundle/imagen/label) y un admin vía `sudo -u swarm-deploy deploy.sh <sha>`
# (sin haber pasado por el entrypoint). Por eso este script vuelve a validar
# el SHA de forma independiente — nunca asume que ya se validó nada.
#
# Uso: deploy.sh <sha completo, 40 hex> [actor]
set -euo pipefail

COMPONENT="deploy"

# --- SWARM_ROOT: mismo contrato en los tres scripts de esta fase (ver
# ci-deploy-entrypoint.sh para la razón de la duplicación deliberada). En
# producción nunca debe configurarse — el default fijo es el único valor real.
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
# Nombre de proyecto de Compose fijo y compartido entre TODAS las releases —
# es lo que hace que `docker compose up` de una release nueva reemplace los
# contenedores de la release anterior (mismo proyecto, mismo nombre de
# servicio) en vez de levantar un segundo stack en paralelo. Sin esto, cada
# release (en su propio directorio) sería un proyecto Compose distinto.
readonly COMPOSE_PROJECT_NAME="swarm_trading_staging"
readonly HEALTH_CHECK_ATTEMPTS=20
readonly HEALTH_CHECK_INTERVAL_SEC=3
readonly HEALTH_URL="http://127.0.0.1:8000/health/ready"
readonly BACKUP_RETENTION_COUNT=14

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

# --- Argumentos --------------------------------------------------------
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
[ -d "$RELEASE_DIR" ] || die "no existe $RELEASE_DIR — nada que desplegar"
# docker-compose.yml viaja en cada release por trazabilidad/referencia
# (mismo archivo que dev usaría en ese commit), pero Compose se invoca
# únicamente con docker-compose.staging.yml — autocontenido, sin merge,
# sin ambigüedad de build:/image: entre versiones de Compose (ver ADR-0011).
[ -f "$RELEASE_DIR/docker-compose.yml" ] || die "falta docker-compose.yml en $RELEASE_DIR"
[ -f "$RELEASE_DIR/docker-compose.staging.yml" ] || die "falta docker-compose.staging.yml en $RELEASE_DIR"
COMPOSE_FILES=(-f "$RELEASE_DIR/docker-compose.staging.yml")

# --- flock no bloqueante -------------------------------------------------
# GitHub Actions ya encola vía `concurrency`; esto es defensa adicional
# contra una ejecución manual que coincida con una automática. Falla
# inmediato con mensaje claro, no espera indefinidamente.
LOCK_FILE="$SWARM_ROOT/.deploy.lock"
mkdir -p "$SWARM_ROOT" 2>/dev/null || true
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "REJECTED: otro deploy o rollback en curso, lock no adquirido"
    echo "$COMPONENT: otro deploy o rollback está en curso — abortando" >&2
    exit 1
fi
log "lock adquirido, iniciando deploy de $SHA"

# --- Resolver referencia de imagen por digest, no por tag mutable -------
# Si el entrypoint ya corrió, .image-digest ya existe y trae el digest que
# ese entrypoint verificó (tag + label OCI) — se usa tal cual, sin volver a
# resolver por tag, para no reabrir la ventana entre "lo que se verificó" y
# "lo que se despliega" (ver ADR-0011, caso de digest distinto entre
# inspección y deploy). Si no existe (invocación manual/directa sin pasar
# por el entrypoint), se resuelve aquí mismo.
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
log "imagen a desplegar: $DEPLOY_IMAGE_REF"

# --- PREVIOUS_SHA: capturado ANTES de tocar nada ------------------------
PREVIOUS_SHA=""
if [ -L "$SWARM_ROOT/current" ]; then
    PREVIOUS_SHA="$(basename "$(readlink -f "$SWARM_ROOT/current" 2>/dev/null || true)" 2>/dev/null || true)"
fi
log "release actual antes de este deploy: ${PREVIOUS_SHA:-<ninguna>}"

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

# --- Rollback automático: lógica INLINE en el deploy.sh que se acaba de
# validar en esta misma ejecución (frontera estable y conocida) — nunca
# invoca el rollback.sh de otra release. Limitado a volver a levantar el
# compose y la imagen de la release anterior, nada más.
attempt_rollback() {
    local target_sha="$1"
    local reason="$2"

    log "iniciando rollback automático — motivo: $reason"

    if [ -z "$target_sha" ]; then
        log "sin release anterior — deteniendo el servicio nuevo, requiere intervención manual"
        docker compose -p "$COMPOSE_PROJECT_NAME" "${COMPOSE_FILES[@]}" stop swarm 2>/dev/null || true
        die "deploy de $SHA falló ($reason) y no hay release anterior al cual volver (primer deploy)"
    fi

    local prev_dir="$SWARM_ROOT/releases/$target_sha"
    [ -d "$prev_dir" ] || die "deploy de $SHA falló ($reason); release anterior $target_sha no existe en disco — rollback automático imposible"

    local prev_files=(-f "$prev_dir/docker-compose.staging.yml")
    local prev_digest_file="$prev_dir/.image-digest"
    local prev_image_ref
    if [ -f "$prev_digest_file" ]; then
        prev_image_ref="$(cat "$prev_digest_file")"
    else
        prev_image_ref="${IMAGE_REPO}:sha-${target_sha}"
    fi

    if ! DEPLOY_IMAGE_REF="$prev_image_ref" docker compose -p "$COMPOSE_PROJECT_NAME" "${prev_files[@]}" up -d --wait; then
        die "deploy de $SHA falló ($reason); rollback automático a $target_sha también falló al levantar el stack — intervención manual urgente"
    fi

    if wait_for_ready; then
        ln -sfn "$prev_dir" "$SWARM_ROOT/current"
        log "rollback automático completado — current vuelve a apuntar a $target_sha"
        die "deploy de $SHA falló ($reason) — rollback automático a $target_sha exitoso, servicio restaurado"
    fi

    die "deploy de $SHA falló ($reason); rollback automático a $target_sha también falló el health check — intervención manual urgente"
}

# --- Backup: se basa en si postgres está REALMENTE corriendo, nunca
# únicamente en si existe `current` — un postgres con datos puede estar
# arriba aunque `current` todavía no exista (ver ADR-0011 / casos de
# prueba). Si el backup falla su propia validación de integridad, la
# migración NUNCA se invoca.
POSTGRES_CID="$(docker ps --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" \
    --filter "label=com.docker.compose.service=postgres" --filter "status=running" \
    --quiet | head -1)"
if [ -n "$POSTGRES_CID" ]; then
    BACKUP_DIR="$SWARM_ROOT/backups"
    mkdir -p "$BACKUP_DIR"
    BACKUP_FILE="$BACKUP_DIR/pre-deploy_${SHA}_$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
    if ! docker exec "$POSTGRES_CID" sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
            | gzip > "$BACKUP_FILE"; then
        rm -f "$BACKUP_FILE"
        die "backup falló — deploy abortado antes de la migración, servicio activo intacto"
    fi
    if [ ! -s "$BACKUP_FILE" ]; then
        rm -f "$BACKUP_FILE"
        die "backup vacío — deploy abortado antes de la migración, servicio activo intacto"
    fi
    if ! gzip -t "$BACKUP_FILE"; then
        rm -f "$BACKUP_FILE"
        die "backup falló verificación de integridad gzip — deploy abortado, servicio activo intacto"
    fi
    log "backup OK: $BACKUP_FILE"
else
    log "sin contenedor postgres corriendo bajo el proyecto $COMPOSE_PROJECT_NAME — se omite backup (arranque inicial)"
fi

# --- Migración con la imagen nueva --------------------------------------
if ! docker compose -p "$COMPOSE_PROJECT_NAME" "${COMPOSE_FILES[@]}" run --rm migrate; then
    die "migración falló — abortando, el servicio activo no fue tocado"
fi
log "migración OK"

# --- Cutover: current NO se mueve todavía -------------------------------
if ! docker compose -p "$COMPOSE_PROJECT_NAME" "${COMPOSE_FILES[@]}" up -d --wait; then
    attempt_rollback "$PREVIOUS_SHA" "docker compose up falló"
fi

if ! wait_for_ready; then
    attempt_rollback "$PREVIOUS_SHA" "health check falló tras el deploy"
fi

# --- Solo ahora, con el nuevo stack confirmado saludable ----------------
ln -sfn "$RELEASE_DIR" "$SWARM_ROOT/current"
log "deploy de $SHA exitoso — current actualizado (previo: ${PREVIOUS_SHA:-<ninguna>})"

# --- Poda de backups: solo tras un ciclo completo exitoso ---------------
BACKUP_DIR="$SWARM_ROOT/backups"
if [ -d "$BACKUP_DIR" ]; then
    # shellcheck disable=SC2012 # nombres generados por este mismo script, formato fijo — ls -t es seguro aquí
    ls -1t "$BACKUP_DIR"/*.sql.gz 2>/dev/null | tail -n +$((BACKUP_RETENTION_COUNT + 1)) | while IFS= read -r old; do
        rm -f -- "$old"
        log "backup podado: $old"
    done
fi

log "deploy completo: sha=$SHA actor=$ACTOR resultado=OK"
