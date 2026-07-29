#!/usr/bin/env bash
#
# Pruebas de deploy.sh y rollback.sh que NO requieren Docker: validación de
# SWARM_ROOT, validación de SHA, manejo de release ausente, y el mecanismo
# real de `flock` no bloqueante (contra un lockfile real, dos procesos
# reales compitiendo por él — no una simulación).
#
# Los casos que sí requieren Docker/Compose real (fallo de backup antes de
# migración, migración protege el servicio activo, rollback automático tras
# fallo de health check, primer deploy con Postgres ya existente, "current"
# no se mueve hasta health exitoso, fallo de rollback sin loop infinito,
# despliegue/redeploy end-to-end, digest/label OCI reales, flock entre dos
# ejecuciones reales) corren en el job aislado
# "deploy-scripts-docker-integration" de ci.yml — no ejecutables en este
# entorno de desarrollo (sin Docker), documentados al final de este archivo
# a título informativo, no como pendiente.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SH="$SCRIPT_DIR/deploy.sh"
ROLLBACK_SH="$SCRIPT_DIR/rollback.sh"

WORKDIR="$(mktemp -d)"
# shellcheck disable=SC2329,SC2317 # invocada vía trap, no directamente — falso positivo conocido (el código exacto varía entre versiones de shellcheck)
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT INT TERM

PASS_COUNT=0
FAIL_COUNT=0
FAILURES=()
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "PASS: $1" >&2; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); FAILURES+=("$1"); echo "FAIL: $1" >&2; }

VALID_SHA="$(printf 'a%.0s' $(seq 1 40))"

# --- SWARM_ROOT: mismo contrato en deploy.sh y rollback.sh -----------------
assert_swarm_root_rejected() {
    local script="$1" label="$2" root="$3"
    if SWARM_ROOT="$root" bash "$script" "$VALID_SHA" >/dev/null 2>&1; then
        fail "$label: SWARM_ROOT='$root' (se esperaba rechazo)"
    else
        pass "$label: SWARM_ROOT='$root'"
    fi
}

for script_pair in "$DEPLOY_SH:deploy.sh" "$ROLLBACK_SH:rollback.sh"; do
    script="${script_pair%%:*}"
    label="${script_pair##*:}"
    assert_swarm_root_rejected "$script" "$label" ""
    assert_swarm_root_rejected "$script" "$label" "/"
    assert_swarm_root_rejected "$script" "$label" "relative/path"
    assert_swarm_root_rejected "$script" "$label" "/opt/swarm-trading/../etc"
done

# --- SHA inválido ------------------------------------------------------
assert_bad_sha_rejected() {
    local script="$1" label="$2" sha="$3"
    if SWARM_ROOT="$WORKDIR/root_$$" bash "$script" "$sha" >/dev/null 2>&1; then
        fail "$label: SHA='$sha' (se esperaba rechazo)"
    else
        pass "$label: SHA='$sha' rechazado"
    fi
}
for script_pair in "$DEPLOY_SH:deploy.sh" "$ROLLBACK_SH:rollback.sh"; do
    script="${script_pair%%:*}"
    label="${script_pair##*:}"
    assert_bad_sha_rejected "$script" "$label" "abc123"
    assert_bad_sha_rejected "$script" "$label" "$(printf 'A%.0s' $(seq 1 40))"
    assert_bad_sha_rejected "$script" "$label" "; rm -rf /"
done

# --- Release inexistente -------------------------------------------------
ROOT_NO_RELEASE="$WORKDIR/root_no_release"
mkdir -p "$ROOT_NO_RELEASE"
if SWARM_ROOT="$ROOT_NO_RELEASE" bash "$DEPLOY_SH" "$VALID_SHA" >/dev/null 2>&1; then
    fail "deploy.sh: release inexistente (se esperaba rechazo)"
else
    pass "deploy.sh: release inexistente rechazado sin tocar Docker"
fi
if SWARM_ROOT="$ROOT_NO_RELEASE" bash "$ROLLBACK_SH" "$VALID_SHA" >/dev/null 2>&1; then
    fail "rollback.sh: release inexistente (se esperaba rechazo)"
else
    pass "rollback.sh: release inexistente rechazado sin reconstruir nada"
fi

