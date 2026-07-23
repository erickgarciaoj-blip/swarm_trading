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
