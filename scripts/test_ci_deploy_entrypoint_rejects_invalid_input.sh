#!/usr/bin/env bash
#
# Valida que scripts/ci-deploy-entrypoint.sh rechaza exactamente lo que
# debe rechazar (comando malformado, path traversal, rutas absolutas,
# symlinks/hardlinks, entradas duplicadas, nombres con caracteres de
# control, archivos fuera de la allowlist, manifest inconsistente, checksum
# inválido, bundle vacío/demasiado grande, SWARM_ROOT inválido) y que
# acepta correctamente un bundle válido, incluyendo el caso de redeploy
# idéntico del mismo SHA y el rechazo de una release existente con
# contenido distinto para el mismo SHA.
#
# Corre el script REAL, no una reimplementación de su lógica. Usa un
# SWARM_ROOT temporal (nunca toca /opt/swarm-trading) y un `docker` falso
# en PATH (este entorno de desarrollo no tiene Docker) que simula
# exactamente las respuestas que el entrypoint espera de él — permite
# ejercitar el flujo completo, incluida la verificación de imagen/label/
# digest, sin una instalación real de Docker.
#
# Limpia su propio directorio de trabajo siempre, éxito o fallo.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENTRYPOINT="$SCRIPT_DIR/ci-deploy-entrypoint.sh"

WORKDIR="$(mktemp -d)"
# shellcheck disable=SC2329,SC2317 # invocada vía trap, no directamente — falso positivo conocido (el código exacto varía entre versiones de shellcheck)
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT INT TERM

PASS_COUNT=0
FAIL_COUNT=0
FAILURES=()

# stderr, no stdout — assert_accepted usa $(...) para devolver un valor
# limpio (la ruta de SWARM_ROOT de prueba) por stdout; mezclar mensajes
# aquí corrompería esa captura.
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "PASS: $1" >&2; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); FAILURES+=("$1"); echo "FAIL: $1" >&2; }

# --- docker falso -----------------------------------------------------
# Responde exactamente lo que el entrypoint necesita para las pruebas que
# llegan hasta la verificación de imagen: la imagen "conocida" siempre
# existe local (nunca se llama a pull), su label OCI coincide con el SHA
# pedido, y su digest resuelto es fijo y predecible.
FAKE_BIN="$WORKDIR/fakebin"
mkdir -p "$FAKE_BIN"
cat > "$FAKE_BIN/docker" <<'DOCKEREOF'
#!/usr/bin/env bash
set -euo pipefail
case "$1" in
    image)
        # docker image inspect <ref> — siempre "existe local" en estas pruebas
        exit 0
        ;;
    pull)
        # No debería llamarse nunca (image inspect ya dice que existe) —
        # si se llama, algo en el flujo cambió; fallar ruidosamente.
        echo "fake docker: pull inesperado de $2" >&2
        exit 1
        ;;
    inspect)
        fmt=""
        ref=""
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
        exit 0
        ;;
    *)
        echo "fake docker: subcomando no soportado en pruebas: $1" >&2
        exit 1
        ;;
esac
DOCKEREOF
chmod +x "$FAKE_BIN/docker"

# --- constructor de bundles (Python: control preciso de tipos de entrada
# tar que `tar` de línea de comandos no puede crear de forma portable —
# hardlinks, symlinks, nombres con caracteres de control) ----------------
BUILD_BUNDLE="$WORKDIR/build_bundle.py"
cat > "$BUILD_BUNDLE" <<'PYEOF'
import hashlib
import io
import sys
import tarfile

VALID_COMPOSE = b"services: {}\n"
VALID_DEPLOY_SH = b"#!/usr/bin/env bash\necho deploy\n"
VALID_ROLLBACK_SH = b"#!/usr/bin/env bash\necho rollback\n"


def add_file(tar, name, content, mode=0o644):
    info = tarfile.TarInfo(name=name)
    info.size = len(content)
    info.mode = mode
    tar.addfile(info, io.BytesIO(content))


def add_dir(tar, name, mode=0o755):
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.DIRTYPE
    info.mode = mode
    tar.addfile(info)


def add_symlink(tar, name, target):
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    tar.addfile(info)


def add_hardlink(tar, name, target):
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.LNKTYPE
    info.linkname = target
    tar.addfile(info)


def payload_checksum(compose, staging, deploy_sh, rollback_sh):
    h = hashlib.sha256()
    h.update(compose)
    h.update(staging)
    h.update(deploy_sh)
    h.update(rollback_sh)
    return h.hexdigest()


