#!/bin/bash
# Idempotent per-session workspace teardown, run INSIDE a cld container
# (via `docker exec` from the host / master during shutdown). Removes the
# workspace's VCS registration and its directory + anchor record from the
# origin repo. Safe to re-run; no-op if the workspace is already gone.
#
# Requires SESSION_NAME, WORKSPACE_ORIGIN in the environment (both are set
# by container-init.sh on every cld container).

set -eu
# shellcheck disable=SC1091
source /workspace/container-init.sh
# shellcheck disable=SC1091
source /workspace/vcs-lib.sh

: "${SESSION_NAME:?SESSION_NAME must be set}"
: "${WORKSPACE_ORIGIN:?WORKSPACE_ORIGIN must be set}"

WORKSPACE_PATH="$WORKSPACE_ORIGIN/.cld/workspaces/$SESSION_NAME"
ANCHOR_RECORD="$WORKSPACE_ORIGIN/.cld/anchors/$SESSION_NAME"

detect_vcs >/dev/null 2>&1 || { echo "[cleanup] no VCS detected; skipping"; exit 0; }

echo "[cleanup] forget workspace: $SESSION_NAME"
vcs_forget_workspace "$SESSION_NAME" "$WORKSPACE_PATH" 2>&1 || true

if [ -d "$WORKSPACE_PATH" ]; then
    rm -rf "$WORKSPACE_PATH"
    echo "[cleanup] removed $WORKSPACE_PATH"
fi

if [ -f "$ANCHOR_RECORD" ]; then
    rm -f "$ANCHOR_RECORD"
    echo "[cleanup] removed $ANCHOR_RECORD"
fi
