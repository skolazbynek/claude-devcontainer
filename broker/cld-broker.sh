#!/usr/bin/env bash
# cld host broker -- the sshd ForceCommand target for the restricted broker key.
# sshd ignores whatever command the client asked for and delivers it in
# $SSH_ORIGINAL_COMMAND, which must have the shape:
#
#     <action> <session> <base64-argv>
#
# The broker serves ANY repo that has a running master, agent, or task-agent --
# no per-repo config, no whitelist. It resolves the target repo from the
# calling container's host-set `org.cld.repo-root` label (established at
# launch, not caller input), so the caller controls only: the action, a
# validated session id, and the decoded argv. Nothing is ever eval'd.
#
# Sessions come in three shapes: `cld_master_*` (a `cld master`), `cld_agent_*`
# (both the standing repo agent and task-agents -- kind is a label, not a
# name, see cld/docker.py:task_agent_container_name), and the bare ephemeral
# devcontainer (`cld_<name>`, org.cld.kind=devcontainer -- an ephemeral,
# single-user `cld master`, so it gets the same broker reach). Any of these
# may call `run-tests` / `list-containers`. The `agent` / `task-agent`
# launcher actions (spawning siblings) stay master/devcontainer-only in
# practice even though the session regex admits every kind of caller: they
# gate on the `org.cld.targets` label via validate_target, which only master
# and bare-devcontainer sessions ever carry (set from `master_targets`, see
# build_container_args) -- a repo agent or task-agent session always fails
# validate_target for lack of any registered target.
#
# The regex below is a format check only, not an authorization boundary --
# `$session` doubles as the docker container name, and the label read below
# (set at launch, trusted) is what actually gates repo/target access.
#
# ADD AN ACTION: define a function named `action_<name>` (hyphens in <name>
# become underscores, so `run-tests` -> `action_run_tests`). It receives the
# decoded argv as "$@" and may use the shared context prepared by the
# dispatcher: $session (validated session id) and $REPO (its host repo
# path). Revision/secrets are per-action -- run-tests resolves them via
# `resolve_test_context`; add your own helper if your action needs more.
set -euo pipefail

CONF="${CLD_BROKER_CONF:-/etc/cld/broker.conf}"
# shellcheck source=/dev/null
[ -r "$CONF" ] && . "$CONF"

: "${RUNTESTS_IMAGE:=runtests:latest}"

# Read a single quoted-string key from a flat cld TOML config (e.g. .cld/config.toml).
# Empty if the file or key is absent. Avoids a TOML dependency in the broker.
cld_conf_get() {
    local file="$1" key="$2"
    [ -r "$file" ] || return 0
    sed -n -E "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*\"([^\"]*)\".*/\1/p" "$file" | head -n1
}

# ---- actions --------------------------------------------------------------
# Each runs on the host as your user and receives the decoded pytest/tool argv
# as "$@". `exec` so the child's stdout/stderr/exit code flow straight back.

