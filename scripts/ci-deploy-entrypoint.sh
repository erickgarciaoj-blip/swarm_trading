#!/usr/bin/env bash
#
# Frontera fija de seguridad para el deploy de staging (Fase 4.6, ADR-0011).
# Instalado una sola vez durante el bootstrap del VPS, referenciado desde
# `command=` en authorized_keys de swarm-deploy — NUNCA se actualiza desde
# un release. Es el único componente de este pipeline que se confía sin
# haber sido validado por sí mismo primero.
#
# Invocación real (desde GitHub Actions, vía command= forzado):
#   tar -cf - <allowlist> | ssh swarm-deploy@host   (SSH_ORIGINAL_COMMAND="deploy <sha>")
#
# Contrato de entrada:
#   - $SSH_ORIGINAL_COMMAND: exactamente "deploy <40 hex lowercase>", nada más.
#   - stdin: un bundle tar con la allowlist exacta (ver ALLOWED_FILES abajo),
#     incluyendo un MANIFEST de 3 líneas (SHA=, ACTOR=, BUNDLE_SHA256=).
#
# No confía en ninguna de las dos entradas sin validar. Nunca usa eval ni
# interpola datos no confiables dentro de una cadena ejecutada.
#
# shared/.env: un único archivo persistente en $SWARM_ROOT/shared/.env
# (creado a mano una vez, ver runbook paso 16) es la única fuente de
# secretos de runtime — el bundle JAMÁS lo transporta (no está en
# ALLOWED_FILES; ningún tar con un ".env" pasa la allowlist). Este script
# valida shared/.env de forma independiente y crea releases/<sha>/.env como
# symlink relativo hacia él, del lado del VPS, después de extraer el
# release y antes de tocar backup/migración/contenedores — ver paso 4 más
# abajo.
set -euo pipefail

COMPONENT="entrypoint"

# --- SWARM_ROOT: mismo contrato de validación en los tres scripts de esta
# fase (deploy.sh, rollback.sh, este). Duplicado a propósito en vez de una
# librería compartida — ver ADR-0011 y la discusión de diseño del PR 2: se
# prefiere explícito y autocontenido sobre una abstracción compartida.
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
# En producción esta variable NUNCA debe configurarse — el default fijo
# (/opt/swarm-trading) es el único valor real. Solo existe para que las
# pruebas locales corran contra un directorio temporal aislado, sin tocar
# el filesystem real ni requerir privilegios de swarm-deploy.

RELEASES_DIR="$SWARM_ROOT/releases"
LOG_FILE="$SWARM_ROOT/logs/deploy.log"
IMAGE_REPO="ghcr.io/erickgarciaoj-blip/swarm_trading"

# Límite de tamaño del bundle: constante interna, no configurable por
# entorno en producción (evita que una variable mal seteada abra la puerta
# a un bundle arbitrariamente grande). El bundle real son 5 archivos
# pequeños (dos YAML, dos scripts, un manifest) — unos pocos KB.
readonly MAX_BUNDLE_BYTES=$((2 * 1024 * 1024))

log() {
    mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
    printf '%s|actor=%s|sha=%s|component=%s|%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${ACTOR:-unknown}" "${SHA:-none}" "$COMPONENT" "$*" \
        >> "$LOG_FILE" 2>/dev/null || true
}

reject() {
    log "REJECTED: $*"
    echo "$COMPONENT: rechazado — $*" >&2
    exit 1
}

TMP_BUNDLE=""
cleanup() {
    [ -n "$TMP_BUNDLE" ] && rm -f "$TMP_BUNDLE"
}
trap cleanup EXIT INT TERM

# --- 1. Validar el comando exacto ------------------------------------------
# Un solo verbo, un espacio, un SHA completo en hex minúscula. Cualquier
# otra cosa (vacío, verbo distinto, SHA corto/mayúsculas, argumentos extra,
# caracteres de shell) se rechaza sin ejecutar nada más.
CMD="${SSH_ORIGINAL_COMMAND:-}"
if [[ ! "$CMD" =~ ^deploy\ ([0-9a-f]{40})$ ]]; then
    reject "comando SSH inválido (se esperaba exactamente 'deploy <40 hex>')"
fi
SHA="${BASH_REMATCH[1]}"

# --- 2. Leer el bundle con límite de tamaño real, no post-hoc --------------
# head -c N+1: si el resultado tiene N+1 bytes, stdin excedía el límite —
# se detecta sin haber escrito más que ese exceso a disco.
TMP_BUNDLE="$(mktemp)"
head -c "$((MAX_BUNDLE_BYTES + 1))" > "$TMP_BUNDLE"
BUNDLE_SIZE="$(wc -c < "$TMP_BUNDLE" | tr -d '[:space:]')"
if [ "$BUNDLE_SIZE" -eq 0 ]; then
    reject "bundle vacío"
