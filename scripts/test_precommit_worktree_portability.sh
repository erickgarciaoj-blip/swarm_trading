#!/usr/bin/env bash
#
# Validates that the mypy pre-commit hook (scripts/run_mypy_precommit.sh,
# wired in .pre-commit-config.yaml) actually works from a git worktree whose
# name would have broken the old hook — dots, dashes, and a space,
# deliberately, all at once.
#
# Runs the REAL configured hook via `pre-commit run mypy`, not a
# reimplementation of its logic, so this exercises exactly what a
# developer's `git commit` would invoke. Compares the worktree run's result
# against a canonical run from the main checkout — pass requires an
# identical "no issues found in N source files" summary from both.
#
# Creates and always removes its own throwaway worktree + branch, even on
# failure or an interrupting signal.
set -euo pipefail

die() {
    echo "test_precommit_worktree_portability.sh: error: $*" >&2
    exit 1
}

command -v pre-commit >/dev/null 2>&1 \
    || die "pre-commit not found on PATH — activate the project venv first (source .venv/bin/activate)"

MAIN_ROOT="$(git rev-parse --show-toplevel)"
cd -- "$MAIN_ROOT"

TMP_PARENT="$(mktemp -d)"
# Deliberately hostile: dots + dashes (the exact pattern that broke the
# original hook, e.g. a worktree named after a branch like
# "fase-4.5-daily-halt") plus a space, to also prove the quoting throughout
# run_mypy_precommit.sh holds up.
HOSTILE_NAME="wt test.fase-4.5 hostile"
WORKTREE_DIR="$TMP_PARENT/$HOSTILE_NAME"
BRANCH_NAME="_tmp_precommit_portability_check_$$"

cleanup() {
    cd -- "$MAIN_ROOT" 2>/dev/null || cd /
    git worktree remove --force -- "$WORKTREE_DIR" >/dev/null 2>&1 || true
    git branch -D "$BRANCH_NAME" >/dev/null 2>&1 || true
    rm -rf -- "$TMP_PARENT"
}
trap cleanup EXIT INT TERM

extract_summary() {
    # mypy's own summary line, e.g. "Success: no issues found in 83 source files".
    grep -E 'Success: no issues found in [0-9]+ source files' <<<"$1" || true
}

echo "==> Canonical run (main checkout: $MAIN_ROOT)"
canonical_output="$(pre-commit run mypy --all-files --verbose 2>&1)" || {
    echo "$canonical_output" >&2
    die "canonical mypy hook run failed in the main checkout — fix that before testing portability"
}
canonical_summary="$(extract_summary "$canonical_output")"
[ -n "$canonical_summary" ] || die "could not find mypy's summary line in the canonical run's output:
$canonical_output"
echo "    $canonical_summary"

echo "==> Creating hostile-named worktree: $WORKTREE_DIR"
git worktree add -q -b "$BRANCH_NAME" -- "$WORKTREE_DIR" HEAD

echo "==> Hook run from hostile worktree"
cd -- "$WORKTREE_DIR"
worktree_output="$(pre-commit run mypy --all-files --verbose 2>&1)" || {
    echo "$worktree_output" >&2
    die "mypy hook FAILED from worktree '$HOSTILE_NAME' — portability regression"
}
worktree_summary="$(extract_summary "$worktree_output")"
[ -n "$worktree_summary" ] || die "could not find mypy's summary line in the worktree run's output:
$worktree_output"
echo "    $worktree_summary"

[ "$canonical_summary" = "$worktree_summary" ] \
    || die "file count mismatch — canonical: '$canonical_summary' vs worktree: '$worktree_summary'"

echo "==> PASS: identical result from a hostile-named worktree ($worktree_summary)"