def build_valid(out_path, sha, actor="octocat", checksum_override=None,
                 compose=VALID_COMPOSE, staging=VALID_COMPOSE,
                 deploy_sh=VALID_DEPLOY_SH, rollback_sh=VALID_ROLLBACK_SH,
                 skip_manifest=False, extra=None):
    checksum = checksum_override or payload_checksum(compose, staging, deploy_sh, rollback_sh)
    manifest = f"SHA={sha}\nACTOR={actor}\nBUNDLE_SHA256={checksum}\n".encode("ascii")
    with tarfile.open(out_path, "w") as tar:
        add_dir(tar, "scripts")
        add_file(tar, "docker-compose.yml", compose)
        add_file(tar, "docker-compose.staging.yml", staging)
        add_file(tar, "scripts/deploy.sh", deploy_sh, mode=0o755)
        add_file(tar, "scripts/rollback.sh", rollback_sh, mode=0o755)
        if not skip_manifest:
            add_file(tar, "MANIFEST", manifest)
        if extra:
            extra(tar)


def main():
    mode = sys.argv[1]
    out_path = sys.argv[2]
    sha = sys.argv[3] if len(sys.argv) > 3 else "a" * 40

    if mode == "valid":
        build_valid(out_path, sha)
    elif mode == "path-traversal":
        def extra(tar):
            add_file(tar, "scripts/../../../etc/passwd", b"pwned")
        build_valid(out_path, sha, extra=extra)
    elif mode == "absolute-path":
        def extra(tar):
            add_file(tar, "/etc/cron.d/evil", b"pwned")
        build_valid(out_path, sha, extra=extra)
    elif mode == "symlink":
        def extra(tar):
            add_symlink(tar, "scripts/evil-link", "/etc/passwd")
        build_valid(out_path, sha, extra=extra)
    elif mode == "hardlink":
        def extra(tar):
            add_hardlink(tar, "scripts/evil-hardlink", "docker-compose.yml")
        build_valid(out_path, sha, extra=extra)
    elif mode == "duplicate-entry":
        def extra(tar):
            add_file(tar, "docker-compose.yml", b"segunda version distinta")
        build_valid(out_path, sha, extra=extra)
    elif mode == "control-char-name":
        def extra(tar):
            add_file(tar, "scripts/evil\nname.sh", b"x")
        build_valid(out_path, sha, extra=extra)
    elif mode == "outside-allowlist":
        def extra(tar):
            add_file(tar, "scripts/not-allowed.sh", b"x")
        build_valid(out_path, sha, extra=extra)
    elif mode == "unexpected-exec-bit":
        build_valid(out_path, sha, compose=VALID_COMPOSE)
        # reabrir y agregar docker-compose.yml con bit ejecutable en su lugar
        with tarfile.open(out_path, "w") as tar:
            add_dir(tar, "scripts")
            add_file(tar, "docker-compose.yml", VALID_COMPOSE, mode=0o755)  # inesperado
            add_file(tar, "docker-compose.staging.yml", VALID_COMPOSE)
            add_file(tar, "scripts/deploy.sh", VALID_DEPLOY_SH, mode=0o755)
            add_file(tar, "scripts/rollback.sh", VALID_ROLLBACK_SH, mode=0o755)
            checksum = payload_checksum(VALID_COMPOSE, VALID_COMPOSE, VALID_DEPLOY_SH, VALID_ROLLBACK_SH)
            add_file(tar, "MANIFEST", f"SHA={sha}\nACTOR=octocat\nBUNDLE_SHA256={checksum}\n".encode())
    elif mode == "missing-exec-bit":
        with tarfile.open(out_path, "w") as tar:
            add_dir(tar, "scripts")
            add_file(tar, "docker-compose.yml", VALID_COMPOSE)
            add_file(tar, "docker-compose.staging.yml", VALID_COMPOSE)
            add_file(tar, "scripts/deploy.sh", VALID_DEPLOY_SH, mode=0o644)  # falta +x
            add_file(tar, "scripts/rollback.sh", VALID_ROLLBACK_SH, mode=0o755)
            checksum = payload_checksum(VALID_COMPOSE, VALID_COMPOSE, VALID_DEPLOY_SH, VALID_ROLLBACK_SH)
            add_file(tar, "MANIFEST", f"SHA={sha}\nACTOR=octocat\nBUNDLE_SHA256={checksum}\n".encode())
    elif mode == "manifest-sha-mismatch":
        build_valid(out_path, sha, checksum_override=None)
        # el MANIFEST declara un SHA distinto al pedido por el comando SSH
        other_sha = "b" * 40 if sha != "b" * 40 else "c" * 40
        build_valid(out_path, other_sha)
    elif mode == "manifest-bad-format":
        def extra_replace():
            pass
        checksum = payload_checksum(VALID_COMPOSE, VALID_COMPOSE, VALID_DEPLOY_SH, VALID_ROLLBACK_SH)
        with tarfile.open(out_path, "w") as tar:
            add_dir(tar, "scripts")
            add_file(tar, "docker-compose.yml", VALID_COMPOSE)
            add_file(tar, "docker-compose.staging.yml", VALID_COMPOSE)
            add_file(tar, "scripts/deploy.sh", VALID_DEPLOY_SH, mode=0o755)
            add_file(tar, "scripts/rollback.sh", VALID_ROLLBACK_SH, mode=0o755)
            add_file(tar, "MANIFEST", f"SHA={sha}\nACTOR=octocat\nBUNDLE_SHA256={checksum}\nLINEA_EXTRA=x\n".encode())
    elif mode == "manifest-bad-actor":
        checksum = payload_checksum(VALID_COMPOSE, VALID_COMPOSE, VALID_DEPLOY_SH, VALID_ROLLBACK_SH)
        with tarfile.open(out_path, "w") as tar:
            add_dir(tar, "scripts")
            add_file(tar, "docker-compose.yml", VALID_COMPOSE)
            add_file(tar, "docker-compose.staging.yml", VALID_COMPOSE)
            add_file(tar, "scripts/deploy.sh", VALID_DEPLOY_SH, mode=0o755)
            add_file(tar, "scripts/rollback.sh", VALID_ROLLBACK_SH, mode=0o755)
            add_file(tar, "MANIFEST", f"SHA={sha}\nACTOR=evil; rm -rf /\nBUNDLE_SHA256={checksum}\n".encode())
    elif mode == "checksum-invalid":
        checksum = "0" * 64
        with tarfile.open(out_path, "w") as tar:
            add_dir(tar, "scripts")
            add_file(tar, "docker-compose.yml", VALID_COMPOSE)
            add_file(tar, "docker-compose.staging.yml", VALID_COMPOSE)
            add_file(tar, "scripts/deploy.sh", VALID_DEPLOY_SH, mode=0o755)
            add_file(tar, "scripts/rollback.sh", VALID_ROLLBACK_SH, mode=0o755)
            add_file(tar, "MANIFEST", f"SHA={sha}\nACTOR=octocat\nBUNDLE_SHA256={checksum}\n".encode())
    elif mode == "empty":
        open(out_path, "wb").close()
    elif mode == "different-content":
        # Bundle válido y auto-consistente (checksum correcto para SU
        # PROPIO contenido) pero con payload distinto al de "valid" para
        # el mismo SHA — simula un segundo build real del mismo commit
        # (o un intento de sobrescribir con contenido no idéntico).
        build_valid(out_path, sha, actor="alguien-distinto",
                    compose=b"services: {distinto: true}\n")
    elif mode == "oversized":
        with open(out_path, "wb") as f:
            f.write(b"0" * (3 * 1024 * 1024))  # 3 MiB > limite de 2 MiB
    else:
        sys.exit(f"modo desconocido: {mode}")


