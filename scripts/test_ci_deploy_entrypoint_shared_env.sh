#!/usr/bin/env bash
#
# Valida el paso "shared/.env" de ci-deploy-entrypoint.sh (Fase 4.6, PR 2/4
# — cierre del gap de secretos, ver ADR-0011): shared/.env debe existir, ser
# un archivo regular, no world-readable, pertenecer al usuario que corre el
# entrypoint, y el symlink resultante releases/<sha>/.env debe apuntar
# exactamente a ../../shared/.env — todo antes de exec deploy.sh (o sea,
# antes de backup, migración o cualquier cambio de contenedores).
#
# Corre el script REAL, igual que
# test_ci_deploy_entrypoint_rejects_invalid_input.sh: un SWARM_ROOT temporal
# (nunca /opt/swarm-trading) y un `docker` falso en PATH (este entorno de
# desarrollo no tiene Docker) que simula las respuestas de imagen/label/
# digest que el entrypoint espera — el bundle de estas pruebas siempre es
# válido y estable, lo único que varía es el estado de shared/.env y de
# releases/<sha>/.env antes de invocar el script.
set -euo pipefail

# BSD tar (macOS) escribe metadatos AppleDouble ("._archivo") junto a cada
# entrada al archivar rutas con xattrs — inofensivo para producción (el VPS
# objetivo es Debian/Ubuntu con GNU tar) pero rompe la allowlist del
# entrypoint en un entorno de desarrollo macOS. No tiene efecto en GNU tar.
export COPYFILE_DISABLE=1

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENTRYPOINT="$SCRIPT_DIR/ci-deploy-entrypoint.sh"

WORKDIR="$(mktemp -d)"
# shellcheck disable=SC2329,SC2317 # invocada vía trap, no directamente — falso positivo conocido (el código exacto varía entre versiones de shellcheck)
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT INT TERM

PASS_COUNT=0
FAIL_COUNT=0
FAILURES=()
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "PASS: $1" >&2; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); FAILURES+=("$1"); echo "FAIL: $1" >&2; }

# --- docker falso — mismo contrato que test_ci_deploy_entrypoint_rejects_invalid_input.sh:
# la imagen "conocida" siempre existe local, su label OCI coincide con el
# SHA pedido, su digest resuelto es fijo. Estas pruebas no ejercitan esa
# parte del entrypoint (ya cubierta por el otro archivo) — solo necesitan
# que llegue viva hasta el paso de shared/.env y, si lo pasa, hasta el exec
# final.
FAKE_BIN="$WORKDIR/fakebin"
mkdir -p "$FAKE_BIN"
cat > "$FAKE_BIN/docker" <<'DOCKEREOF'
#!/usr/bin/env bash
set -euo pipefail
case "$1" in
    image) exit 0 ;;
    pull) echo "fake docker: pull inesperado de $2" >&2; exit 1 ;;
    inspect)
        fmt=""; ref=""
        for arg in "$@"; do
            case "$arg" in
                --format) next_is_fmt=1 ;;
                *)
                    if [ "${next_is_fmt:-0}" = "1" ]; then fmt="$arg"; next_is_fmt=0;
                    else ref="$arg"; fi
                    ;;
            esac
        done
        case "$fmt" in
            *Labels*) echo "${FAKE_DOCKER_LABEL_SHA:-}" ;;
            *RepoDigests*) echo "${FAKE_DOCKER_DIGEST:-ghcr.io/erickgarciaoj-blip/swarm_trading@sha256:0000000000000000000000000000000000000000000000000000000000000000}" ;;
            *) echo "" ;;
        esac
        exit 0 ;;
    *) echo "fake docker: subcomando no soportado en pruebas: $1" >&2; exit 1 ;;
esac
DOCKEREOF
chmod +x "$FAKE_BIN/docker"

