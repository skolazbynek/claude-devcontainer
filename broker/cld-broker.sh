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
: "${GRAPHQL_IMAGE:=graphqlserver:latest}"
# poetry install dominates every graphql start/restart (slow path, no fast
# restart yet -- see docs/impl-graphql-broker-plan.md §11), so the readiness
# budget has to cover it, not just a normal server boot.
: "${GRAPHQL_START_TIMEOUT:=300}"
: "${GRAPHQL_QUERY_TIMEOUT:=30}"
: "${GRAPHQL_OUTPUT_MAX_BYTES:=65536}"
# GRAPHQL_URL_ALLOWLIST is deliberately left unset here (see check_url_allowlisted):
# empty means "no raw URLs at all", and that has to be the default, not a value
# defaulted in code that's easy to lose track of.

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

# Shared by resolve_test_context, resolve_graphql_config, and graphql alias
# lookup: the project's own .env, following pyproject_dir the same way
# PROJECT_SUBDIR does for run-tests -- pyproject.toml and .env are assumed to
# share a directory (docs/design-cld-broker.md §13). Sets $PROJECT_SUBDIR and
# $SECRETS_ENV_FILE. No revision resolution here -- callers that only need the
# secrets path (e.g. a graphql alias lookup for a repo whose graphql_command
# isn't even set, or a git-backed repo where jj would exit 3) must not pay for
# jj resolution they don't need.
resolve_secrets_env_file() {
    PROJECT_SUBDIR=$(cld_conf_get "$REPO/.cld/config.toml" pyproject_dir)
    : "${PROJECT_SUBDIR:=.}"
    case "$PROJECT_SUBDIR" in
        /*) SECRETS_ENV_FILE="$PROJECT_SUBDIR/.env" ;;
        .)  SECRETS_ENV_FILE="$REPO/.env" ;;
        *)  SECRETS_ENV_FILE="$REPO/$PROJECT_SUBDIR/.env" ;;
    esac
}

# run-tests needs the session's current revision + the project's .env; resolve
# those lazily here (not in the shared dispatcher) so read-only actions like
# list-containers don't depend on the session bookmark being resolvable.
#
# REV comes from "${session}@" -- the *workspace* tip, not the `$session`
# bookmark. The bookmark is set once at first container launch
# (imgs/claude-devcontainer/entrypoint-claude-devcontainer.sh) and never moved
# again, so it freezes at the container's first `jj commit` -- the normal state
# once an agent starts working, which left this resolving one change behind
# the code the caller just wrote. Workspace name == bookmark name == container
# name == $session (verified via `jj workspace list`), and `${session}@` is
# the same workspace-scoped-`@` idiom `cld/vcs/detect.py` already uses.
# --ignore-working-copy matters here too: without it, `jj -R "$REPO" log`
# snapshots the *host's* default workspace as a side effect, which would
# violate the broker's store-reading-only posture (docs/design-cld-broker.md
# §9) for no freshness gain -- watchman already wrote the container's own
# snapshot independently.
resolve_test_context() {
    REV=$(jj -R "$REPO" --ignore-working-copy log --no-graph -n1 -r "${session}@" -T commit_id) \
        || { echo "cannot resolve revision for workspace $session" >&2; exit 3; }
    resolve_secrets_env_file
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

# ---- graphql: server lifecycle + credentialed queries over the broker -----
# See docs/impl-graphql-broker-plan.md for the design; this block implements
# §3-§6. One server per session (`cld_gql_<session>`, jj workspace
# `gql-<session>`), started from the `graphqlserver` image (§2) at the
# session's current revision, ops: start/stop/restart/status/logs (server
# lifecycle) and query/introspect/endpoints (client, credentialed by the
# broker so the container never holds a token).

# Per-repo graphql config (§3), soft: does NOT require graphql_command, so
# status/logs/stop/endpoints still work (or report "not configured") for a
# repo that has never been wired up. Only resolve_graphql_context (below,
# used by `start`) hard-requires it -- that's the op about to spend a
# poetry install on it. cld_conf_get parses double-quoted strings only:
# `graphql_port = 8000` silently yields nothing, it must be `= "8000"`.
resolve_graphql_config() {
    GQL_COMMAND=$(cld_conf_get "$REPO/.cld/config.toml" graphql_command)
    GQL_PORT=$(cld_conf_get "$REPO/.cld/config.toml" graphql_port)
    : "${GQL_PORT:=8000}"
    GQL_HEALTH_PATH=$(cld_conf_get "$REPO/.cld/config.toml" graphql_health_path)
    : "${GQL_HEALTH_PATH:=/graphql}"
}

# start's context: config (hard-required -- no guessed default, the old MCP's
# `poetry run python manage.py` default was a lide-api-shaped guess and is not
# being carried over) + the revision + the secrets path.
resolve_graphql_context() {
    REV=$(jj -R "$REPO" --ignore-working-copy log --no-graph -n1 -r "${session}@" -T commit_id) \
        || { echo "cannot resolve revision for workspace $session" >&2; exit 3; }
    resolve_secrets_env_file
    resolve_graphql_config
    [ -n "$GQL_COMMAND" ] || {
        echo "denied: graphql_command is not set in $REPO/.cld/config.toml -- add" \
             "graphql_command = \"...\" (quotes required; cld_conf_get silently" \
             "ignores an unquoted value)" >&2
        exit 3
    }
}

# GRAPHQL_BIND resolved lazily (only `start` publishes a port) and only if the
# operator hasn't pinned one in broker.conf. Bridge gateway, not 0.0.0.0 --
# binding the wildcard address would expose a real-credentials server to the
# whole LAN, mirroring the sshd's own bridge-only posture
# (broker/sshd_cld_broker.conf).
resolve_graphql_bind() {
    : "${GRAPHQL_BIND:=$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || echo 172.17.0.1)}"
}

# Nothing reaps what `start` spawns: cld-brokerctl.sh manages only the sshd,
# and no host-side hook fires when a session container is reaped. Run at the
# top of `start` and `status` so staleness never survives past the next
# lookup, and exposed standalone as `cld-brokerctl.sh graphql-sweep` for an
# operator to reap after a host reboot without starting any session. Never
# fatal -- a sweep failure must not block the caller's own request.
sweep_gql_orphans() {
    local c s r
    for c in $(docker ps -a --filter label=org.cld.gql-session --format '{{.Names}}'); do
        s=$(docker inspect "$c" --format '{{index .Config.Labels "org.cld.gql-session"}}' 2>/dev/null) || continue
        # Deliberately "exists", not "running": a stopped-but-not-removed
        # session container is expected to resume (cld persistence model, see
        # CLAUDE.md), and reaping its graphql server on every stop would
        # defeat the point of this being an *orphan* sweep -- only a session
        # that is actually gone (container removed) should cost its server.
        docker inspect "$s" >/dev/null 2>&1 && continue   # owning session exists -- keep
        r=$(docker inspect "$c" --format '{{index .Config.Labels "org.cld.gql-repo"}}' 2>/dev/null) || true
        docker rm -f "$c" >/dev/null 2>&1 || true
        [ -n "$r" ] && jj -R "$r" --ignore-working-copy workspace forget "gql-$s" >/dev/null 2>&1 || true
    done
}

# Two host-side filters applied to everything logs/query/introspect return --
# nothing but run-tests bounds broker output today (docs/impl-graphql-broker-plan.md
# §6). Masking happens here, not client-side in the container's MCP: a server
# holding real DB credentials can echo a DSN or token in an error body, and by
# the time the container sees it, it's too late. Deliberately duplicates
# cld/log.py's mask_secrets() patterns (cross-reference:
# cld/log.py:_SECRET_KV_RE / _SECRET_PATH_RE / _SECRET_URL_RE) -- the broker
# must not depend on `cld` being importable by the host's python3.
mask_output() {
    sed -E \
        -e "s/(TOKEN|KEY|SECRET|PASSWORD)=[^[:space:],'\"]+/\\1=<redacted>/gI" \
        -e "s#/run/secrets/[A-Za-z0-9._/-]+#/run/secrets/<redacted>#g" \
        -e "s#([A-Za-z][A-Za-z0-9+.-]*://)[^/@[:space:]]*@#\\1<redacted>@#g" \
        -e 's/"(token|key|secret|password)"[[:space:]]*:[[:space:]]*"[^"]*"/"\1": "<redacted>"/gI'
}

# Cap output to $GRAPHQL_OUTPUT_MAX_BYTES. `keep`=head for query (the response
# starts with `data`), `keep`=tail for logs (recent lines matter most).
# Truncation notice goes to stderr, never mixed into the capped stdout.
cap_output() {
    local max="$1" keep="$2" data total
    data=$(cat)
    total=${#data}
    if [ "$total" -le "$max" ]; then
        printf '%s' "$data"
        return 0
    fi
    echo "[cld-broker] graphql output truncated to $max of $total bytes" \
         "(raise GRAPHQL_OUTPUT_MAX_BYTES for more)" >&2
    if [ "$keep" = head ]; then
        printf '%s' "${data:0:max}"
    else
        printf '%s' "${data: -$max}"
    fi
}

# Runs curl with '\n%{http_code}' appended to its output, splits that into
# $CURL_BODY / $CURL_STATUS globals. Centralizes the "status glued to the
# body" idiom shared by the readiness probe and query execution. Any stdin
# piped into the caller's invocation reaches curl untouched -- command
# substitution only redirects stdout.
curl_capture() {
    local out
    out=$(curl -sS -w '\n%{http_code}' "$@") || return 1
    CURL_STATUS="${out##*$'\n'}"
    CURL_BODY="${out%$'\n'*}"
}

# Aliases come from the repo's own .env (the same per-repo secrets file
# run-tests already mounts) -- no new secret store, no new broker config:
#   CLD_GRAPHQL_URL_<ALIAS>    = https://dev.example/graphql
#   CLD_GRAPHQL_AUTH_<ALIAS>   = Bearer eyJ...        # full Authorization value
#   CLD_GRAPHQL_COOKIE_<ALIAS> = session=...          # full Cookie value
# Sourced in a **subshell** and printed via indirect expansion, so the
# broker's own environment is never polluted by whatever else the repo's
# .env happens to define.
lookup_alias() {
    local uc; uc=$(printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_')
    ( set -a; [ -f "$SECRETS_ENV_FILE" ] && . "$SECRETS_ENV_FILE"; set +a
      local u="CLD_GRAPHQL_URL_$uc" a="CLD_GRAPHQL_AUTH_$uc" c="CLD_GRAPHQL_COOKIE_$uc"
      printf '%s\n%s\n%s\n' "${!u-}" "${!a-}" "${!c-}" )
}

# If the broker will curl any URL the container names, the container gains
# the host as an egress channel and SSRF pivot -- it could reach an internal
# service it cannot itself route to. It cannot leak a credential that way
# (raw URLs get no auth, see resolve_target), but the channel is new. Gated on
# an explicit hostname allowlist; empty (the default) means no raw URLs at
# all, aliases only. Exact hostname match, no substring/prefix/wildcard.
check_url_allowlisted() {
    local target="$1" host allowed=0 h
    case "$target" in
        http://*)  host="${target#http://}" ;;
        https://*) host="${target#https://}" ;;
        *) echo "denied: unsupported URL scheme in '$target' (http/https only)" >&2; exit 2 ;;
    esac
    host="${host%%/*}"     # strip path
    host="${host%%\?*}"    # strip query (in case no path preceded it)
    host="${host%%#*}"     # strip fragment
    host="${host##*@}"     # strip userinfo (user:pass@) -- must precede the port strip
    host="${host%%:*}"     # strip port
    local IFS=' '
    for h in ${GRAPHQL_URL_ALLOWLIST:-}; do
        [ "$host" = "$h" ] && allowed=1
    done
    unset IFS
    [ "$allowed" = 1 ] || {
        echo "denied: host '$host' is not in GRAPHQL_URL_ALLOWLIST -- set it in" \
             "broker.conf to permit raw-URL graphql targets (empty = none allowed)" >&2
        exit 3
    }
}

# Resolves <target> (§5.1) into $TARGET_URL / $AUTH_HEADER / $COOKIE_HEADER:
#   local              this session's own server, discovered via `docker port`
#   [A-Za-z0-9_-]+     an alias, looked up in the repo's .env -- auth attached
#   http(s)://…        the URL verbatim, gated on the allowlist -- never any auth
#   anything else      denied
resolve_target() {
    local target="$1"
    TARGET_URL=""; AUTH_HEADER=""; COOKIE_HEADER=""
    case "$target" in
        local)
            resolve_graphql_config
            local host_port
            host_port=$(docker port "cld_gql_$session" "$GQL_PORT/tcp" 2>/dev/null | head -n1) || true
            [ -n "$host_port" ] || {
                echo "denied: no local graphql server running for $session -- start one first" >&2
                exit 3
            }
            TARGET_URL="http://$host_port$GQL_HEALTH_PATH"
            ;;
        http://*|https://*)
            check_url_allowlisted "$target"
            TARGET_URL="$target"
            ;;
        *)
            [[ "$target" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "denied: bad target '$target'" >&2; exit 2; }
            resolve_secrets_env_file
            local -a fields
            mapfile -t fields <<<"$(lookup_alias "$target")"
            TARGET_URL="${fields[0]:-}"
            AUTH_HEADER="${fields[1]:-}"
            COOKIE_HEADER="${fields[2]:-}"
            [ -n "$TARGET_URL" ] || {
                local uc; uc=$(printf '%s' "$target" | tr '[:lower:]-' '[:upper:]_')
                echo "denied: unknown graphql alias '$target' -- set CLD_GRAPHQL_URL_$uc in" \
                     "$SECRETS_ENV_FILE" >&2
                exit 3
            }
            ;;
    esac
}

# Argv: query <target> <query-string> [variables-json]. The body is built with
# python3 (never string concatenation, so a query containing quotes can't
# corrupt it) and both the query and the variables travel via environment,
# never argv -- an argv element would be visible in the host's process table.
# curl gets the finished body over stdin (--data-binary @-), same reason.
do_graphql_query() {
    local target="$1" qstring="$2" variables="${3:-}"
    resolve_target "$target"

    local body
    body=$(GQL_QUERY_STRING="$qstring" GQL_VARIABLES_JSON="$variables" python3 -c '
import json, os, sys
q = os.environ["GQL_QUERY_STRING"]
v = os.environ.get("GQL_VARIABLES_JSON", "")
try:
    variables = json.loads(v) if v else {}
except json.JSONDecodeError as e:
    sys.exit(f"bad variables JSON: {e}")
sys.stdout.write(json.dumps({"query": q, "variables": variables}))
') || { echo "denied: malformed variables JSON for graphql query" >&2; exit 2; }

    local -a hdrs=(-H 'Content-Type: application/json')
    [ -n "$AUTH_HEADER" ] && hdrs+=(-H "Authorization: $AUTH_HEADER")
    [ -n "$COOKIE_HEADER" ] && hdrs+=(-H "Cookie: $COOKIE_HEADER")

    if ! printf '%s' "$body" | curl_capture --max-time "$GRAPHQL_QUERY_TIMEOUT" \
            -X POST "${hdrs[@]}" --data-binary @- "$TARGET_URL"; then
        echo "graphql request to $TARGET_URL failed (curl error)" >&2
        exit 3
    fi

    printf '%s' "$CURL_BODY" | mask_output | cap_output "$GRAPHQL_OUTPUT_MAX_BYTES" head
    echo

    case "$CURL_STATUS" in
        2??) ;;
        *) echo "graphql endpoint returned HTTP $CURL_STATUS" >&2; exit 1 ;;
    esac
}

# Kept broker-side so the container doesn't need to ship the introspection
# document itself.
_GRAPHQL_INTROSPECTION_QUERY='{__schema{queryType{name} mutationType{name} types{name kind description fields{name description type{name kind ofType{name kind ofType{name kind}}} args{name type{name kind ofType{name kind}}}} inputFields{name type{name kind ofType{name kind ofType{name kind}}}} enumValues{name description}}}}'

# alias names only, one per line, discovered by scanning .env for
# CLD_GRAPHQL_URL_* -- never any value. Printed in the alias's canonical
# lower-case/hyphenated spelling (lookup_alias's uc transform is lossy --
# both `-` and `_` fold to `_` -- so this is *a* valid spelling that round
# -trips through lookup_alias, not necessarily byte-identical to however the
# operator originally wrote it in .env).
do_graphql_endpoints() {
    resolve_secrets_env_file
    [ -f "$SECRETS_ENV_FILE" ] || return 0
    grep -oE '^CLD_GRAPHQL_URL_[A-Za-z0-9_]+' "$SECRETS_ENV_FILE" \
        | sed -E 's/^CLD_GRAPHQL_URL_//; s/_/-/g' \
        | tr '[:upper:]' '[:lower:]' \
        || true
}

# One tab-separated line: state<TAB>port<TAB>endpoint<TAB>revision<TAB>container<TAB>stale.
# state in running|starting|exited|not_started. stale is "true"/"false" when
# there's a serving revision to compare against the session's current tip,
# empty otherwise (not_started, exited, or the tip couldn't be resolved).
# Parsed client-side the same way _parse_container_line (cld/broker.py)
# parses list-containers.
print_gql_status_line() {
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5" "${6:-}"
}

do_graphql_status() {
    sweep_gql_orphans
    resolve_graphql_config
    local cname="cld_gql_$session"
    if ! docker inspect "$cname" >/dev/null 2>&1; then
        print_gql_status_line not_started "" "" "" "" ""
        return 0
    fi
    local running
    running=$(docker inspect "$cname" --format '{{.State.Running}}' 2>/dev/null)
    if [ "$running" != "true" ]; then
        print_gql_status_line exited "" "" "" "$cname" ""
        return 0
    fi
    # Read the actually-serving revision back off the container's own env
    # rather than re-resolving the session's *current* tip -- they can differ
    # once the caller has kept editing since this server started.
    local rev host_port
    rev=$(docker inspect "$cname" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
            | sed -n 's/^REVISION=//p')
    # Compare against the current tip so the caller can tell a running server
    # is serving stale code without a second call. resolve_graphql_context's
    # hard exit-3-on-failure isn't safe here -- status has to keep answering
    # on a git-backed repo, or after graphql_command was removed, same as
    # stop does today -- so resolve the tip inline and leave stale empty
    # rather than aborting the op if it can't be resolved.
    local tip stale=""
    tip=$(jj -R "$REPO" --ignore-working-copy log --no-graph -n1 -r "${session}@" -T commit_id 2>/dev/null) || true
    if [ -n "$tip" ] && [ -n "$rev" ]; then
        [ "$rev" = "$tip" ] && stale=false || stale=true
    fi
    host_port=$(docker port "$cname" "$GQL_PORT/tcp" 2>/dev/null | head -n1)
    if [ -z "$host_port" ]; then
        print_gql_status_line starting "" "" "$rev" "$cname" "$stale"
        return 0
    fi
    # docker port publishing early (or `poetry install` still running behind
    # it) doesn't mean the server itself answers yet -- a live probe is the
    # only way to tell "running" from "starting" here, same probe do_graphql_start
    # waits on.
    if ! { curl_capture --max-time 3 -X POST -H 'Content-Type: application/json' \
             --data-binary '{"query":"{__typename}"}' \
             "http://$host_port$GQL_HEALTH_PATH" \
           && [ "$CURL_STATUS" = 200 ] && [[ "$CURL_BODY" != *'"errors"'* ]]; }; then
        print_gql_status_line starting "" "" "$rev" "$cname" "$stale"
        return 0
    fi
    print_gql_status_line running "${host_port##*:}" "http://$host_port$GQL_HEALTH_PATH" "$rev" "$cname" "$stale"
}

# `docker rm -f` sends SIGKILL, so the entrypoint's own `trap EXIT` (and the
# fact that it `exec`s into $GQL_COMMAND, which drops the trap even on a
# graceful exit) never gets a chance to forget the jj workspace -- it would
# leak into the user's store. So `stop` forgets it explicitly, unconditionally
# (docs/impl-graphql-broker-plan.md §4.4). Deliberately does NOT call
# resolve_graphql_context / resolve_graphql_config -- teardown must work even
# for a repo whose graphql_command was since unset or removed.
do_graphql_stop() {
    local cname="cld_gql_$session" wsname="gql-$session"
    docker stop -t 10 "$cname" >/dev/null 2>&1 || true
    docker rm -f "$cname" >/dev/null 2>&1 || true
    jj -R "$REPO" --ignore-working-copy workspace forget "$wsname" >/dev/null 2>&1 || true
    echo "stopped"
}

# Port allocation: let Docker do it. No probing for a free port, no deriving
# one from a hash -- publish with an empty host port and read it back via
# `docker port`. Race-free, and no port range to own.
do_graphql_start() {
    sweep_gql_orphans
    resolve_graphql_context
    local cname="cld_gql_$session" wsname="gql-$session"

    # start is idempotent: a running server already answers, so return its
    # existing endpoint rather than erroring -- it's what makes the MCP's
    # start_server safe to call without checking status first.
    if docker inspect "$cname" >/dev/null 2>&1; then
        local running
        running=$(docker inspect "$cname" --format '{{.State.Running}}' 2>/dev/null)
        if [ "$running" = "true" ]; then
            do_graphql_status
            return 0
        fi
        # Stopped/crashed leftover from a previous start -- clear it before
        # relaunching so `docker run --name` below doesn't collide.
        docker rm -f "$cname" >/dev/null 2>&1 || true
        jj -R "$REPO" --ignore-working-copy workspace forget "$wsname" >/dev/null 2>&1 || true
    fi

    resolve_graphql_bind

    local secret_args=()
    [ -f "$SECRETS_ENV_FILE" ] && secret_args=(-v "$SECRETS_ENV_FILE:/secrets/.env:ro")

    # -v "$REPO:/repo" is read-write, as run-tests already does -- `jj
    # workspace add` writes store objects. The server serves from its own
    # workspace and never touches the caller's @ or bookmarks.
    docker run -d --name "$cname" \
        --label "org.cld.gql-session=$session" \
        --label "org.cld.gql-repo=$REPO" \
        --user "$(id -u):$(id -g)" -e HOME=/tmp \
        -v "$REPO:/repo" "${secret_args[@]}" \
        -e "REVISION=$REV" -e "PROJECT_SUBDIR=$PROJECT_SUBDIR" \
        -e "GQL_WORKSPACE=$wsname" -e "GQL_COMMAND=$GQL_COMMAND" \
        -e "GQL_PORT=$GQL_PORT" \
        -p "${GRAPHQL_BIND}:0:${GQL_PORT}" \
        "$GRAPHQL_IMAGE" >/dev/null

    local host_port
    host_port=$(docker port "$cname" "$GQL_PORT/tcp" | head -n1 | sed 's/.*://')
    [ -n "$host_port" ] || {
        echo "denied: could not read back the published port for $cname" >&2
        docker rm -f "$cname" >/dev/null 2>&1 || true
        exit 3
    }

    # Probe with {__typename} -- valid at the root of every GraphQL schema,
    # unlike the old in-process MCP's `query { hello }` probe, which could
    # never succeed against a real schema and busy-looped until timeout
    # (docs/impl-graphql-broker-plan.md §4.3). A real sleep between attempts here.
    local deadline=$((SECONDS + GRAPHQL_START_TIMEOUT)) ready=0 died=0
    while [ "$SECONDS" -lt "$deadline" ]; do
        # Check the container is still alive before probing it -- otherwise a
        # crash (bad graphql_command, poetry install failure) just busy-loops
        # the curl probe against a dead port until the full timeout elapses.
        running=$(docker inspect "$cname" --format '{{.State.Running}}' 2>/dev/null) || true
        if [ "$running" != "true" ]; then
            died=1
            break
        fi
        if curl_capture --max-time 5 -X POST -H 'Content-Type: application/json' \
                --data-binary '{"query":"{__typename}"}' \
                "http://${GRAPHQL_BIND}:${host_port}${GQL_HEALTH_PATH}" \
           && [ "$CURL_STATUS" = 200 ] && [[ "$CURL_BODY" != *'"errors"'* ]]; then
            ready=1
            break
        fi
        sleep 2
    done

    if [ "$died" = 1 ]; then
        echo "[cld-broker] graphql server for $session exited before becoming healthy" \
             "-- last logs:" >&2
        docker logs --tail 50 "$cname" 2>&1 | mask_output >&2 || true
        docker rm -f "$cname" >/dev/null 2>&1 || true
        jj -R "$REPO" --ignore-working-copy workspace forget "$wsname" >/dev/null 2>&1 || true
        exit 3
    fi

    if [ "$ready" != 1 ]; then
        echo "[cld-broker] graphql server for $session did not become healthy within" \
             "${GRAPHQL_START_TIMEOUT}s -- last logs:" >&2
        docker logs --tail 50 "$cname" 2>&1 | mask_output >&2 || true
        do_graphql_stop >/dev/null
        exit 3
    fi

    do_graphql_status
}

action_graphql() {
    local op="${1:-}"
    shift 2>/dev/null || true
    case "$op" in
        start|stop|restart|status|logs|query|introspect|endpoints) ;;
        *) echo "denied: bad graphql op '$op'" >&2; exit 2 ;;
    esac
    case "$op" in
        start)   do_graphql_start ;;
        stop)    do_graphql_stop ;;
        restart) do_graphql_stop >/dev/null; do_graphql_start ;;
        status)  do_graphql_status ;;
        logs)
            local n="${1:-50}"
            [[ "$n" =~ ^[0-9]+$ ]] || { echo "denied: bad tail count '$n'" >&2; exit 2; }
            [ "$n" -le 2000 ] || n=2000    # a debugging aid, not a log export
            docker logs --tail "$n" "cld_gql_$session" 2>&1 | mask_output | cap_output "$GRAPHQL_OUTPUT_MAX_BYTES" tail
            ;;
        query)
            [ $# -ge 2 ] || { echo "denied: query needs <target> <query-string> [variables-json]" >&2; exit 2; }
            do_graphql_query "$@"
            ;;
        introspect)
            do_graphql_query "${1:-local}" "$_GRAPHQL_INTROSPECTION_QUERY" ""
            ;;
        endpoints) do_graphql_endpoints ;;
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