fi
if [ "$BUNDLE_SIZE" -gt "$MAX_BUNDLE_BYTES" ]; then
    reject "bundle excede el límite de tamaño ($MAX_BUNDLE_BYTES bytes)"
fi

# --- 3. Inspección estructural + manifest + checksum + extracción atómica -
# Delegado a python3 (tarfile de la librería estándar) en vez de parsear la
# salida de `tar -tvf` por columnas — parsear texto de tar es frágil ante
# nombres con espacios/caracteres raros; tarfile da acceso estructurado real
# (tipo de entrada, permisos, contenido) sin ambigüedad. El heredoc está
# citado ('PYEOF') para que bash nunca interpole nada dentro del código
# Python — todas las entradas no confiables viajan por variables de entorno
# que el propio Python lee, nunca por sustitución de texto.
RESULT="$(
    SWARM_BUNDLE_PATH="$TMP_BUNDLE" \
    SWARM_EXPECTED_SHA="$SHA" \
    SWARM_RELEASES_DIR="$RELEASES_DIR" \
    python3 - <<'PYEOF'
import hashlib
import os
import shutil
import sys
import tarfile
import tempfile

ALLOWED_FILES = {
    "docker-compose.yml",
    "docker-compose.staging.yml",
    "scripts/deploy.sh",
    "scripts/rollback.sh",
    "MANIFEST",
}
ALLOWED_DIRS = {"scripts"}
EXECUTABLE_ALLOWED = {"scripts/deploy.sh", "scripts/rollback.sh"}
PAYLOAD_ORDER = [
    "docker-compose.yml",
    "docker-compose.staging.yml",
    "scripts/deploy.sh",
    "scripts/rollback.sh",
]

import re

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ACTOR_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def die(msg: str) -> None:
    sys.stderr.write(f"bundle inspection: {msg}\n")
    sys.exit(1)


def has_control_chars(name: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in name)


def normalize_dir(name: str) -> str:
    return name[:-1] if name.endswith("/") else name