# --- constructor de bundles válidos --------------------------------------
# deploy.sh/rollback.sh son stubs triviales aquí a propósito — estas
# pruebas ejercitan únicamente la lógica de shared/.env del entrypoint, no
# el deploy real (eso lo cubren test_deploy_and_rollback_scripts.sh y el
# job deploy-scripts-docker-integration).
build_bundle() {
    # Uso: build_bundle <archivo_tar_salida> <sha>
    # Imprime por stdout el directorio de staging usado — para poder
    # pre-sembrar un release con contenido bit-a-bit idéntico (mismo
    # checksum) cuando una prueba necesita forzar la rama IDEMPOTENT real
    # del entrypoint en vez de PROMOTED.
    local out="$1" sha="$2"
    local bdir
    bdir="$(mktemp -d "$WORKDIR/bundle.XXXXXX")"
    mkdir -p "$bdir/scripts"
    printf 'services: {}\n' > "$bdir/docker-compose.yml"
    printf 'services: {}\n' > "$bdir/docker-compose.staging.yml"
    printf '#!/usr/bin/env bash\necho deploy\n' > "$bdir/scripts/deploy.sh"
    printf '#!/usr/bin/env bash\necho rollback\n' > "$bdir/scripts/rollback.sh"
    chmod +x "$bdir/scripts/deploy.sh" "$bdir/scripts/rollback.sh"
    local checksum
    checksum="$(cat "$bdir/docker-compose.yml" "$bdir/docker-compose.staging.yml" "$bdir/scripts/deploy.sh" "$bdir/scripts/rollback.sh" | shasum -a 256 | cut -d' ' -f1)"
    {
        echo "SHA=$sha"
        echo "ACTOR=octocat"
        echo "BUNDLE_SHA256=$checksum"
    } > "$bdir/MANIFEST"
    tar -C "$bdir" -cf "$out" docker-compose.yml docker-compose.staging.yml scripts MANIFEST
    printf '%s' "$bdir"
}

# Pre-siembra releases/<sha>/ con el mismo contenido que <bundle_dir> (los 4
# payloads) más un symlink .env ya presente mal apuntado — fuerza a que el
# entrypoint tome su propia rama IDEMPOTENT real (mismo SHA, checksum
# coincide) en vez de crashear por "falta el payload" o rechazar por
# "inconsistencia de integridad". Esa rama nunca toca el resto del
# contenido ya presente, así que deja en pie el symlink incorrecto hasta
# que el paso de shared/.env lo corrija (o falle) por su cuenta.
preseed_release_with_env_link() {
    local root="$1" sha="$2" bundle_dir="$3" link_target="$4"
    local dir="$root/releases/$sha"
    mkdir -p "$dir/scripts"
    cp "$bundle_dir/docker-compose.yml" "$dir/docker-compose.yml"
    cp "$bundle_dir/docker-compose.staging.yml" "$dir/docker-compose.staging.yml"
    cp "$bundle_dir/scripts/deploy.sh" "$dir/scripts/deploy.sh"
    cp "$bundle_dir/scripts/rollback.sh" "$dir/scripts/rollback.sh"
    chmod +x "$dir/scripts/deploy.sh" "$dir/scripts/rollback.sh"
    ln -sfn "$link_target" "$dir/.env"
    # La rama IDEMPOTENT nunca escribe un MANIFEST nuevo (solo compara los
    # 4 payloads y, si coinciden, deja todo lo demás tal cual) — el paso 6
    # del entrypoint (exec deploy.sh) sí necesita leer ACTOR= de un
    # MANIFEST ya presente, así que la pre-siembra debe incluir uno.
    local checksum
    checksum="$(cat "$dir/docker-compose.yml" "$dir/docker-compose.staging.yml" "$dir/scripts/deploy.sh" "$dir/scripts/rollback.sh" | shasum -a 256 | cut -d' ' -f1)"
    {
        echo "SHA=$sha"
        echo "ACTOR=octocat"
        echo "BUNDLE_SHA256=$checksum"
    } > "$dir/MANIFEST"
}

setup_shared_env() {
    # Uso: setup_shared_env <SWARM_ROOT> [permisos octales, default 600]
    local root="$1" perms="${2:-600}"
    mkdir -p "$root/shared"
    printf 'POSTGRES_PASSWORD=ci-test\n' > "$root/shared/.env"
    chmod "$perms" "$root/shared/.env"
}

run_entrypoint() {
    # Uso: run_entrypoint <SWARM_ROOT> <comando SSH> <bundle> [label-sha-falso]
    local root="$1" cmd="$2" bundle="$3" label_sha="${4:-}"
    FAKE_DOCKER_LABEL_SHA="$label_sha" \
    SWARM_ROOT="$root" \
    SSH_ORIGINAL_COMMAND="$cmd" \
    PATH="$FAKE_BIN:$PATH" \
        bash "$ENTRYPOINT" < "$bundle" > "$WORKDIR/last_stdout" 2> "$WORKDIR/last_stderr"
}