if __name__ == "__main__":
    main()
PYEOF

build_bundle() { python3 "$BUILD_BUNDLE" "$@"; }

run_entrypoint() {
    # $1=SWARM_ROOT de prueba  $2=comando SSH  $3=archivo bundle  $4=label-sha-falso (opcional)
    local root="$1" cmd="$2" bundle="$3" label_sha="${4:-}"
    FAKE_DOCKER_LABEL_SHA="$label_sha" \
    SWARM_ROOT="$root" \
    SSH_ORIGINAL_COMMAND="$cmd" \
    PATH="$FAKE_BIN:$PATH" \
        bash "$ENTRYPOINT" < "$bundle" > "$WORKDIR/last_stdout" 2> "$WORKDIR/last_stderr"
}

assert_rejected() {
    local desc="$1" sha="$2" cmd="$3" bundle="$4"
    local root; root="$(mktemp -d "$WORKDIR/root.XXXXXX")"
    if run_entrypoint "$root" "$cmd" "$bundle" "$sha"; then
        fail "$desc (se esperaba rechazo, salió 0)"
        return
    fi
    if [ -d "$root/releases/$sha" ]; then
        fail "$desc (rechazado pero releases/$sha quedó creado)"
        return
    fi
    pass "$desc"
}

assert_accepted() {
    local desc="$1" sha="$2" cmd="$3" bundle="$4"
    local root; root="$(mktemp -d "$WORKDIR/root.XXXXXX")"
    # shared/.env es requisito obligatorio desde el paso 4 del entrypoint
    # (ver test_ci_deploy_entrypoint_shared_env.sh para la cobertura
    # dedicada de ese paso) — sin esto, ningún camino de aceptación llega
    # siquiera a la verificación de imagen/label.
    mkdir -p "$root/shared"
    printf 'POSTGRES_PASSWORD=ci-test\n' > "$root/shared/.env"
    chmod 600 "$root/shared/.env"
    if ! FAKE_DOCKER_LABEL_SHA="$sha" run_entrypoint "$root" "$cmd" "$bundle" "$sha"; then
        cat "$WORKDIR/last_stderr" >&2
        fail "$desc (se esperaba aceptación, entrypoint rechazó)"
        return
    fi
    if [ ! -d "$root/releases/$sha" ]; then
        fail "$desc (aceptado pero releases/$sha no existe)"
        return
    fi
    if [ ! -f "$root/releases/$sha/.image-digest" ]; then
        fail "$desc (aceptado pero .image-digest no se escribió)"
        return
    fi
    pass "$desc"
    printf '%s' "$root"
}

