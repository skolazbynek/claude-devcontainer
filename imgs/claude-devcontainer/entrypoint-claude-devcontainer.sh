#!/bin/bash
source /workspace/container-init.sh
source /workspace/vcs-lib.sh

copy_host_configs
ensure_own_mailbox
MAILBOX_OK=$?

BOOKMARK="${SESSION_NAME:?SESSION_NAME must be set}"

# Ticket container (v2): per-repo boot loop over the launch manifest, then its
# own after-loop steps and PID-1 idle. It never reaches the v1 single-repo
# flow below (docs/design-ticket-containers.md section 4).
if [ -n "${TICKET_MODE:-}" ]; then
    : "${CLD_TICKET_MANIFEST:?CLD_TICKET_MANIFEST must be set in TICKET_MODE}"
    if ! command -v jq &>/dev/null; then
        echo "Error: TICKET_MODE requires jq in the image" >&2
        exit 1
    fi
    TICKET=$(printf '%s' "$CLD_TICKET_MANIFEST" | jq -r '.ticket')
    TICKET_ROOT="/workspace/${TICKET}"
    N_REPOS=$(printf '%s' "$CLD_TICKET_MANIFEST" | jq '.repos | length')
    mkdir -p "$TICKET_ROOT"
    TICKET_ROWS=""
    REPO_INDEX=0

    # Workspaces live inside the container's ephemeral filesystem under the
    # ticket root. jj stores everything into each origin's .jj/repo/store via
    # its RW bind mount, so bookmarks and (watchman-driven) snapshots persist
    # across `docker rm && docker run` even though the workspace dirs do not.
    # Per-repo boot failure is fatal for the whole boot (no sentinel): a
    # ticket with a silently missing repo is worse than a failed start.
    while IFS= read -r _repo_json; do
        REPO_INDEX=$((REPO_INDEX + 1))
        REPO_NAME=$(jq -r '.name' <<<"$_repo_json")
        REPO_BASE=$(jq -r '.anchor_base' <<<"$_repo_json")
        REPO_MODE=$(jq -r '.anchor_mode' <<<"$_repo_json")
        REPO_ORIGIN="/workspace/origin/${REPO_NAME}"
        REPO_WORKSPACE="${TICKET_ROOT}/${REPO_NAME}"
        echo "[cld] repo ${REPO_INDEX}/${N_REPOS}: ${REPO_NAME} (base=${REPO_BASE:0:12}, mode=${REPO_MODE})"
        if [ ! -d "$REPO_ORIGIN" ]; then
            echo "Error: repo '${REPO_NAME}': no origin mount at ${REPO_ORIGIN}" >&2
            exit 1
        fi
        REPO_BACKEND=$(cld_detect_backend "$REPO_ORIGIN")
        case "$REPO_BACKEND" in
            jj)
                if ! cld_boot_workspace "$REPO_ORIGIN" "$REPO_WORKSPACE" \
                        "$BOOKMARK" "$REPO_BASE" "$REPO_MODE" default; then
                    echo "Error: repo '${REPO_NAME}': boot failed" >&2
                    exit 1
                fi
                cld_enable_watchman "$REPO_WORKSPACE"
                ;;
            git)
                if ! cld_boot_worktree "$REPO_ORIGIN" "$REPO_WORKSPACE" \
                        "$BOOKMARK" "$REPO_BASE"; then
                    echo "Error: repo '${REPO_NAME}': boot failed" >&2
                    exit 1
                fi
                ;;
            *)
                echo "Error: repo '${REPO_NAME}': no supported VCS at ${REPO_ORIGIN} (expected .jj/ or .git)" >&2
                exit 1
                ;;
        esac
        link_workspace_files "$REPO_NAME" "$REPO_ORIGIN" "$REPO_WORKSPACE"
        # Optional per-repo bootstrap, opted in via the registry `bootstrap`
        # key and resolved host-side into CLD_REPO_BOOTSTRAP (<name>=<pyproject
        # subdir>;...). Replaces v1's unconditional depth-3 poetry scan, which
        # does not scale to N repos.
        BOOTSTRAP_DIR=$(cld_repo_kv_get "${CLD_REPO_BOOTSTRAP:-}" "$REPO_NAME")
        if [ -n "$BOOTSTRAP_DIR" ] && command -v poetry &>/dev/null; then
            echo "[cld] repo '${REPO_NAME}': poetry install in ${BOOTSTRAP_DIR}"
            (cd "$REPO_WORKSPACE/$BOOTSTRAP_DIR" && \
                poetry install --no-interaction -q >/dev/null 2>&1) || \
                echo "[WARN] poetry install failed for ${REPO_NAME} (continuing)"
        fi
        REPO_ANCHOR="${CLD_BOOT_ANCHOR:-$REPO_BASE}"
        TICKET_ROWS="${TICKET_ROWS}| ${REPO_NAME} | ${REPO_ANCHOR:0:12} | ${REPO_MODE} | ${REPO_BACKEND} |