link_target_of() {
    readlink "$1" 2>/dev/null || echo "<ausente>"
}

# =========================================================================
# 1. shared/.env inexistente
# =========================================================================
SHA1="$(printf '1%.0s' $(seq 1 40))"
B1="$WORKDIR/b1.tar"
build_bundle "$B1" "$SHA1" > /dev/null
ROOT1="$(mktemp -d "$WORKDIR/root.XXXXXX")"
mkdir -p "$ROOT1/shared"   # el directorio existe, el archivo no

if run_entrypoint "$ROOT1" "deploy $SHA1" "$B1" "$SHA1"; then
    fail "shared/.env inexistente (se esperaba rechazo)"
else
    if grep -qF "shared/.env no existe" "$WORKDIR/last_stderr"; then
        pass "shared/.env inexistente — rechazado con el mensaje esperado"
    else
        fail "shared/.env inexistente — rechazado pero sin el mensaje esperado"
    fi
    if [ -e "$ROOT1/releases/$SHA1/.env" ]; then
        fail "shared/.env inexistente — pero releases/$SHA1/.env quedó creado"
    else
        pass "shared/.env inexistente — releases/$SHA1/.env correctamente ausente"
    fi
fi

# =========================================================================
# 2. shared/.env con permisos demasiado abiertos (world-readable)
# =========================================================================
SHA2="$(printf '2%.0s' $(seq 1 40))"
B2="$WORKDIR/b2.tar"
build_bundle "$B2" "$SHA2" > /dev/null
ROOT2="$(mktemp -d "$WORKDIR/root.XXXXXX")"
setup_shared_env "$ROOT2" 644

if run_entrypoint "$ROOT2" "deploy $SHA2" "$B2" "$SHA2"; then
    fail "shared/.env world-readable (se esperaba rechazo)"
else
    if grep -qF "world-readable" "$WORKDIR/last_stderr"; then
        pass "shared/.env world-readable (644) — rechazado con el mensaje esperado"
    else
        fail "shared/.env world-readable — rechazado pero sin el mensaje esperado"
    fi
    if [ -e "$ROOT2/releases/$SHA2/.env" ]; then
        fail "shared/.env world-readable — pero releases/$SHA2/.env quedó creado"
    else
        pass "shared/.env world-readable — releases/$SHA2/.env correctamente ausente"
    fi
fi

# --- variante: shared/.env no es un archivo regular (es un symlink) -----
# No es uno de los 5 casos pedidos explícitamente, pero "que es archivo
# regular" sí es un requisito explícito del entrypoint — se prueba aparte
# para no dejarlo sin cubrir.
SHA2B="$(printf 'b%.0s' $(seq 1 40))"
B2B="$WORKDIR/b2b.tar"
build_bundle "$B2B" "$SHA2B" > /dev/null
ROOT2B="$(mktemp -d "$WORKDIR/root.XXXXXX")"
mkdir -p "$ROOT2B/shared"
printf 'POSTGRES_PASSWORD=elsewhere\n' > "$WORKDIR/somewhere.env"
ln -s "$WORKDIR/somewhere.env" "$ROOT2B/shared/.env"

if run_entrypoint "$ROOT2B" "deploy $SHA2B" "$B2B" "$SHA2B"; then
    fail "shared/.env es un symlink (se esperaba rechazo — debe ser archivo regular)"
else
    if grep -qF "no puede ser en sí mismo un symlink" "$WORKDIR/last_stderr"; then
        pass "shared/.env es un symlink — rechazado con el mensaje esperado"
    else
        fail "shared/.env es un symlink — rechazado pero sin el mensaje esperado"
    fi
fi

# =========================================================================
# 3. releases/<sha>/.env ya existía como symlink apuntando a otro lugar —
#    el entrypoint debe corregirlo, nunca dejarlo como estaba.
# =========================================================================
SHA3="$(printf '3%.0s' $(seq 1 40))"
B3="$WORKDIR/b3.tar"
BDIR3="$(build_bundle "$B3" "$SHA3")"
ROOT3="$(mktemp -d "$WORKDIR/root.XXXXXX")"
setup_shared_env "$ROOT3" 600
preseed_release_with_env_link "$ROOT3" "$SHA3" "$BDIR3" "/etc/passwd"