# --- Label OCI no coincide — invocación directa de deploy.sh/rollback.sh,
# sin pasar por el entrypoint (hallazgo H1 de la auditoría del PR 2: esta
# rama de verificación no existía en absoluto en la ruta directa). Usa un
# `docker` falso en PATH (mismo patrón que
# test_ci_deploy_entrypoint_rejects_invalid_input.sh) para ejercitar la
# lógica real de deploy.sh/rollback.sh sin depender de Docker/Compose
# reales — el escenario Docker real equivalente vive en el job
# "deploy-scripts-docker-integration" de ci.yml.
#
# Requiere `flock` real (deploy.sh/rollback.sh lo exigen antes de llegar a
# la resolución de imagen) — no disponible por defecto en macOS, igual que
# la prueba de flock más abajo en este mismo archivo.
if ! command -v flock >/dev/null 2>&1; then
    echo "SKIP: flock no disponible en este sistema (macOS) — pruebas de label OCI en deploy.sh/rollback.sh directos no verificables aquí, sí en el VPS objetivo y en el job Docker de CI" >&2
else
FAKE_BIN="$WORKDIR/fakebin"
mkdir -p "$FAKE_BIN"
cat > "$FAKE_BIN/docker" <<'DOCKEREOF'
#!/usr/bin/env bash
set -euo pipefail
case "$1" in
    pull) exit 0 ;;
    inspect)
        fmt=""
        for arg in "$@"; do
            case "$arg" in
                --format) next_is_fmt=1 ;;
                *)
                    if [ "${next_is_fmt:-0}" = "1" ]; then fmt="$arg"; next_is_fmt=0; fi
                    ;;
            esac
        done
        case "$fmt" in
            *Labels*) echo "${FAKE_DOCKER_LABEL_SHA:-}" ;;
            *RepoDigests*)
                echo "${FAKE_DOCKER_DIGEST:-ghcr.io/erickgarciaoj-blip/swarm_trading@sha256:0000000000000000000000000000000000000000000000000000000000000000}"
                ;;
            *) echo "" ;;
        esac
        exit 0 ;;
    *) echo "fake docker: subcomando no soportado en pruebas: $1" >&2; exit 1 ;;
esac
DOCKEREOF
chmod +x "$FAKE_BIN/docker"

prepare_minimal_release() {
    # Uso: prepare_minimal_release <SWARM_ROOT> <sha>
    # Deja releases/<sha>/ con lo mínimo que deploy.sh/rollback.sh exigen
    # ANTES de llegar a la resolución de imagen — a propósito sin
    # .image-digest, para forzar la rama de resolución directa (docker pull
    # + verificación de label) que es la que este bloque quiere ejercitar.
    local root="$1" sha="$2"
    local dir="$root/releases/$sha"
    mkdir -p "$dir/scripts"
    printf 'services: {}\n' > "$dir/docker-compose.yml"
    printf 'services: {}\n' > "$dir/docker-compose.staging.yml"
    printf '#!/usr/bin/env bash\necho deploy\n' > "$dir/scripts/deploy.sh"
    printf '#!/usr/bin/env bash\necho rollback\n' > "$dir/scripts/rollback.sh"
    chmod +x "$dir/scripts/deploy.sh" "$dir/scripts/rollback.sh"
}

LABEL_TEST_SHA="$(printf 'e%.0s' $(seq 1 40))"
WRONG_LABEL_SHA="$(printf 'f%.0s' $(seq 1 40))"

ROOT_LABEL_DEPLOY="$WORKDIR/root_label_deploy"
mkdir -p "$ROOT_LABEL_DEPLOY"
prepare_minimal_release "$ROOT_LABEL_DEPLOY" "$LABEL_TEST_SHA"
if FAKE_DOCKER_LABEL_SHA="$WRONG_LABEL_SHA" SWARM_ROOT="$ROOT_LABEL_DEPLOY" PATH="$FAKE_BIN:$PATH" \
        bash "$DEPLOY_SH" "$LABEL_TEST_SHA" ci-test > "$WORKDIR/label_deploy_out" 2>&1; then
    fail "deploy.sh directo: label OCI no coincide (se esperaba rechazo, salió 0)"
else
    if grep -qi "label OCI" "$WORKDIR/label_deploy_out" && grep -qi "no coincide" "$WORKDIR/label_deploy_out"; then
        pass "deploy.sh directo: label OCI no coincide — rechazado con el mensaje esperado, antes de tocar backup/migración/contenedores"
    else
        fail "deploy.sh directo: label OCI no coincide — rechazado pero sin el mensaje esperado"
        cat "$WORKDIR/label_deploy_out" >&2
    fi
fi
if [ -f "$ROOT_LABEL_DEPLOY/releases/$LABEL_TEST_SHA/.image-digest" ]; then
    fail "deploy.sh directo: label OCI no coincide — pero .image-digest se escribió de todas formas"
else
    pass "deploy.sh directo: label OCI no coincide — .image-digest correctamente ausente"
fi
if [ -e "$ROOT_LABEL_DEPLOY/current" ]; then
    fail "deploy.sh directo: label OCI no coincide — pero 'current' quedó creado"