"
    done < <(printf '%s' "$CLD_TICKET_MANIFEST" | jq -c '.repos[]')

    write_ticket_claude_md "$TICKET_ROOT" "$TICKET" "$TICKET_ROWS"
    build_claude_config

    # Claude wrapper: permission-skip and baked skills stay in-container; the
    # model comes per-invocation from `cld claude -- --model ...`, never baked
    # (design 4.3). The flock is the single-session refusal (design 4.5): one
    # live harness session per ticket container (POC); the lock file carries
    # pid + start time so the refusal can name the holder.
    CLAUDE_BIN=$(which claude)
    cat > /tmp/bin/claude <<EOF
#!/bin/bash
exec 9>>/tmp/cld-session.lock
if ! flock -n 9; then
    echo "Error: a claude session is already live in this ticket container:" >&2
    sed 's/^/  /' /tmp/cld-session.lock >&2
    echo "One session per ticket (POC) -- wait for it to end, or use it." >&2
    exit 1
fi
printf 'pid=%s start=%s\n' "\$\$" "\$(date -Is)" > /tmp/cld-session.lock
exec $CLAUDE_BIN --dangerously-skip-permissions --add-dir /opt/cld "\$@"
EOF
    chmod +x /tmp/bin/claude

    # /tmp (not /run, which is root-owned 755) is writable by the non-root
    # container user.
    touch /tmp/cld-ticket-ready
    echo "[cld] ticket '${TICKET}' ready (${N_REPOS} repos)"

    # PID 1 idles; harness sessions arrive via `docker exec` from the host.
    # Unlike v1 master, TERM (docker stop) tears nothing down: stop is the
    # *pause* verb, and all bookmark/workspace forgetting lives host-side in
    # `cld shutdown` (design 4.3).
    trap 'exit 0' TERM INT
    sleep infinity &
    wait $!
    exit 0
fi

cd "$WORKSPACE_ORIGIN"

# v1 single-repo boot: the three-branch workspace logic (warm restart /
# reattach / first launch) lives in cld_boot_workspace (vcs-lib.sh), shared
# with the ticket loop above. Base revision comes from AGENT_REVISION_HINT (a
# resolved hash from the host, or an unresolved revset when a `cld master`
# delegated to this peer; see docs/design-master-sibling-launch.md).
#
# FIRST_LAUNCH records which of the three branches we took. The brief lives in
# scratch commit B (a child of anchor A), so every descendant carries it and
# file presence can no longer tell a first launch from a restart -- only this
# flag can.
if ! cld_boot_workspace "$WORKSPACE_ORIGIN" /workspace/current "$BOOKMARK" \
        "${AGENT_REVISION_HINT:-@}" "${AGENT_ANCHOR_MODE:-isolated}" env; then
    exit 1
fi
FIRST_LAUNCH=$CLD_BOOT_FIRST_LAUNCH
AGENT_ANCHOR_HASH="$CLD_BOOT_ANCHOR"
export AGENT_ANCHOR_HASH

cld_enable_watchman /workspace/current

cd /workspace/current

# Task-agent only: the deliverable branch is a *second*, durable bookmark that
# survives teardown (the session bookmark does not). Created at the anchor so it
# has a base to exist at; from then on only the agent moves it, by squashing its
# work into it on wrap-up. Create-if-absent, so a restart or reattach never
# rewinds a branch that already advanced.
if [ -n "${TASK_AGENT_MODE:-}" ] && [ -n "${AGENT_DELIVERABLE_BRANCH:-}" ]; then
    if [ -z "${AGENT_ANCHOR_HASH:-}" ]; then
        echo "[WARN] no anchor recovered -- skipping deliverable bookmark '$AGENT_DELIVERABLE_BRANCH'" >&2
    elif jj bookmark list -T 'name ++ "\n"' | grep -qx "$AGENT_DELIVERABLE_BRANCH"; then
        echo "[cld] deliverable bookmark '$AGENT_DELIVERABLE_BRANCH' already exists -- leaving it alone"
    elif jj bookmark create "$AGENT_DELIVERABLE_BRANCH" -r "$AGENT_ANCHOR_HASH"; then
        echo "[cld] deliverable bookmark '$AGENT_DELIVERABLE_BRANCH' created at ${AGENT_ANCHOR_HASH:0:12}"
    else
        echo "[WARN] could not create deliverable bookmark '$AGENT_DELIVERABLE_BRANCH'" >&2
    fi
    # Wrap-up may push this branch to the remote; agent containers ship no
    # known_hosts, so a first push would fail host-key verification.
    seed_known_hosts
