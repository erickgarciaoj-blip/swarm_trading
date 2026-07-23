#!/usr/bin/env bash
# Helpers compartidos por el job "deploy-scripts-docker-integration" de
# ci.yml (Fase 4.6, PR 2/4) — extraído a un archivo versionado en vez de un
# heredoc embebido en el YAML para que quede bajo ShellCheck/pre-commit como
# cualquier otro script de esta fase, sea diffable de forma normal, y se
# pueda leer/editar sin desenrollar YAML primero.
#
# Cada step de ese job corre en un shell nuevo (GitHub Actions no persiste
# nada entre "run:"), así que esto se `source`ea al principio de cada step
# que lo necesita — nunca se ejecuta solo. Los valores que un step necesita
# pasarle a otro (p. ej. los digests reales resueltos) viajan por
# $GITHUB_ENV, no por este archivo.
set -euo pipefail

REGISTRY="localhost:5000"

# Wrapper usado por todo `docker compose` ad hoc del job — siempre explícito
# sobre -p/-f/--env-file, igual que deploy.sh/rollback.sh, para que nunca
# pueda caer silenciosamente en docker-compose.yml (el de dev, con
# contextos build:) solo porque a alguna llamada se le olvidó una bandera.
#
# Compose interpola el archivo COMPLETO antes de ejecutar cualquier
# subcomando (up/ps/exec/logs/down/config), incluidas las variables de
# servicios que ni siquiera se están tocando — migrate/swarm exigen
# DEPLOY_IMAGE_REF con `${DEPLOY_IMAGE_REF:?...}` (ver docker-compose.staging.yml).
# dc() nunca se usa para desplegar migrate/swarm de verdad (eso solo lo
# hacen deploy.sh/rollback.sh reales, con su propio valor correcto ya
# exportado) — un valor de relleno aquí es inofensivo porque ningún
# subcomando que este wrapper invoca crea o recrea esos dos servicios.
dc() {
    DEPLOY_IMAGE_REF="${DEPLOY_IMAGE_REF:-unused-placeholder-see-ci-deploy-integration-lib}" \
    docker compose -p swarm_trading_staging -f docker-compose.staging.yml --env-file .ci-env-fixture "$@"
}

# Hornea el label OCI de revisión en una imagen nueva derivada del build
# base compartido (docker commit reutiliza capas cacheadas — no reconstruye
# nada), la empuja a través del registry LOCAL únicamente (nunca ghcr.io) —
# así el digest resultante es real, de un round-trip real de push/pull — y
# la alias bajo el nombre ghcr.io que IMAGE_REPO tiene hardcodeado en los
# tres scripts. Ese alias nunca toca la red — es un tag local, solo se lee
# vía `docker image inspect` — pero sus RepoDigests (ligados a la imagen ya
# extraída, no a un tag en particular) son genuinos.
build_release_image() {
    local sha="$1"
    local reg_tag="${REGISTRY}/swarm_trading:sha-${sha}"
    local ghcr_tag="ghcr.io/erickgarciaoj-blip/swarm_trading:sha-${sha}"
    local cid
    cid="$(docker create swarm_test_base:ci)"
    docker commit --change "LABEL org.opencontainers.image.revision=${sha}" "$cid" "$reg_tag" >/dev/null
    docker rm "$cid" >/dev/null
    docker push "$reg_tag" >/dev/null
    docker rmi "$reg_tag" >/dev/null
    docker pull "$reg_tag" >/dev/null
    docker tag "$reg_tag" "$ghcr_tag"
    docker inspect --format '{{index .RepoDigests 0}}' "$ghcr_tag"
}

# shared/.env: un único archivo persistente, creado UNA vez (igual que en el
# runbook, paso 16) — cada release lo consume por symlink, nunca por copia.
# Para SHA_A (el único que pasa por el entrypoint real) el propio entrypoint
# crea y valida ese symlink (ver ci-deploy-entrypoint.sh, paso 4); para los
# SHAs deployados directamente (B-F, bypaseando el entrypoint a propósito)
# prepare_release_dir hace exactamente lo mismo que haría un admin siguiendo
# el runbook para un deploy manual.
setup_shared_env() {
    mkdir -p "$SWARM_ROOT/shared"
    cp .ci-env-fixture "$SWARM_ROOT/shared/.env"
    chmod 600 "$SWARM_ROOT/shared/.env"
}

# Deja $SWARM_ROOT/releases/<sha>/ exactamente como lo dejaría
# ci-deploy-entrypoint.sh, para los escenarios que invocan deploy.sh/
# rollback.sh directamente (a propósito, para ejercitar su propia lógica en
# aislamiento) — siempre con un digest real ya resuelto, para que ninguno de
# los dos intente `docker pull` contra el ghcr.io real.
prepare_release_dir() {
    local sha="$1" compose_staging_src="$2" digest="$3"
    local dir="$SWARM_ROOT/releases/$sha"
    mkdir -p "$dir/scripts"
    cp docker-compose.yml "$dir/docker-compose.yml"
    cp "$compose_staging_src" "$dir/docker-compose.staging.yml"
    cp scripts/deploy.sh "$dir/scripts/deploy.sh"
    cp scripts/rollback.sh "$dir/scripts/rollback.sh"
    chmod +x "$dir/scripts/deploy.sh" "$dir/scripts/rollback.sh"
    ln -sfn ../../shared/.env "$dir/.env"
    printf '%s\n' "$digest" > "$dir/.image-digest"
    local checksum
    checksum="$(cat "$dir/docker-compose.yml" "$dir/docker-compose.staging.yml" "$dir/scripts/deploy.sh" "$dir/scripts/rollback.sh" | sha256sum | cut -d' ' -f1)"
    {
        echo "SHA=$sha"
        echo "ACTOR=ci-integration"
        echo "BUNDLE_SHA256=$checksum"
    } > "$dir/MANIFEST"
}

assert_contains() {
    local haystack_file="$1" needle="$2" desc="$3"
    if grep -qF -- "$needle" "$haystack_file"; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc — expected to find: $needle"
        echo "--- actual output ($haystack_file) ---"
        cat "$haystack_file" >&2
        exit 1
    fi
}

assert_eq() {
    local actual="$1" expected="$2" desc="$3"
    if [ "$actual" = "$expected" ]; then
        echo "PASS: $desc (== $expected)"
    else
        echo "FAIL: $desc — expected '$expected', got '$actual'"
        exit 1
    fi
}

current_sha() {
    if [ -L "$SWARM_ROOT/current" ]; then
        basename "$(readlink -f "$SWARM_ROOT/current")"
    else
        echo "<none>"
    fi
}