else
    pass "deploy.sh directo: label OCI no coincide — 'current' correctamente ausente"
fi

ROOT_LABEL_ROLLBACK="$WORKDIR/root_label_rollback"
mkdir -p "$ROOT_LABEL_ROLLBACK"
prepare_minimal_release "$ROOT_LABEL_ROLLBACK" "$LABEL_TEST_SHA"
if FAKE_DOCKER_LABEL_SHA="$WRONG_LABEL_SHA" SWARM_ROOT="$ROOT_LABEL_ROLLBACK" PATH="$FAKE_BIN:$PATH" \
        bash "$ROLLBACK_SH" "$LABEL_TEST_SHA" ci-test > "$WORKDIR/label_rollback_out" 2>&1; then
    fail "rollback.sh directo: label OCI no coincide (se esperaba rechazo, salió 0)"
else
    if grep -qi "label OCI" "$WORKDIR/label_rollback_out" && grep -qi "no coincide" "$WORKDIR/label_rollback_out"; then
        pass "rollback.sh directo: label OCI no coincide — rechazado con el mensaje esperado, antes de tocar el stack"
    else
        fail "rollback.sh directo: label OCI no coincide — rechazado pero sin el mensaje esperado"
        cat "$WORKDIR/label_rollback_out" >&2
    fi
fi
if [ -f "$ROOT_LABEL_ROLLBACK/releases/$LABEL_TEST_SHA/.image-digest" ]; then
    fail "rollback.sh directo: label OCI no coincide — pero .image-digest se escribió de todas formas"
else
    pass "rollback.sh directo: label OCI no coincide — .image-digest correctamente ausente"
fi
if [ -e "$ROOT_LABEL_ROLLBACK/current" ]; then
    fail "rollback.sh directo: label OCI no coincide — pero 'current' quedó creado"
else
    pass "rollback.sh directo: label OCI no coincide — 'current' correctamente ausente"
fi

# --- RepoDigests solo contiene entradas de OTRO repositorio ---------------
# Label correcto (pasa el paso previo), pero ninguna entrada de RepoDigests
# pertenece a IMAGE_REPO — debe rechazar con un mensaje claro, sin caer a la
# primera entrada disponible ni a un índice fijo, y sin morir en silencio
# por `set -e`/`pipefail` cuando el grep no encuentra nada (auditoría del
# PR 2, hallazgo M6 — corrección final).
WRONG_REPO_DIGEST="docker.io/someone-else/unrelated@sha256:1111111111111111111111111111111111111111111111111111111111111111"
DIGEST_TEST_SHA="$(printf 'd%.0s' $(seq 1 40))"

ROOT_DIGEST_DEPLOY="$WORKDIR/root_digest_deploy"
mkdir -p "$ROOT_DIGEST_DEPLOY"
prepare_minimal_release "$ROOT_DIGEST_DEPLOY" "$DIGEST_TEST_SHA"
if FAKE_DOCKER_LABEL_SHA="$DIGEST_TEST_SHA" FAKE_DOCKER_DIGEST="$WRONG_REPO_DIGEST" \
        SWARM_ROOT="$ROOT_DIGEST_DEPLOY" PATH="$FAKE_BIN:$PATH" \
        bash "$DEPLOY_SH" "$DIGEST_TEST_SHA" ci-test > "$WORKDIR/digest_deploy_out" 2>&1; then
    fail "deploy.sh directo: RepoDigests de otro repositorio (se esperaba rechazo, salió 0)"
else
    if grep -qi "RepoDigest" "$WORKDIR/digest_deploy_out" && grep -qF "ghcr.io/erickgarciaoj-blip/swarm_trading" "$WORKDIR/digest_deploy_out"; then
        pass "deploy.sh directo: RepoDigests de otro repositorio — rechazado, el mensaje nombra el repositorio esperado"
    else
        fail "deploy.sh directo: RepoDigests de otro repositorio — rechazado pero sin el mensaje esperado"
        cat "$WORKDIR/digest_deploy_out" >&2
    fi
fi
if [ -f "$ROOT_DIGEST_DEPLOY/releases/$DIGEST_TEST_SHA/.image-digest" ]; then
    fail "deploy.sh directo: RepoDigests de otro repositorio — pero .image-digest se escribió de todas formas"
else
    pass "deploy.sh directo: RepoDigests de otro repositorio — .image-digest correctamente ausente"
fi