BEFORE="$(link_target_of "$ROOT3/releases/$SHA3/.env")"
if [ "$BEFORE" != "/etc/passwd" ]; then
    fail "test mal construido: el symlink incorrecto pre-sembrado no quedó como se esperaba ('$BEFORE')"
fi

if run_entrypoint "$ROOT3" "deploy $SHA3" "$B3" "$SHA3"; then
    AFTER="$(link_target_of "$ROOT3/releases/$SHA3/.env")"
    if [ "$AFTER" = "../../shared/.env" ]; then
        pass "symlink apuntando a otro lugar (/etc/passwd) — corregido a ../../shared/.env"
    else
        fail "symlink apuntando a otro lugar — no se corrigió (sigue en '$AFTER')"
    fi
else
    fail "symlink apuntando a otro lugar con shared/.env válido (se esperaba aceptación tras corregir el symlink)"
    cat "$WORKDIR/last_stderr" >&2
fi

# =========================================================================
# 4. Release promovida (primera vez, rama PROMOTED) con symlink correcto
# =========================================================================
SHA4="$(printf '4%.0s' $(seq 1 40))"
B4="$WORKDIR/b4.tar"
build_bundle "$B4" "$SHA4" > /dev/null
ROOT4="$(mktemp -d "$WORKDIR/root.XXXXXX")"
setup_shared_env "$ROOT4" 600

if run_entrypoint "$ROOT4" "deploy $SHA4" "$B4" "$SHA4"; then
    LINK="$(link_target_of "$ROOT4/releases/$SHA4/.env")"
    if [ "$LINK" = "../../shared/.env" ]; then
        pass "release promovida (PROMOTED) — releases/$SHA4/.env -> ../../shared/.env"
    else
        fail "release promovida — symlink incorrecto o ausente: '$LINK'"
    fi
    if grep -qF "bundle validado y extraído a releases/$SHA4" "$ROOT4/logs/deploy.log"; then
        pass "release promovida — confirmado que pasó por la rama PROMOTED, no IDEMPOTENT"
    else
        fail "release promovida — el log no confirma la rama PROMOTED"
    fi
else
    fail "release promovida con shared/.env válido (se esperaba aceptación)"
    cat "$WORKDIR/last_stderr" >&2
fi

# =========================================================================
# 5. Redeploy del mismo SHA (rama IDEMPOTENT) preserva el symlink correcto
# =========================================================================
SHA5="$(printf '5%.0s' $(seq 1 40))"
B5="$WORKDIR/b5.tar"
build_bundle "$B5" "$SHA5" > /dev/null
ROOT5="$(mktemp -d "$WORKDIR/root.XXXXXX")"
setup_shared_env "$ROOT5" 600

run_entrypoint "$ROOT5" "deploy $SHA5" "$B5" "$SHA5" || {
    fail "redeploy del mismo SHA — el primer deploy debía aceptarse"
    cat "$WORKDIR/last_stderr" >&2
}
LINK_FIRST="$(link_target_of "$ROOT5/releases/$SHA5/.env")"

if run_entrypoint "$ROOT5" "deploy $SHA5" "$B5" "$SHA5"; then
    LINK_SECOND="$(link_target_of "$ROOT5/releases/$SHA5/.env")"
    if [ "$LINK_FIRST" = "../../shared/.env" ] && [ "$LINK_SECOND" = "../../shared/.env" ]; then
        pass "redeploy del mismo SHA — symlink correcto se preserva entre el primer deploy y el redeploy"
    else
        fail "redeploy del mismo SHA — symlink no se preservó (primero='$LINK_FIRST' segundo='$LINK_SECOND')"
    fi
    if grep -qF "redeploy idéntico del mismo SHA — release existente reutilizada" "$ROOT5/logs/deploy.log"; then
        pass "redeploy del mismo SHA — confirmado que el segundo intento tomó la rama IDEMPOTENT"
    else
        fail "redeploy del mismo SHA — el log no confirma la rama IDEMPOTENT en el segundo intento"
    fi
else
    fail "redeploy del mismo SHA (se esperaba aceptación en el segundo intento)"
    cat "$WORKDIR/last_stderr" >&2
fi

echo
echo "=== Resultado: $PASS_COUNT pasaron, $FAIL_COUNT fallaron ==="
if [ "$FAIL_COUNT" -gt 0 ]; then
    printf 'Fallos:\n' >&2
    printf ' - %s\n' "${FAILURES[@]}" >&2
    exit 1
fi