def main() -> None:
    bundle_path = os.environ.get("SWARM_BUNDLE_PATH")
    expected_sha = os.environ.get("SWARM_EXPECTED_SHA", "")
    releases_dir = os.environ.get("SWARM_RELEASES_DIR")
    if not bundle_path or not releases_dir:
        die("variables de entorno requeridas ausentes")
    if not SHA_RE.match(expected_sha):
        die("SWARM_EXPECTED_SHA inválido")

    try:
        tar = tarfile.open(bundle_path, mode="r")
    except tarfile.TarError as exc:
        die(f"tar inválido: {exc}")
        return

    try:
        members = tar.getmembers()

        seen_names: set[str] = set()
        for m in members:
            name = m.name
            if has_control_chars(name):
                die(f"nombre con caracteres de control: {name!r}")
            if name.startswith("/"):
                die(f"ruta absoluta rechazada: {name!r}")
            parts = name.split("/")
            if ".." in parts:
                die(f"'..' rechazado en ruta: {name!r}")
            if m.issym() or m.islnk():
                die(f"symlink/hardlink rechazado: {name!r}")
            if not (m.isreg() or m.isdir()):
                die(f"tipo de entrada no permitido: {name!r}")

            norm = normalize_dir(name) if m.isdir() else name
            if norm in seen_names:
                die(f"entrada duplicada: {name!r}")
            seen_names.add(norm)

            if m.isdir():
                if norm not in ALLOWED_DIRS:
                    die(f"directorio no permitido: {name!r}")
                continue

            if name not in ALLOWED_FILES:
                die(f"archivo fuera de la allowlist: {name!r}")

            is_exec = bool(m.mode & 0o111)
            if name in EXECUTABLE_ALLOWED:
                if not is_exec:
                    die(f"falta bit ejecutable esperado: {name!r}")
            elif is_exec:
                die(f"bit ejecutable inesperado: {name!r}")

        for required in ALLOWED_FILES:
            if required not in seen_names:
                die(f"falta archivo requerido en el bundle: {required}")

        # --- MANIFEST: exactamente 3 líneas, formato fijo ---
        manifest_member = tar.getmember("MANIFEST")
        mf = tar.extractfile(manifest_member)
        if mf is None:
            die("MANIFEST no es un archivo regular legible")
            return
        manifest_raw = mf.read()
        try:
            manifest_text = manifest_raw.decode("ascii")
        except UnicodeDecodeError:
            die("MANIFEST no es ASCII puro")
            return
        lines = manifest_text.split("\n")
        if lines and lines[-1] == "":
            lines = lines[:-1]
        if len(lines) != 3:
            die(f"MANIFEST debe tener exactamente 3 líneas, tiene {len(lines)}")

        if not lines[0].startswith("SHA="):
            die("MANIFEST: primera línea debe ser SHA=")
        manifest_sha = lines[0][len("SHA="):]
        if not SHA_RE.match(manifest_sha):
            die("MANIFEST: SHA inválido")
        if manifest_sha != expected_sha:
            die("MANIFEST: SHA no coincide con el solicitado por el comando SSH")

        if not lines[1].startswith("ACTOR="):
            die("MANIFEST: segunda línea debe ser ACTOR=")
        manifest_actor = lines[1][len("ACTOR="):]
        if not ACTOR_RE.match(manifest_actor):
            die("MANIFEST: ACTOR inválido")

        if not lines[2].startswith("BUNDLE_SHA256="):
            die("MANIFEST: tercera línea debe ser BUNDLE_SHA256=")
        manifest_checksum = lines[2][len("BUNDLE_SHA256="):]
        if not HEX64_RE.match(manifest_checksum):
            die("MANIFEST: BUNDLE_SHA256 inválido")

        # --- Checksum: SHA-256 sobre la concatenación de los 4 archivos de
        # payload, en orden fijo. Nunca incluye el propio MANIFEST (evita la
        # referencia circular de hashear un archivo que contiene su propio
        # hash) ni timestamps u otros datos variables.
        hasher = hashlib.sha256()
        for payload_name in PAYLOAD_ORDER:
            member = tar.getmember(payload_name)
            fh = tar.extractfile(member)
            if fh is None:
                die(f"no se pudo leer {payload_name!r} para checksum")
                return
            hasher.update(fh.read())
        actual_checksum = hasher.hexdigest()
        if actual_checksum != manifest_checksum:
            die("checksum inválido: BUNDLE_SHA256 no coincide con el contenido real")

        # --- Extracción a directorio en cuarentena, nunca directo a
        # releases/<sha>/ — solo se promueve ahí mediante rename atómico
        # tras validar todo, incluido lo que quedó en disco.
        os.makedirs(releases_dir, exist_ok=True)
        incoming = tempfile.mkdtemp(prefix=f".incoming-{expected_sha}-", dir=releases_dir)
        try:
            tar.extractall(path=incoming, members=members)

            for name in ALLOWED_FILES:
                full = os.path.join(incoming, name)
                if not os.path.isfile(full):
                    die(f"extracción incompleta: falta {name!r}")
                mode = os.stat(full).st_mode & 0o777
                is_exec = bool(mode & 0o111)
                if name in EXECUTABLE_ALLOWED and not is_exec:
                    die(f"post-extracción: falta bit ejecutable en {name!r}")
                if name not in EXECUTABLE_ALLOWED and is_exec:
                    die(f"post-extracción: bit ejecutable inesperado en {name!r}")

            try:
                dirfd = os.open(incoming, os.O_RDONLY)
                os.fsync(dirfd)
                os.close(dirfd)
            except OSError:
                pass

            final_dir = os.path.join(releases_dir, expected_sha)
            if os.path.isdir(final_dir):
                existing_hasher = hashlib.sha256()
                for payload_name in PAYLOAD_ORDER:
                    with open(os.path.join(final_dir, payload_name), "rb") as fh2:
                        existing_hasher.update(fh2.read())
                if existing_hasher.hexdigest() == actual_checksum:
                    shutil.rmtree(incoming)
                    incoming = None
                    print("RESULT=IDEMPOTENT")
                    return
                die(
                    "inconsistencia de integridad: ya existe una release para "
                    "este SHA con contenido distinto al del bundle recibido"
                )
                return

            os.rename(incoming, final_dir)
            incoming = None
            print("RESULT=PROMOTED")
        finally:
            if incoming is not None and os.path.isdir(incoming):
                shutil.rmtree(incoming, ignore_errors=True)
    finally:
        tar.close()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - último recurso, nunca dejar un traceback crudo
        die(f"error inesperado durante la validación: {exc}")
PYEOF
)" || reject "validación del bundle falló (ver detalle arriba)"

case "$RESULT" in
    RESULT=PROMOTED) log "bundle validado y extraído a releases/$SHA" ;;
    RESULT=IDEMPOTENT) log "redeploy idéntico del mismo SHA — release existente reutilizada" ;;
    *) reject "resultado inesperado del validador: $RESULT" ;;
esac