ROOT_DIGEST_ROLLBACK="$WORKDIR/root_digest_rollback"
mkdir -p "$ROOT_DIGEST_ROLLBACK"
prepare_minimal_release "$ROOT_DIGEST_ROLLBACK" "$DIGEST_TEST_SHA"
if FAKE_DOCKER_LABEL_SHA="$DIGEST_TEST_SHA" FAKE_DOCKER_DIGEST="$WRONG_REPO_DIGEST" \
        SWARM_ROOT="$ROOT_DIGEST_ROLLBACK" PATH="$FAKE_BIN:$PATH" \
        bash "$ROLLBACK_SH" "$DIGEST_TEST_SHA" ci-test > "$WORKDIR/digest_rollback_out" 2>&1; then
    fail "rollback.sh directo: RepoDigests de otro repositorio (se esperaba rechazo, salió 0)"
else
    if grep -qi "RepoDigest" "$WORKDIR/digest_rollback_out" && grep -qF "ghcr.io/erickgarciaoj-blip/swarm_trading" "$WORKDIR/digest_rollback_out"; then
        pass "rollback.sh directo: RepoDigests de otro repositorio — rechazado, el mensaje nombra el repositorio esperado"
    else
        fail "rollback.sh directo: RepoDigests de otro repositorio — rechazado pero sin el mensaje esperado"
        cat "$WORKDIR/digest_rollback_out" >&2
    fi
fi
if [ -f "$ROOT_DIGEST_ROLLBACK/releases/$DIGEST_TEST_SHA/.image-digest" ]; then
    fail "rollback.sh directo: RepoDigests de otro repositorio — pero .image-digest se escribió de todas formas"
else
    pass "rollback.sh directo: RepoDigests de otro repositorio — .image-digest correctamente ausente"
fi
fi  # command -v flock

# --- flock no bloqueante: mecanismo real, mismo patrón que deploy.sh/
# rollback.sh (exec 9>lockfile; flock -n 9). Dos procesos reales compitiendo
# por el mismo lockfile — no una simulación de la lógica.
#
# `flock` (util-linux) no viene instalado por defecto en macOS — a
# diferencia del VPS objetivo (Debian/Ubuntu, donde es parte del sistema
# base). Sin esta detección, "command not found" en ambos intentos se vería
# como un PASS falso (ambos fallan, pero no por el motivo que se está
# probando). Se reporta explícitamente como no verificable en vez de
# fingir una validación que no ocurrió.
if ! command -v flock >/dev/null 2>&1; then
    echo "SKIP: flock no disponible en este sistema (macOS) — no verificable aquí, sí presente en el VPS objetivo (Debian/Ubuntu, util-linux)" >&2
else
    LOCK_TEST_DIR="$WORKDIR/flock_test"
    mkdir -p "$LOCK_TEST_DIR"
    LOCK_FILE="$LOCK_TEST_DIR/.deploy.lock"

    (
        exec 9>"$LOCK_FILE"
        flock -n 9 || exit 1
        sleep 2
    ) &
    HOLDER_PID=$!

    sleep 0.5  # dar tiempo a que el holder adquiera el lock primero

    if (
        exec 9>"$LOCK_FILE"
        flock -n 9
    ); then
        fail "flock no bloqueante: el segundo intento adquirió el lock mientras el primero lo tenía"
    else
        pass "flock no bloqueante: el segundo intento fue rechazado de inmediato mientras el primero lo tenía"
    fi

    wait "$HOLDER_PID"

    if (
        exec 9>"$LOCK_FILE"
        flock -n 9
    ); then
        pass "flock no bloqueante: liberado el lock, un nuevo intento lo adquiere sin problema"
    else
        fail "flock no bloqueante: el lock no se liberó tras terminar el proceso que lo tenía"
    fi
fi

echo
echo "=== Resultado: $PASS_COUNT pasaron, $FAIL_COUNT fallaron ==="
if [ "$FAIL_COUNT" -gt 0 ]; then
    printf 'Fallos:\n' >&2
    printf ' - %s\n' "${FAILURES[@]}" >&2
    exit 1
fi

cat >&2 <<'EOF'

--- No ejecutado aquí (requiere Docker real) — cubierto por el job aislado
    "deploy-scripts-docker-integration" en .github/workflows/ci.yml ---
  - fallo de backup antes de migración (migración nunca se invoca)
  - migración falla -> servicio activo (swarm) no se toca
  - health check falla -> rollback automático inline, current no se mueve
  - primer deploy con Postgres ya corriendo (con datos) -> sí intenta backup
  - "current" no se mueve hasta que el nuevo stack está saludable
  - rollback cuyo propio health check falla -> no reintenta en loop
  - deploy/redeploy end-to-end contra un stack Compose real
  - digest pinneado end-to-end (tag correcto, digest coincide en pull real)
  - flock entre dos ejecuciones reales de deploy.sh, no solo el mecanismo en solitario
EOF