# run-tests needs the session's current revision + the project's .env; resolve
# those lazily here (not in the shared dispatcher) so read-only actions like
# list-containers don't depend on the session bookmark being resolvable.
resolve_test_context() {
    REV=$(jj -R "$REPO" log --no-graph -n1 -r "$session" -T commit_id) \
        || { echo "cannot resolve revision for $session" >&2; exit 3; }
    PROJECT_SUBDIR=$(cld_conf_get "$REPO/.cld/config.toml" pyproject_dir)
    : "${PROJECT_SUBDIR:=.}"
    case "$PROJECT_SUBDIR" in
        /*) SECRETS_ENV_FILE="$PROJECT_SUBDIR/.env" ;;
        .)  SECRETS_ENV_FILE="$REPO/.env" ;;
        *)  SECRETS_ENV_FILE="$REPO/$PROJECT_SUBDIR/.env" ;;
    esac
}

action_run_tests() {
    resolve_test_context
    local secret_args=()
    [ -f "$SECRETS_ENV_FILE" ] && secret_args=(-v "$SECRETS_ENV_FILE:/secrets/.env:ro")
    exec docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
        -v "$REPO:/repo" "${secret_args[@]}" \
        -e "REVISION=$REV" -e "PROJECT_SUBDIR=$PROJECT_SUBDIR" \
        "$RUNTESTS_IMAGE" "$@"
}

# Enumerate cld containers for the messenger / `cld agent status`. Read-only:
# the sole argv is an optional kind filter (agent|master). Emits one
# tab-separated `name<TAB>kind<TAB>repo<TAB>raw-status` line per container.
action_list_containers() {
    local kind="${1:-}"
    local filter=(--filter label=org.cld.kind)
    [ -n "$kind" ] && filter=(--filter "label=org.cld.kind=$kind")
    docker ps -a "${filter[@]}" --format '{{.Names}}'"$(printf '\t')"'{{.Status}}' \
      | while IFS=$'\t' read -r name status; do
            [ -n "$name" ] || continue
            local labels ckind repo
            labels=$(docker inspect "$name" --format \
                '{{index .Config.Labels "org.cld.kind"}}|{{index .Config.Labels "org.cld.repo-root"}}' \
                2>/dev/null) || continue
            ckind="${labels%%|*}"
            repo="${labels#*|}"
            printf '%s\t%s\t%s\t%s\n' "$name" "$ckind" "$repo" "$status"
        done
}

# Shared by both launcher actions: a container that pushes a deliverable branch
# needs the host user's ssh-agent, and `stage_ssh_agent` (cld/docker.py) forwards
# whatever socket $SSH_AUTH_SOCK names. sshd builds a fresh session environment,
# so that variable only exists here if broker.conf sets it -- and a conf
# assignment is not exported, hence this. Agent *forwarding* over the broker
# connection is not an alternative: that socket dies with the ssh session, while
# the container outlives it. Never fatal, matching stage_ssh_agent: a dead path
# is warned about and the launch proceeds without a key, as it did before.
stage_agent_socket() {
    [ -n "${SSH_AUTH_SOCK:-}" ] || return 0
    if [ -S "$SSH_AUTH_SOCK" ]; then
        export SSH_AUTH_SOCK
    else
        echo "[cld-broker] SSH_AUTH_SOCK=$SSH_AUTH_SOCK is not a live socket --" \
             "launching without agent forwarding" >&2
        unset SSH_AUTH_SOCK
    fi
}

# Shared by both launcher actions: <target> is validated against the master's
# host-set labels (org.cld.repo-root + org.cld.targets), never trusted from the
# caller alone -- so an action can only ever run for a repo the host sanctioned.
validate_target() {
    local target="$1" targets allowed=0 t
    targets=$(docker inspect "$session" --format '{{index .Config.Labels "org.cld.targets"}}' 2>/dev/null) || true
    [ "$target" = "$REPO" ] && allowed=1
    local IFS=:
    for t in $targets; do [ "$target" = "$t" ] && allowed=1; done
    unset IFS
    [ "$allowed" = 1 ] || { echo "denied: target '$target' not registered for $session" >&2; exit 3; }
    { [ -d "$target/.jj" ] || [ -d "$target/.git" ]; } \
        || { echo "denied: target '$target' is not a repo" >&2; exit 3; }
}

# Launch / manage a sibling `cld agent` on the host for one of this master's
# registered repos. The caller (cld inside master) sends <target> <op> [args];
# <op> is checked against a fixed set, so this can never run an arbitrary command.
action_agent() {
    local target="${1:-}" op="${2:-}"
    shift 2 2>/dev/null || { echo "denied: agent needs <target> <op>" >&2; exit 2; }
    case "$op" in
        start|restart|shutdown|status|logs) ;;
        *) echo "denied: bad agent op '$op'" >&2; exit 2 ;;
    esac
    validate_target "$target"
    stage_agent_socket

    # `cld agent` (no subcommand) starts; the rest are subcommands.
    cd "$target" || { echo "cannot cd to $target" >&2; exit 3; }
    if [ "$op" = start ]; then
        exec cld agent "$@"
    else
        exec cld agent "$op" "$@"
    fi
}

# Launch / manage a task-scoped agent for one of this master's registered repos
# (docs/design-task-agents.md §9). Same target validation as `agent`, plus three
# argv rules that make this safe to expose to a container:
#
#   --force   denied outright. Overriding a reap-readiness refusal is a human act;
#             a master must not be able to discard uncaptured work or break a third
#             agent's edge (§7).
#   --parent  denied from the caller and appended by us as the validated $session,
#             so an agent's recorded owner is host-set and cannot be forged.
#   prompts   every positional must be an `@ref`. Refs are resolved host-side and their
#             text composed into the new container's brief, so a bare path would let a
#             container read any host file the user can. The container-side client folds
#             its own local files into `-p` instead (docs/design-prompt-chaining.md §4);
#             cld.prompts contains the escape check for the refs themselves.
action_task_agent() {
    local target="${1:-}" op="${2:-}"
    shift 2 2>/dev/null || { echo "denied: task-agent needs <target> <op>" >&2; exit 2; }
    # Exactly the ops the container route delegates. `transcript` is absent on
    # purpose: the mailbox is bind-mounted into master, so it never needs the host.
    case "$op" in
        start|status|logs|shutdown) ;;
        *) echo "denied: bad task-agent op '$op'" >&2; exit 2 ;;
    esac
    validate_target "$target"
    stage_agent_socket

    # Both spellings of each denial: click accepts `--opt=value`, so matching the bare
    # token alone would let `--parent=<other-master>` through and forge an owner.
    local a
    for a in "$@"; do
        case "$a" in
            --force|--force=*)   echo "denied: --force is host-only" >&2; exit 2 ;;
            --parent|--parent=*) echo "denied: --parent is set by the broker" >&2; exit 2 ;;
        esac
    done

    # start's positionals are prompt refs, and only `@refs` may cross: they name files
    # in the *target repo's* prompts tree, which the host resolves. Option values are
    # still not ref-checked -- task text legitimately contains slashes, so checking them
    # would deny ordinary work like -p "fix cld/cli.py" -- but arity is enumerated, not
    # guessed: only these options of `cld task-agent start` take a *separate* value, so
    # only they swallow the next token. Every other `-*` token (a flag, or the
    # `--opt=value` form, which carries its value inside the token) consumes nothing;
    # assuming it did skipped the positional after it, letting a bare path cross.
    if [ "$op" = start ]; then
        local skip_value=0
        for a in "$@"; do
            if [ "$skip_value" = 1 ]; then skip_value=0; continue; fi
            case "$a" in
                -n|--name|-p|--prompt|--branch|-m|--model|-r|--revision|--peer) skip_value=1 ;;
                -*)  ;;
                @*)  ;;
                *)   echo "denied: prompt ref '$a' must be an @ref, not a path" >&2; exit 2 ;;
            esac
        done
    fi

    cd "$target" || { echo "cannot cd to $target" >&2; exit 3; }
    # --parent is what makes ownership host-set: it stamps the spawn, scopes the roster
    # and gates the reap. `logs` has no such option (a read needs no authority), so
    # appending it there would just make cld reject the argv.
    case "$op" in
        start|status|shutdown) exec cld task-agent "$op" "$@" --parent "$session" ;;
        *)                     exec cld task-agent "$op" "$@" ;;
    esac
}

# Template for a second action -- copy, rename, point at its image, enable by
# uncommenting. Callable from the container as `cld broker lint …`.
# action_lint() {
#     exec docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
#         -v "$REPO:/repo" -e "REVISION=$REV" -e "PROJECT_SUBDIR=$PROJECT_SUBDIR" \
#         "${LINT_IMAGE:-runlint:latest}" "$@"
# }

# ---- dispatch -------------------------------------------------------------
read -r action session payload <<<"${SSH_ORIGINAL_COMMAND:-}"

# Resolve the action to an action_* function. The charset check keeps the
# constructed name to [a-z0-9_], and the function must actually exist.
[[ "$action" =~ ^[a-z][a-z0-9-]*$ ]] || { echo "denied: bad action" >&2; exit 2; }
fn="action_${action//-/_}"
declare -F "$fn" >/dev/null || { echo "denied: unknown action '$action'" >&2; exit 2; }

[[ "$session" =~ ^cld_[A-Za-z0-9_-]+$ ]] || { echo "denied: bad session id" >&2; exit 2; }

# Resolve the target repo from the calling container's host-set label. The
# container name == session, and the label is set at launch (trusted), so the
# caller cannot point at an arbitrary host path -- only a real master's, an
# agent's, or a task-agent's own repo.
REPO=$(docker inspect "$session" --format '{{index .Config.Labels "org.cld.repo-root"}}' 2>/dev/null) || true
[ -n "$REPO" ] && { [ -d "$REPO/.jj" ] || [ -d "$REPO/.git" ]; } \
    || { echo "no master/agent/task-agent/devcontainer container for session $session" >&2; exit 3; }

# Per-action context (REV, secrets, target validation) is resolved inside each
# action_* function now, so read-only actions don't pay for -- or fail on --
# resolution they don't need.

# Arbitrary argv, decoded from base64(NUL-joined argv). Never eval'd, so it can
# only ever become arguments to the action's command.
args=()
[ -n "${payload:-}" ] && mapfile -d '' args < <(printf %s "$payload" | base64 -d)

"$fn" "${args[@]}"
