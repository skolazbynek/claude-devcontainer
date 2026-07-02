#!/bin/bash
source /workspace/container-init.sh
source /workspace/vcs-lib.sh

copy_host_configs
ensure_own_mailbox
MAILBOX_OK=$?

BOOKMARK="${SESSION_NAME:?SESSION_NAME must be set}"

if [ -z "${WORKSPACE_PREINITIALIZED:-}" ]; then
    echo "Error: Devcontainer requires the host to pre-create the workspace (WORKSPACE_PREINITIALIZED=1)" >&2
    exit 1
fi

if [ -z "${AGENT_ANCHOR_HASH:-}" ]; then
    echo "Error: AGENT_ANCHOR_HASH must be set" >&2
    exit 1
fi

detect_vcs || exit 1

echo "Using $VCS_TYPE repository at: $WORKSPACE_ORIGIN"
echo "Anchor: ${AGENT_ANCHOR_HASH:0:12}"

cd "$WORKSPACE_CURRENT"

build_claude_config

link_workspace_files

if command -v poetry &>/dev/null; then
    while IFS= read -r pyproject; do
        project_dir=$(dirname "$pyproject")
        echo "[INFO] Installing poetry packages in $project_dir"
        (cd "$project_dir" && poetry install --no-interaction -q >/dev/null 2>&1) || \
            echo "[WARN] poetry install failed in $project_dir (continuing)"
    done < <(find "$WORKSPACE_CURRENT" -maxdepth 3 -name pyproject.toml \
        -not -path '*/.*' -not -path '*/node_modules/*' -not -path '*/.venv/*' 2>/dev/null)
fi

CLAUDE_BIN=$(which claude)
CLAUDE_EXTRA_ARGS="--dangerously-skip-permissions"
if [ -n "${AGENT_MODEL:-}" ]; then
    CLAUDE_EXTRA_ARGS="$CLAUDE_EXTRA_ARGS --model $AGENT_MODEL"
fi
printf '#!/bin/bash\nexec %s %s "$@"\n' "$CLAUDE_BIN" "$CLAUDE_EXTRA_ARGS" > /tmp/bin/claude
chmod +x /tmp/bin/claude

TASK_FILE_MOUNT="/config/task.md"
COMPOSED_PROMPT=""
if [ -n "$AGENT_INLINE_PROMPT" ] && [ -f "$TASK_FILE_MOUNT" ]; then
    COMPOSED_PROMPT="$(cat "$TASK_FILE_MOUNT")"$'\n\n## Additional Instructions\n\n'"$AGENT_INLINE_PROMPT"
elif [ -n "$AGENT_INLINE_PROMPT" ]; then
    COMPOSED_PROMPT="$AGENT_INLINE_PROMPT"
elif [ -f "$TASK_FILE_MOUNT" ]; then
    COMPOSED_PROMPT="$(cat "$TASK_FILE_MOUNT")"
fi

if [ -n "${MASTER_MODE:-}" ]; then
    # Signal readiness as soon as setup is done, before the optional first-launch
    # prompt, so the host can attach immediately no matter how long the prompt runs.
    # /tmp (not /run, which is root-owned 755) is writable by the non-root container user.
    touch /tmp/cld-master-ready
fi

if [ -n "$COMPOSED_PROMPT" ]; then
    [ -n "${MASTER_MODE:-}" ] && \
        echo "[INFO] Running first-launch prompt; attach anytime with 'cld devcontainer --master'."
    claude -- "$COMPOSED_PROMPT" || true
fi

if [ -n "${MASTER_MODE:-}" ]; then
    # PID 1 idles; user shells arrive via `docker exec` from the host.
    exec sleep infinity
fi

if [ -n "${AGENT_MODE:-}" ]; then
    if [ "$MAILBOX_OK" -ne 0 ]; then
        echo "Error: repo agent cannot start without its mailbox (see error above)" >&2
        exit 1
    fi
    touch /tmp/cld-agent-ready               # host readiness sentinel (/tmp is non-root writable)
    exec python3 -m cld.messenger.agent_loop
fi

/bin/bash

vcs_forget_workspace "$BOOKMARK" "$WORKSPACE_CURRENT"