fi

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

# The launcher composed the prompt refs and -p into one brief and shipped it in the
# anchor scratch, so it is committed in anchor B (docs/design-prompt-chaining.md).
# It therefore stays readable here for the whole session -- which is the point --
# so its presence says nothing about whether it has already been consumed.
BRIEF_FILE="$WORKSPACE_CURRENT/.cld-run/brief.md"
COMPOSED_PROMPT=""
[ -f "$BRIEF_FILE" ] && COMPOSED_PROMPT="$(cat "$BRIEF_FILE")"

# Materialize registered sibling targets as empty placeholder directories so
# `cd <target>` inside the shell succeeds. This runs for both `cld master` and
# the bare ephemeral devcontainer (HUB_MODE, not MASTER_MODE -- see
# in_master_container() in cld/docker.py) -- neither gets a bind mount of the
# sibling repo; cld-inside-the-container resolves cwd to the host path via
# config lookup. See docs/design-master-sibling-launch.md.
if [ -n "${HUB_MODE:-}" ] && [ -n "${MASTER_TARGETS:-}" ]; then
    IFS=':' read -r -a _cld_targets <<< "$MASTER_TARGETS"
    for t in "${_cld_targets[@]}"; do
        [ -n "$t" ] || continue
        # $t is a host path (e.g. /home/<user>/projects/x). The unprivileged
        # container user can only create under its own $HOME, so mirror the
        # target by swapping the host-home prefix ($CLD_HOST_HOME) for $HOME.
        # build_container_args guarantees every target lives under host home.
        _mirror="$t"
        case "$t" in
            "${CLD_HOST_HOME:-/nonexistent}"/*) _mirror="$HOME/${t#"${CLD_HOST_HOME}"/}";;
        esac
        mkdir -p "$_mirror" 2>/dev/null || echo "[WARN] could not create placeholder $_mirror (target $t)" >&2
    done
    unset _cld_targets _mirror
fi

if [ -n "${MASTER_MODE:-}" ]; then
    # Signal readiness as soon as setup is done, before the optional first-launch
    # prompt, so the host can attach immediately no matter how long the prompt runs.
    # /tmp (not /run, which is root-owned 755) is writable by the non-root container user.
    touch /tmp/cld-master-ready
fi

# A task-agent's task belongs to the supervisor's composed kickoff prompt (see
# docs/design-task-agents.md §11), so it must NOT be consumed by a one-shot
# pre-run here -- the supervisor reads the same inputs itself.
#
# Gated on FIRST_LAUNCH: a warm restart or bookmark reattach lands on a
# workspace descending from B, so a presence-only check would re-run the user's
# original prompt unattended on top of finished work, once per restart. The
# host agrees -- it passes an empty brief and refuses -p on re-attach.
if [ -n "$COMPOSED_PROMPT" ] && [ "$FIRST_LAUNCH" = 1 ] && [ -z "${TASK_AGENT_MODE:-}" ]; then
    [ -n "${MASTER_MODE:-}" ] && \
        echo "[INFO] Running first-launch prompt; attach anytime with 'cld master'."
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
    # SIGUSR1 (docker kill --signal from `cld master restart`) exits without
    # forgetting the bookmark, so the fresh container reattaches at its tip.
    _cld_master_restart() {
        exit 0
    }
    trap _cld_master_shutdown TERM INT
    trap _cld_master_restart USR1
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

# Bare ephemeral devcontainer: unlike master/agent there is no restart or
# reattach concept for this mode, so nothing else will ever forget this
# session's bookmark or workspace registration out of the origin's jj store.
# Without this, every exited session (each with its own random session name,
# see build_session_name) would leave a permanently orphaned bookmark and a
# stale `jj workspace` entry behind even though the container itself is --rm.
_cld_bare_cleanup() {
    (cd "$WORKSPACE_ORIGIN" && jj bookmark forget "$SESSION_NAME" 2>&1) || true
    (cd "$WORKSPACE_ORIGIN" && jj workspace forget "$SESSION_NAME" 2>&1) || true
}
trap _cld_bare_cleanup EXIT

/bin/bash
