#!/usr/bin/env bash
#
# Portable mypy invocation for the local pre-commit hook — works from the
# main checkout, any git worktree, or this repo cloned/checked out under
# any other name or location.
#
# Why this exists: mypy infers each scanned file's fully-qualified module
# name from the *physical* directory it's run from (this repo's root has an
# __init__.py — the app self-imports as `swarm_trading.*`, see ADR-0002), so
# it silently requires the checkout's own directory to be literally named
# `swarm_trading`. Any other name (e.g. a git worktree named after a branch)
# makes mypy fail outright with "X is not a valid Python package name" —
# and `os.getcwd()` (unlike a shell's `pwd` without `-P`) always resolves
# the *physical* path, so simply `cd`-ing into a symlink named
# `swarm_trading` does not fool it either.
#
# CI sidesteps this by checking out under `path: swarm_trading`
# (.github/workflows/ci.yml). This script reproduces the same trick for any
# local checkout name, without copying a single file: it builds a temp
# directory that is *really* named `swarm_trading` (so os.getcwd() resolves
# correctly), one level deep, whose immediate children are symlinks back to
# this checkout's own top-level entries — then runs mypy from inside it.
#
# Validate any change here with scripts/test_precommit_worktree_portability.sh.
set -euo pipefail

die() {
    echo "run_mypy_precommit.sh: error: $*" >&2
    exit 1
}

command -v git >/dev/null 2>&1 || die "git not found on PATH"

git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || die "not inside a git repository — run this from within a swarm_trading checkout"

# ROOT: this checkout's own working tree — correct regardless of its name,
# whether it's the main checkout or any worktree of it.
ROOT="$(git rev-parse --show-toplevel)"

# MAIN_ROOT: the checkout that owns the shared .git directory, i.e. where
# the project's .venv actually lives — a venv is never per-worktree.
# --git-common-dir resolves to the same real .git for every worktree of
# this repo, regardless of which one we're invoked from.
common_dir="$(git rev-parse --git-common-dir)"
MAIN_ROOT="$(cd "$(dirname "$common_dir")" && pwd)"

[ -d "$MAIN_ROOT/.git" ] \
    || die "resolved MAIN_ROOT ($MAIN_ROOT) has no .git directory — this checkout doesn't look like a normal clone/worktree of this repo"

MYPY_BIN="$MAIN_ROOT/.venv/bin/mypy"
[ -x "$MYPY_BIN" ] \
    || die "mypy not found at $MYPY_BIN — set up the venv in the main checkout first: python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt"

CONFIG_FILE="$ROOT/pyproject.toml"
[ -f "$CONFIG_FILE" ] \
    || die "pyproject.toml not found at $CONFIG_FILE"

# Entries to skip when mirroring $ROOT's top level below: VCS internals, any
# venv that shouldn't exist per-worktree but might stray in, tool caches,
# and (defensively) anything already literally named `swarm_trading`, so a
# checkout that happens to contain one at its top level can never produce a
# self-referential symlink.
SKIP_NAMES=(".git" ".venv" "__pycache__" ".mypy_cache" ".pytest_cache" ".ruff_cache" "swarm_trading")

should_skip() {
    local name="$1" skip
    for skip in "${SKIP_NAMES[@]}"; do
        [ "$name" = "$skip" ] && return 0
    done
    return 1
}

TMP_DIR="$(mktemp -d)"

# Runs on normal exit, any error (set -e), and on INT/TERM — the temp
# directory must never survive this script under any exit path.
cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

# Guard against $TMP_DIR landing inside $ROOT (e.g. a repo-local TMPDIR
# override) — mirroring $ROOT into a symlink tree *inside* $ROOT would make
# mypy walk into its own mirror recursively.
case "$TMP_DIR" in
    "$ROOT"/*) die "temp directory ($TMP_DIR) is inside the repo root ($ROOT) — refusing to avoid infinite recursion; check \$TMPDIR" ;;
esac

PACKAGE_DIR="$TMP_DIR/swarm_trading"
mkdir "$PACKAGE_DIR"

# Shallow, one-level-deep mirror: symlink each top-level entry of $ROOT
# individually, instead of copying file contents or symlinking $ROOT itself.
# This is what makes `cd "$PACKAGE_DIR" && mypy .` see a *real* directory
# named `swarm_trading` (only its children are symlinks) without copying
# any part of the actual source tree.
shopt -s dotglob nullglob
for entry in "$ROOT"/*; do
    name="$(basename -- "$entry")"
    should_skip "$name" && continue
    ln -s -- "$entry" "$PACKAGE_DIR/$name"
done
shopt -u dotglob nullglob

cd -- "$PACKAGE_DIR"
"$MYPY_BIN" --config-file "$CONFIG_FILE" .