SHA="$(printf 'a%.0s' $(seq 1 40))"

# --- comando SSH malformado ------------------------------------------------
B_VALID="$WORKDIR/valid.tar"; build_bundle valid "$B_VALID" "$SHA"

assert_rejected "comando vacío" "$SHA" "" "$B_VALID"
assert_rejected "verbo distinto de deploy" "$SHA" "destroy $SHA" "$B_VALID"
assert_rejected "SHA corto" "$SHA" "deploy abc123" "$B_VALID"
assert_rejected "SHA en mayúsculas" "$SHA" "deploy $(printf 'A%.0s' $(seq 1 40))" "$B_VALID"
assert_rejected "inyección de shell en el comando" "$SHA" "deploy $SHA; rm -rf /" "$B_VALID"
assert_rejected "argumentos extra" "$SHA" "deploy $SHA --force" "$B_VALID"

# --- bundle: tamaño ---------------------------------------------------------
B_EMPTY="$WORKDIR/empty.tar"; build_bundle empty "$B_EMPTY"
assert_rejected "bundle vacío" "$SHA" "deploy $SHA" "$B_EMPTY"

B_HUGE="$WORKDIR/huge.tar"; build_bundle oversized "$B_HUGE"
assert_rejected "bundle demasiado grande" "$SHA" "deploy $SHA" "$B_HUGE"

# --- bundle: estructura del tar --------------------------------------------
B_TRAVERSAL="$WORKDIR/traversal.tar"; build_bundle path-traversal "$B_TRAVERSAL" "$SHA"
assert_rejected "path traversal ('..')" "$SHA" "deploy $SHA" "$B_TRAVERSAL"

B_ABS="$WORKDIR/abs.tar"; build_bundle absolute-path "$B_ABS" "$SHA"
assert_rejected "ruta absoluta" "$SHA" "deploy $SHA" "$B_ABS"

B_SYM="$WORKDIR/sym.tar"; build_bundle symlink "$B_SYM" "$SHA"
assert_rejected "symlink malicioso" "$SHA" "deploy $SHA" "$B_SYM"

B_HARD="$WORKDIR/hard.tar"; build_bundle hardlink "$B_HARD" "$SHA"
assert_rejected "hardlink malicioso" "$SHA" "deploy $SHA" "$B_HARD"

B_DUP="$WORKDIR/dup.tar"; build_bundle duplicate-entry "$B_DUP" "$SHA"
assert_rejected "entrada duplicada" "$SHA" "deploy $SHA" "$B_DUP"

B_CTRL="$WORKDIR/ctrl.tar"; build_bundle control-char-name "$B_CTRL" "$SHA"
assert_rejected "nombre con salto de línea" "$SHA" "deploy $SHA" "$B_CTRL"

B_OUTSIDE="$WORKDIR/outside.tar"; build_bundle outside-allowlist "$B_OUTSIDE" "$SHA"
assert_rejected "archivo fuera de la allowlist" "$SHA" "deploy $SHA" "$B_OUTSIDE"

B_UNEXP_EXEC="$WORKDIR/unexp_exec.tar"; build_bundle unexpected-exec-bit "$B_UNEXP_EXEC" "$SHA"
assert_rejected "bit ejecutable inesperado (docker-compose.yml)" "$SHA" "deploy $SHA" "$B_UNEXP_EXEC"

