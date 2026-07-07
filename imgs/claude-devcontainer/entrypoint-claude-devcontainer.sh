#!/bin/bash
source /workspace/container-init.sh
source /workspace/vcs-lib.sh

copy_host_configs
ensure_own_mailbox
MAILBOX_OK=$?

BOOKMARK="${SESSION_NAME:?SESSION_NAME must be set}"
ANCHOR="${AGENT_ANCHOR_HASH:?AGENT_ANCHOR_HASH must be set}"

cd "$WORKSPACE_ORIGIN"

# Workspace lives inside the container's ephemeral filesystem at
# /workspace/current. jj stores everything into the origin's .jj/repo/store via
# the RW bind mount at $WORKSPACE_ORIGIN, so bookmarks and (watchman-driven)
# snapshots persist across `docker rm && docker run` even though the workspace
# directory itself does not. If bookmark $BOOKMARK exists this is a restart;
# reattach by pointing a fresh workspace at the bookmark's last tip. Otherwise
# it's a first launch and we anchor at $ANCHOR (the split-produced B commit
# containing .cld-run/*).
if jj bookmark list -T 'name ++ "\n"' | grep -qx "$BOOKMARK"; then
    echo "[cld] reattaching workspace '$BOOKMARK'"
    jj workspace forget "$BOOKMARK" 2>&1 || true
    jj workspace add --name "$BOOKMARK" -r "$BOOKMARK" /workspace/current
else
    echo "[cld] first launch, anchor=${ANCHOR:0:12}"
    jj workspace add --name "$BOOKMARK" -r "$ANCHOR" /workspace/current
    (cd /workspace/current && jj bookmark set "$BOOKMARK" -r @ --allow-backwards)
fi

# Enable watchman auto-snapshot inside the workspace so background file
# changes get snapshotted without a jj command. `register-snapshot-trigger`
# fires under our cap-drop=ALL / no-new-privileges / non-root posture.
(cd /workspace/current && \
    jj config set --workspace fsmonitor.backend watchman && \
    jj config set --workspace fsmonitor.watchman.register-snapshot-trigger true && \
    jj status >/dev/null)

cd /workspace/current

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
    exec python3 -P -m cld.messenger.agent_loop
fi

/bin/bash