# --- 4. shared/.env: validar y enlazar, nunca copiar --------------------
# Corre para PROMOTED e IDEMPOTENT por igual (un redeploy del mismo SHA
# debe dejar el symlink igual de correcto que un release nuevo). Cualquier
# rechazo aquí ocurre antes de exec deploy.sh — o sea, antes de backup,
# migración o cualquier cambio de contenedores.
SHARED_ENV="$SWARM_ROOT/shared/.env"
RELEASE_ENV_LINK="$RELEASES_DIR/$SHA/.env"

[ -e "$SHARED_ENV" ] || reject "shared/.env no existe ($SHARED_ENV) — requerido antes de enlazar la release"
if [ -L "$SHARED_ENV" ]; then
    reject "shared/.env no puede ser en sí mismo un symlink ($SHARED_ENV)"
fi
[ -f "$SHARED_ENV" ] || reject "shared/.env no es un archivo regular ($SHARED_ENV)"

# `stat` no tiene una sintaxis portable entre GNU (Linux, el VPS objetivo:
# Debian/Ubuntu) y BSD (macOS) — se detecta el flavor en vez de asumir uno,
# para que las pruebas también corran igual en un entorno de desarrollo
# macOS.
if stat -c '%a' "$SHARED_ENV" >/dev/null 2>&1; then
    SHARED_ENV_PERMS="$(stat -c '%a' "$SHARED_ENV")"
    SHARED_ENV_OWNER_UID="$(stat -c '%u' "$SHARED_ENV")"
else
    SHARED_ENV_PERMS="$(stat -f '%Lp' "$SHARED_ENV")"
    SHARED_ENV_OWNER_UID="$(stat -f '%u' "$SHARED_ENV")"
fi

# World-readable = el dígito octal de "otros" (el último) tiene el bit 4
# (lectura) encendido — 4, 5, 6 o 7.
OTHER_PERM_DIGIT=$(( 8#$SHARED_ENV_PERMS % 8 ))
if (( OTHER_PERM_DIGIT & 4 )); then
    reject "shared/.env es world-readable (permisos $SHARED_ENV_PERMS) — corrige con chmod antes de reintentar"
fi

if [ "$SHARED_ENV_OWNER_UID" != "$(id -u)" ]; then
    reject "shared/.env no pertenece al usuario que corre este entrypoint (uid $(id -u)), sino a uid $SHARED_ENV_OWNER_UID"
fi

# -f: si releases/<sha>/.env ya existía (redeploy, o un symlink/archivo
# incorrecto dejado por algo externo) se reemplaza sin preguntar — el
# resultado deseado es siempre el mismo symlink correcto, nunca lo que
# hubiera antes ahí.
ln -sfn ../../shared/.env "$RELEASE_ENV_LINK"
LINK_TARGET="$(readlink "$RELEASE_ENV_LINK")"
if [ "$LINK_TARGET" != "../../shared/.env" ]; then
    reject "releases/$SHA/.env no quedó apuntando exactamente a ../../shared/.env (apunta a '$LINK_TARGET')"
fi

log "shared/.env validado (permisos=$SHARED_ENV_PERMS owner_uid=$SHARED_ENV_OWNER_UID) y enlazado en releases/$SHA/.env"

# --- 5. Verificar imagen + label OCI, sin red si ya está local -------------
IMAGE="$IMAGE_REPO:sha-$SHA"
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker pull "$IMAGE" || reject "imagen inexistente o inaccesible: $IMAGE"
fi
LABEL_SHA="$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE" 2>/dev/null || true)"
if [ "$LABEL_SHA" != "$SHA" ]; then
    reject "label OCI org.opencontainers.image.revision ('$LABEL_SHA') no coincide con el SHA solicitado"
fi

# Captura el digest resuelto AHORA (no vuelve a resolverse por tag más
# adelante) y lo deja como artefacto local de la release — deploy.sh lo lee
# y despliega por digest, no por tag mutable, cerrando la ventana entre
# "esta verificación" y "lo que realmente se ejecuta" (ver ADR-0011 y los
# casos de prueba de digest/label).
DIGEST_REF="$(docker inspect --format '{{index .RepoDigests 0}}' "$IMAGE" 2>/dev/null || true)"
if [ -z "$DIGEST_REF" ]; then
    reject "no se pudo resolver el digest de la imagen tras el pull"
fi
printf '%s\n' "$DIGEST_REF" > "$RELEASES_DIR/$SHA/.image-digest"

log "imagen verificada: tag=$IMAGE digest=$DIGEST_REF label_ok=true"

# --- 6. Ejecutar el deploy, sin eval ----------------------------------------
ACTOR_LINE="$(grep '^ACTOR=' "$RELEASES_DIR/$SHA/MANIFEST" | head -1 | cut -d= -f2-)"
exec "$RELEASES_DIR/$SHA/scripts/deploy.sh" "$SHA" "${ACTOR_LINE:-unknown}"