B_MISSING_EXEC="$WORKDIR/missing_exec.tar"; build_bundle missing-exec-bit "$B_MISSING_EXEC" "$SHA"
assert_rejected "falta bit ejecutable (scripts/deploy.sh)" "$SHA" "deploy $SHA" "$B_MISSING_EXEC"

# --- bundle: manifest e integridad -----------------------------------------
B_SHA_MISMATCH="$WORKDIR/sha_mismatch.tar"; build_bundle manifest-sha-mismatch "$B_SHA_MISMATCH" "$SHA"
assert_rejected "manifest con SHA distinto al del comando" "$SHA" "deploy $SHA" "$B_SHA_MISMATCH"

B_BAD_FORMAT="$WORKDIR/bad_format.tar"; build_bundle manifest-bad-format "$B_BAD_FORMAT" "$SHA"
assert_rejected "manifest con línea extra" "$SHA" "deploy $SHA" "$B_BAD_FORMAT"

B_BAD_ACTOR="$WORKDIR/bad_actor.tar"; build_bundle manifest-bad-actor "$B_BAD_ACTOR" "$SHA"
assert_rejected "manifest con actor inválido" "$SHA" "deploy $SHA" "$B_BAD_ACTOR"

B_BAD_CHECKSUM="$WORKDIR/bad_checksum.tar"; build_bundle checksum-invalid "$B_BAD_CHECKSUM" "$SHA"
assert_rejected "checksum inválido" "$SHA" "deploy $SHA" "$B_BAD_CHECKSUM"

# --- SWARM_ROOT inválido ----------------------------------------------------
if SWARM_ROOT="/" SSH_ORIGINAL_COMMAND="deploy $SHA" PATH="$FAKE_BIN:$PATH" \
        bash "$ENTRYPOINT" < "$B_VALID" > "$WORKDIR/o1" 2>&1; then
    fail "SWARM_ROOT=/ (se esperaba rechazo)"
else
    pass "SWARM_ROOT=/"
fi

if SWARM_ROOT="relative/path" SSH_ORIGINAL_COMMAND="deploy $SHA" PATH="$FAKE_BIN:$PATH" \
        bash "$ENTRYPOINT" < "$B_VALID" > "$WORKDIR/o2" 2>&1; then
    fail "SWARM_ROOT relativo (se esperaba rechazo)"
else
    pass "SWARM_ROOT relativo"
fi

# --- camino feliz + redeploy idéntico + integridad -------------------------
ROOT1="$(assert_accepted "bundle válido, primer deploy" "$SHA" "deploy $SHA" "$B_VALID")"

if [ -n "$ROOT1" ]; then
    if FAKE_DOCKER_LABEL_SHA="$SHA" SWARM_ROOT="$ROOT1" SSH_ORIGINAL_COMMAND="deploy $SHA" \
            PATH="$FAKE_BIN:$PATH" bash "$ENTRYPOINT" < "$B_VALID" > "$WORKDIR/redeploy_out" 2> "$WORKDIR/redeploy_err"; then
        pass "redeploy idéntico del mismo SHA (idempotente)"
    else
        cat "$WORKDIR/redeploy_err" >&2
        fail "redeploy idéntico del mismo SHA (se esperaba aceptación)"
    fi

    B_DIFFERENT="$WORKDIR/different.tar"
    build_bundle different-content "$B_DIFFERENT" "$SHA"
    if FAKE_DOCKER_LABEL_SHA="$SHA" SWARM_ROOT="$ROOT1" SSH_ORIGINAL_COMMAND="deploy $SHA" \
            PATH="$FAKE_BIN:$PATH" bash "$ENTRYPOINT" < "$B_DIFFERENT" > "$WORKDIR/mismatch_out" 2> "$WORKDIR/mismatch_err"; then
        fail "release existente con contenido distinto para el mismo SHA (se esperaba rechazo)"
    else
        if grep -qi "inconsistencia de integridad" "$WORKDIR/mismatch_err"; then
            pass "release existente con contenido distinto para el mismo SHA (rechazado con diagnóstico correcto)"
        else
            fail "release existente con contenido distinto (rechazado, pero sin el mensaje de inconsistencia esperado)"
        fi
    fi
fi

echo
echo "=== Resultado: $PASS_COUNT pasaron, $FAIL_COUNT fallaron ==="
if [ "$FAIL_COUNT" -gt 0 ]; then
    printf 'Fallos:\n'
    printf ' - %s\n' "${FAILURES[@]}"
    exit 1
fi
exit 0
