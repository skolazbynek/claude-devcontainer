#!/bin/bash
source /workspace/container-init.sh
source /workspace/vcs-lib.sh

copy_host_configs
ensure_own_mailbox
MAILBOX_OK=$?

BOOKMARK="${SESSION_NAME:?SESSION_NAME must be set}"

cd "$WORKSPACE_ORIGIN"

# Workspace lives inside the container's ephemeral filesystem at
# /workspace/current. jj stores everything into the origin's .jj/repo/store via
# the RW bind mount at $WORKSPACE_ORIGIN, so bookmarks and (watchman-driven)
# snapshots persist across `docker rm && docker run` even though the workspace
# directory itself does not.
#
# Invariant: bookmark $BOOKMARK exists in the origin store <=> a live or
# restart-paused lifecycle owns this session. `cld <role> restart` preserves
# the bookmark (reattach at its tip). `cld <role> shutdown` forgets it so the
# next launch is a fresh lifecycle honoring -r.
# Forget any workspace already registered under $BOOKMARK before adding. A
# prior `cld <role> shutdown` forgets the bookmark but NOT the workspace, so
# on a fresh first-launch the stale registration would make `jj workspace add
# --name` fail with "Workspace named X already exists" -- silently, since
# there's no set -e -- and leave /workspace/current an empty dir. No-op when
# absent (first-ever launch).
jj workspace forget "$BOOKMARK" 2>&1 || true

if jj bookmark list -T 'name ++ "\n"' | grep -qx "$BOOKMARK"; then
    echo "[cld] reattaching workspace '$BOOKMARK'"
    if ! jj workspace add --name "$BOOKMARK" -r "$BOOKMARK" /workspace/current; then
        echo "Error: jj workspace add failed (reattach)" >&2
        exit 1
    fi
    # Recover the anchor: the ancestor of the bookmark with our own
    # 'cld anchor: <session>' description is B.
    AGENT_ANCHOR_HASH=$(jj log --no-graph -n 1 \
        -r "heads(ancestors(${BOOKMARK}) & description(exact:'cld anchor: ${SESSION_NAME}'))" \
        -T commit_id 2>/dev/null || true)
    export AGENT_ANCHOR_HASH
else
    # First launch. Base revision comes from AGENT_REVISION_HINT (a resolved
    # hash from the host, or an unresolved revset when a `cld master`
    # delegated to this peer; see docs/design-master-sibling-launch.md).
    # The anchor commit B is staged INSIDE /workspace/current by
    # `python3 -m cld.vcs.scratch`, so the origin working copy is never touched
    # -- crucial for the common jj case where the user's @ is A itself.
    if [ -z "${AGENT_SCRATCH:-}" ]; then
        echo "Error: AGENT_SCRATCH is required on first launch" >&2
        exit 1
    fi
    BASE_REV="${AGENT_REVISION_HINT:-@}"
    if ! A_HASH=$(jj log --no-graph -n 1 -r "$BASE_REV" -T commit_id 2>/dev/null); then
        echo "Error: could not resolve AGENT_REVISION_HINT='$BASE_REV'" >&2
        exit 1
    fi
    echo "[cld] first launch, base=${A_HASH:0:12}"
    if ! jj workspace add --name "$BOOKMARK" -r "$A_HASH" /workspace/current; then
        echo "Error: jj workspace add failed (first launch)" >&2
        exit 1
    fi
    if ! AGENT_ANCHOR_HASH=$(cd /workspace/current && python3 -m cld.vcs.scratch); then
        echo "Error: peer-side anchor staging failed" >&2
        exit 1
    fi
    export AGENT_ANCHOR_HASH
    echo "[cld] anchor=${AGENT_ANCHOR_HASH:0:12}"
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
# --add-dir /opt/cld surfaces the baked-in .claude/skills/ (agent-start,
# messenger-*) regardless of which repo is mounted at /workspace/origin;
# settings.json's permissions.additionalDirectories grants file access only
# and does not trigger skill auto-loading, so this must be a CLI flag.
CLAUDE_EXTRA_ARGS="--dangerously-skip-permissions --add-dir /opt/cld"
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
    # Materialize registered sibling targets as empty placeholder directories
    # so `cd <target>` inside master's shell succeeds. No bind mount of the
    # sibling repo exists in master; cld-inside-master resolves cwd to the
    # host path via config lookup. See docs/design-master-sibling-launch.md.
    if [ -n "${MASTER_TARGETS:-}" ]; then
        IFS=':' read -r -a _cld_targets <<< "$MASTER_TARGETS"
        for t in "${_cld_targets[@]}"; do
            [ -n "$t" ] || continue
            mkdir -p "$t" 2>/dev/null || echo "[WARN] could not create placeholder $t" >&2
        done
        unset _cld_targets
    fi
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
    # Trap SIGTERM (docker stop) to forget the session bookmark from the
    # origin's jj store before exit. This is the "peer self-cleanup" leg of
    # docs/design-master-sibling-launch.md's shutdown mechanism -- master
    # containers own their bookmark's full lifecycle.
    _cld_master_shutdown() {
        (cd "$WORKSPACE_ORIGIN" && jj bookmark forget "$SESSION_NAME" 2>&1) || true
        exit 0
    }
    trap _cld_master_shutdown TERM INT
    sleep infinity &
    wait $!
    exit 0
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
