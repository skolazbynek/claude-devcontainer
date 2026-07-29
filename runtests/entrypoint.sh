#!/usr/bin/env bash
# runtests entrypoint: materialize a jj revision in an isolated workspace and
# run pytest against it. All args are passed straight through to pytest.
#
#   REVISION            revset to test (default: @)
#   PROJECT_SUBDIR      dir under the workspace holding pyproject.toml (default: .)
#   SECRETS_FILE        env file sourced into pytest's environment (default: /secrets/.env)
#   POETRY_INSTALL_ARGS flags for `poetry install` (default: --all-extras --all-groups,
#                       so test deps get installed however they're declared -- poetry
#                       groups or PEP 621 extras). Override e.g. for --no-root.
#   PYTEST_ADDOPTS      default pytest opts (default: short tracebacks, quiet, no
#                       warnings, stop after 30 failures -- keeps output small).
#                       Explicit args passed to the container still override these.
#   OUTPUT_MAX_BYTES    cap on returned output; only the last N bytes (which hold
#                       pytest's summary) are emitted, with a truncation notice.
#
# The repo store is bind-mounted at /repo; the isolated working copy is created
# under $HOME via `jj workspace add`, which never touches the store's default
# workspace @ or bookmarks.
set -euo pipefail

: "${REVISION:=@}"
: "${PROJECT_SUBDIR:=.}"
: "${SECRETS_FILE:=/secrets/.env}"
: "${POETRY_INSTALL_ARGS:=--all-extras --all-groups}"
# Bound pytest output by default. PYTEST_ADDOPTS is combined with the project's own
# config and the args passed to the container, with the latter winning -- so a caller
# can still ask for --tb=long or a different --maxfail.
: "${PYTEST_ADDOPTS:=--tb=short --disable-warnings -q --maxfail=30}"
export PYTEST_ADDOPTS
: "${OUTPUT_MAX_BYTES:=65536}"
export HOME="${HOME:-/tmp}"
# The host uid passed via --user has no /etc/passwd entry in this image, so
# any getpwuid() lookup (Python's getpass.getuser(), some git paths, etc.)
# would fail; USER/LOGNAME are what those tools check first, ahead of NSS.
export USER="${USER:-runtests}"
export LOGNAME="${LOGNAME:-$USER}"

# jj needs an author identity to create the workspace working-copy commit.
export JJ_CONFIG=/tmp/jj-config.toml
printf '[user]\nname = "runtests"\nemail = "runtests@localhost"\n' > "$JJ_CONFIG"

cd /repo
# Container hostname is a unique per-container id; keeps the workspace name
# collision-free across concurrent runs without a SIGPIPE-prone random pipe.
ws="runtests-${HOSTNAME:-$$}"
# Workspace dir lives under $HOME: with `--user` (the broker path always uses
# it) the container user cannot mkdir at / (root-owned), but $HOME is writable.
work="$HOME/rt-workspace"
trap 'jj workspace forget "$ws" >/dev/null 2>&1 || true' EXIT

jj workspace add --name "$ws" -r "$REVISION" "$work"
cd "$work/$PROJECT_SUBDIR"

if [ -f "$SECRETS_FILE" ]; then
    set -a; . "$SECRETS_FILE"; set +a
else
    echo "[runtests] WARN: secrets file $SECRETS_FILE not found; running without it" >&2
fi

# Buffer install + pytest into one log so we can cap what the caller sees. Markers
# go to stderr live; the log holds the deliverable. Install failure skips pytest.
log=/tmp/runtests.out
rc=0
echo "[runtests] installing dependencies (poetry install $POETRY_INSTALL_ARGS)..." >&2
# shellcheck disable=SC2086  -- intentional word-splitting into separate flags
poetry install --no-interaction $POETRY_INSTALL_ARGS >"$log" 2>&1 || rc=$?
if [ "$rc" -eq 0 ]; then
    echo "[runtests] running: pytest ${*:-<all tests>}  (PYTEST_ADDOPTS=$PYTEST_ADDOPTS)" >&2
    poetry run pytest "$@" >>"$log" 2>&1 || rc=$?
fi
# Return only the last OUTPUT_MAX_BYTES -- pytest's summary (and any install error)
# is at the end -- so a huge failing run can't flood the caller's context.
total=$(wc -c <"$log")
if [ "$total" -gt "$OUTPUT_MAX_BYTES" ]; then
    echo "[runtests] output truncated to last $OUTPUT_MAX_BYTES of $total bytes (raise OUTPUT_MAX_BYTES for more)" >&2
    tail -c "$OUTPUT_MAX_BYTES" "$log"
else
    cat "$log"
fi
exit "$rc"
