#!/usr/bin/env bash
# otelctl -- run the whole standalone observability pipeline: an
# OpenTelemetry Collector receiving Claude Code telemetry from any
# container/host pointed at it, plus aggregate.py continuously turning its
# raw output into per-session stats.json files. One script, one thing to put
# in your startup scripts.
#
#     otelctl.sh start | restart | stop | status | logs [collector|aggregate] [N]
#                | env [--docker] | settings [--docker] [--service-name NAME]
#                | settings install (--user|--project|--local|--file PATH) ...
#                | doctor
#
# Nothing here depends on cld: it's an ordinary OTel collector plus a stdlib
# Python script, shareable with your team as-is (point any Claude Code
# session's OTEL_EXPORTER_OTLP_ENDPOINT at it). `env` prints the export
# lines to do that persistently; `settings`/`settings install` do the same
# via Claude Code's settings.json -- see their own comments below.
#
# State lives under $CLD_OTEL_DIR (default ~/.cld/otel): the raw metrics file
# (data/raw-metrics.jsonl), the aggregator's PID file and log, and the
# per-session output (stats/*.json).
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
DATA_DIR="${CLD_OTEL_DIR:-$HOME/.cld/otel}"
CONTAINER_NAME="cld-otel-collector"
IMAGE="${CLD_OTEL_IMAGE:-otel/opentelemetry-collector-contrib:latest}"
PORT="${CLD_OTEL_PORT:-4318}"
PYTHON="${PYTHON:-python3}"
AGG_PIDFILE="$DATA_DIR/aggregate.pid"
AGG_LOG="$DATA_DIR/aggregate.log"

# --- collector (docker container) -------------------------------------

collector_running() {
    [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null)" = "true" ]
}

collector_preflight() {
    if ! command -v docker >/dev/null 2>&1; then
        echo "collector: 'docker' not found on \$PATH -- install Docker: https://docs.docker.com/get-docker/" >&2
        exit 1
    fi
    if ! docker info >/dev/null 2>&1; then
        echo "collector: docker daemon not reachable -- is it installed and running? (try 'docker info' for the raw error)" >&2
        exit 1
    fi
}

# Turn docker run's stderr into a specific diagnosis where we recognize the
# failure, falling back to the raw output otherwise so nothing is hidden.
collector_diagnose_failure() {
    local err="$1"
    if grep -qi 'port is already allocated\|address already in use\|bind.*address already in use' <<<"$err"; then
        echo "collector: port ${PORT} is already in use by something else -- stop whatever's bound to it, or set \$CLD_OTEL_PORT to a free port" >&2
    elif grep -qi 'pull access denied\|manifest.*not found\|manifest unknown\|no such host\|failed to resolve reference\|error getting credentials\|i/o timeout' <<<"$err"; then
        echo "collector: couldn't pull image '${IMAGE}' -- check network access to the registry, and that \$CLD_OTEL_IMAGE (if set) names a real image" >&2
    elif grep -qi 'permission denied' <<<"$err"; then
        echo "collector: permission denied writing to ${DATA_DIR}/data -- check that directory's ownership (the container runs as your host uid:gid $(id -u):$(id -g))" >&2
    else
        echo "collector: failed to start -- raw docker error:" >&2
    fi
    echo "$err" >&2
}

collector_start() {
    if collector_running; then echo "collector: already running"; return 0; fi
    collector_preflight
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    # Bind to the docker bridge gateway (how containers reach the host via
    # host.docker.internal) plus loopback (for host-side sessions using
    # localhost) -- reachable from both containers and the host itself, but
    # not from 0.0.0.0/the LAN-facing NIC.
    local gateway
    gateway="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')"
    # Run as our own host uid/gid: the image's default (non-root) user can't
    # write into a host-owned bind mount otherwise -- the file exporter fails
    # with "permission denied" on the raw metrics file.
    local err
    if ! err="$(docker run -d --name "$CONTAINER_NAME" \
        --user "$(id -u):$(id -g)" \
        -p "${gateway}:${PORT}:4318" \
        -p "127.0.0.1:${PORT}:4318" \
        -v "$HERE/otel-collector-config.yaml:/etc/otelcol-contrib/config.yaml:ro" \
        -v "$DATA_DIR/data:/data" \
        "$IMAGE" 2>&1 >/dev/null)"; then
        collector_diagnose_failure "$err"
        docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
        exit 1
    fi
    # `docker run -d` only reports errors from creating the container --
    # failures inside it (e.g. the file exporter hitting a permission-denied
    # bind mount) surface a moment later as the container exiting, not here.
    sleep 0.5
    if ! collector_running; then
        collector_diagnose_failure "$(docker logs "$CONTAINER_NAME" 2>&1)"
        docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
        exit 1
    fi
    echo "collector: started on ${gateway}:${PORT} + 127.0.0.1:${PORT} (containers via host.docker.internal, host via localhost -- not exposed to the LAN)"
}

collector_stop() {
    if collector_running; then docker stop "$CONTAINER_NAME" >/dev/null && echo "collector: stopped"; else echo "collector: not running"; fi
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}

# --- aggregator (background python process) -----------------------------

agg_running() {
    local pid
    pid=$(cat "$AGG_PIDFILE" 2>/dev/null) || return 1
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
    echo "$pid"
}

agg_start() {
    local pid
    if pid=$(agg_running); then echo "aggregate: already running (pid $pid)"; return 0; fi
    if ! command -v "$PYTHON" >/dev/null 2>&1; then
        echo "aggregate: '$PYTHON' not found on \$PATH -- install Python 3, or set \$PYTHON to its path" >&2
        exit 1
    fi
    rm -f "$AGG_PIDFILE" 2>/dev/null || true
    nohup "$PYTHON" "$HERE/aggregate.py" --watch >>"$AGG_LOG" 2>&1 &
    disown
    echo $! > "$AGG_PIDFILE"
    sleep 0.3
    if pid=$(agg_running); then
        echo "aggregate: started (pid $pid), logging to $AGG_LOG"
    else
        if grep -qi 'permission denied' "$AGG_LOG" 2>/dev/null; then
            echo "aggregate: failed to start -- permission denied writing under ${DATA_DIR} (check its ownership; this process runs as $(id -u):$(id -g))" >&2
        else
            echo "aggregate: failed to start -- check $AGG_LOG" >&2
        fi
        exit 1
    fi
}

agg_stop() {
    local pid
    if pid=$(agg_running); then kill "$pid" && echo "aggregate: stopped (pid $pid)"; else echo "aggregate: not running"; fi
    rm -f "$AGG_PIDFILE" 2>/dev/null || true
}

# Same lines `otelctl.sh env` prints, both variants at once, so a user who
# just ran `start` can copy-paste without a second command. Single source of
# truth is env_cmd -- don't inline the exports here.
print_env_block() {
    cat <<EOF

Point a Claude Code session at this collector -- pick the block that matches
where the session runs, and edit service.name to whatever you want that
session's stats filed under:

# session running directly on this host:
$(env_cmd)

# session running inside a docker container on this host:
$(env_cmd --docker)

Prefer a config file to shell exports? \`./otelctl.sh settings --help\`
EOF
}

# --- combined commands ---------------------------------------------------

start() {
    mkdir -p "$DATA_DIR/data"
    collector_start
    agg_start
    print_env_block
}

stop() {
    collector_stop
    agg_stop
}

restart() { stop; sleep 0.3; start; }

status() {
    if collector_running; then echo "collector: running"; else echo "collector: stopped"; fi
    local pid
    if pid=$(agg_running); then echo "aggregate: running (pid $pid)"; else echo "aggregate: stopped"; fi
}

# --- env (persistent shell setup) ----------------------------------------

# Single source of truth for the five telemetry NAME=VALUE pairs -- consumed
# by env_cmd (below) and by settings_cmd's JSON emitter (see "settings"
# section), so the two never drift apart. Prints five "NAME=VALUE" lines,
# unquoted; callers decide how to render them (export lines, JSON strings).
telemetry_vars() {
    local host="$1" service_name="${2:-my-session}"
    cat <<EOF
CLAUDE_CODE_ENABLE_TELEMETRY=1
OTEL_METRICS_EXPORTER=otlp
OTEL_EXPORTER_OTLP_PROTOCOL=http/json
OTEL_EXPORTER_OTLP_ENDPOINT=http://${host}:${PORT}
OTEL_RESOURCE_ATTRIBUTES=service.name=${service_name}
EOF
}

# Prints the same export lines documented in README.md/QUICK-START.md,
# sourceable directly (`eval "$(./otelctl.sh env)"`) or appendable to a
# shell rc / `.envrc` (`./otelctl.sh env >> ~/.bashrc`). Defaults to
# localhost -- the common case of a Claude Code session running directly on
# this host; pass --docker for a session running inside a docker container
# instead, which needs host.docker.internal to reach the collector.
#
# Prefer a settings.json instead? See `otelctl.sh settings --help`.
env_cmd() {
    local host="localhost"
    case "${1:-}" in
        --docker) host="host.docker.internal" ;;
        "") ;;
        -h|--help)
            echo "usage: otelctl.sh env [--docker]" >&2
            echo "  (no flag)  host-side Claude Code session (default) -- localhost" >&2
            echo "  --docker   Claude Code session running inside a docker container -- host.docker.internal" >&2
            echo "  see also: otelctl.sh settings --help -- write this into Claude Code's settings.json instead" >&2
            return 0 ;;
        *) echo "usage: otelctl.sh env [--docker]" >&2; exit 2 ;;
    esac
    local line name value
    while IFS='=' read -r name value; do
        if [ "$name" = "OTEL_RESOURCE_ATTRIBUTES" ]; then
            echo "# edit this to identify the session -- keys the per-session stats output"
        fi
        echo "export ${name}=${value}"
    done < <(telemetry_vars "$host")
}

# --- settings (Claude Code settings.json config path) --------------------
#
# Design: docs/design-otel-settings-config.md. `settings` prints the
# merge-ready {"env": {...}} fragment to stdout, human advice on stderr.
# `settings install` merges it into a real settings.json, read-modify-write,
# never touching any other key. Shared file-locating/parsing helpers (also
# used by doctor's check 4 resolver) live in _settings_lib_py so the two
# never disagree about where a settings file lives.

# Shared python source: settings-file discovery (F3/F4/F12/F13) and safe
# parsing. Every path here honors a DOCTOR_SETTINGS_* override so tests (and
# doctor, for its own resolver) can point every tier at a tmp_path without
# touching a real ~/.claude -- see docs/design-otel-settings-config.md's
# "test hazard" note (_run_env has no $HOME, so a naive lookup would read the
# developer's own settings file and make the suite machine-dependent).
_settings_lib_py() {
    cat <<'PYLIB'
import json
import os
import sys
from pathlib import Path

TELEMETRY_KEYS = [
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_METRICS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_RESOURCE_ATTRIBUTES",
]


def managed_settings_path():
    override = os.environ.get("DOCTOR_SETTINGS_MANAGED")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    return Path("/etc/claude-code/managed-settings.json")


def user_settings_path():
    override = os.environ.get("DOCTOR_SETTINGS_USER")
    if override:
        return Path(override)
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return Path(config_dir) / "settings.json"


def project_settings_path(cwd):
    override = os.environ.get("DOCTOR_SETTINGS_PROJECT")
    if override:
        return Path(override)
    return Path(cwd) / ".claude" / "settings.json"


def local_settings_path(cwd):
    override = os.environ.get("DOCTOR_SETTINGS_LOCAL")
    if override:
        return Path(override)
    return Path(cwd) / ".claude" / "settings.local.json"


def tier_paths(cwd):
    """Precedence high -> low, excluding `claude --settings` (unreadable, F15)
    and the shell (not a file)."""
    return [
        ("managed", managed_settings_path()),
        ("local", local_settings_path(cwd)),
        ("project", project_settings_path(cwd)),
        ("user", user_settings_path()),
    ]


class SettingsFileError(Exception):
    """A settings file that exists but cannot be used: unreadable, invalid
    JSON, or a non-object top level. Message is ready to print as-is."""


def read_settings_file(path):
    """Parsed JSON object, or None if the file does not exist. Raises
    SettingsFileError for anything that exists but is broken -- per F8, none
    of that file's settings (not just the telemetry ones) apply until it
    parses, so callers should treat this as a hard stop, not a soft skip."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise SettingsFileError(f"{path} is not readable: {e.strerror or e}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise SettingsFileError(f"{path} is not valid JSON (line {e.lineno} column {e.colno}: {e.msg})")
    if not isinstance(data, dict):
        raise SettingsFileError(f"{path} does not contain a JSON object at the top level")
    return data
PYLIB
}

_settings_usage() {
    echo "usage: otelctl.sh settings [--docker] [--service-name NAME]" >&2
    echo "       otelctl.sh settings install (--user|--project|--local|--file PATH)" >&2
    echo "                                   [--docker] [--service-name NAME] [--force] [--dry-run]" >&2
}

_settings_validate_service_name() {
    case "$1" in
        "")
            echo "otelctl: --service-name must not be empty" >&2
            exit 2 ;;
        *[[:space:],=]*)
            echo "otelctl: --service-name must not contain whitespace, a comma, or '=' -- OTEL_RESOURCE_ATTRIBUTES may not contain them" >&2
            exit 2 ;;
    esac
}

# Prints a merge-ready {"env": {...}} fragment for Claude Code's settings.json
# to stdout, and nothing else -- `./otelctl.sh settings > frag.json` is exact.
# Human advice goes to stderr. See `settings install` to merge it in directly.
settings_print() {
    local host="localhost" service_name="my-session" service_name_given=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --docker) host="host.docker.internal"; shift ;;
            --service-name)
                [ $# -ge 2 ] || { _settings_usage; exit 2; }
                service_name="$2"; service_name_given=1
                _settings_validate_service_name "$service_name"
                shift 2 ;;
            -h|--help)
                _settings_usage
                echo "  bare 'settings' prints a {\"env\": {...}} fragment to stdout for you to merge yourself" >&2
                echo "  'settings install' merges it into a real settings.json -- see its own --help" >&2
                return 0 ;;
            *) _settings_usage; exit 2 ;;
        esac
    done
    local vars
    vars="$(telemetry_vars "$host" "$service_name")"
    SETTINGS_VARS="$vars" "$PYTHON" - <<'PYEOF'
import json
import os
import sys

env = {}
for line in os.environ["SETTINGS_VARS"].splitlines():
    if not line:
        continue
    name, _, value = line.partition("=")
    env[name] = value
json.dump({"env": env}, sys.stdout, indent=2)
sys.stdout.write("\n")
PYEOF
    if [ "$service_name_given" = "0" ]; then
        echo "otelctl: edit service.name above -- it identifies this session's stats output" >&2
    fi
    echo "otelctl: settings-file values are read once at startup -- relaunch claude to apply a change" >&2
}

# Merges the fragment into a real settings.json: read-modify-write, touching
# only settings["env"][KEY] for our five keys, everything else byte-for-byte
# untouched. Design: docs/design-otel-settings-config.md D5-D8.
settings_install() {
    local target_kind="" target_path="" host="localhost" service_name="my-session"
    local force=0 dry_run=0 docker_flag=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --user) target_kind="user"; shift ;;
            --project) target_kind="project"; shift ;;
            --local) target_kind="local"; shift ;;
            --file) target_kind="file"; target_path="${2:-}"; shift 2 ;;
            --docker) docker_flag=1; host="host.docker.internal"; shift ;;
            --service-name)
                [ $# -ge 2 ] || { _settings_usage; exit 2; }
                service_name="$2"
                _settings_validate_service_name "$service_name"
                shift 2 ;;
            --force) force=1; shift ;;
            --dry-run) dry_run=1; shift ;;
            -h|--help) _settings_usage; return 0 ;;
            *) _settings_usage; exit 2 ;;
        esac
    done
    if [ -z "$target_kind" ]; then
        echo "otelctl: settings install needs a target -- --user, --project, --local, or --file PATH" >&2
        _settings_usage
        exit 2
    fi
    if [ "$target_kind" = "file" ] && [ -z "$target_path" ]; then
        echo "otelctl: --file requires a path" >&2
        exit 2
    fi
    if [ "$docker_flag" = "1" ] && [ "$target_kind" != "file" ]; then
        cat >&2 <<'MSG'
otelctl: --docker writes host.docker.internal, which only resolves inside a
container. That endpoint in a host settings file breaks every host session.
For a container, either point --file at that container's config dir, or run
`otelctl.sh settings --docker` and paste it inside the container.
MSG
        exit 2
    fi
    if [ "$target_kind" = "project" ]; then
        echo "otelctl: .claude/settings.json is meant to be committed -- everyone in this repo will pick up this collector. A shared service.name groups the team's sessions under one stats/<name>/ folder; they still split by session id, nothing is lost." >&2
    fi

    local vars
    vars="$(telemetry_vars "$host" "$service_name")"
    { _settings_lib_py; cat <<'PYEOF'
import json
import os
import shutil
import sys
from pathlib import Path

target_kind = os.environ["SETTINGS_TARGET_KIND"]
target_path_env = os.environ.get("SETTINGS_TARGET_PATH", "")
force = os.environ.get("SETTINGS_FORCE") == "1"
dry_run = os.environ.get("SETTINGS_DRY_RUN") == "1"

cwd = os.getcwd()
if target_kind == "user":
    target = user_settings_path()
elif target_kind == "project":
    target = project_settings_path(cwd)
elif target_kind == "local":
    target = local_settings_path(cwd)
else:
    target = Path(target_path_env)

desired = {}
for line in os.environ["SETTINGS_VARS"].splitlines():
    if not line:
        continue
    name, _, value = line.partition("=")
    desired[name] = value

try:
    existing = read_settings_file(target)
except SettingsFileError as e:
    print(f"otelctl: {e}", file=sys.stderr)
    print("otelctl: refusing to merge into a file Claude Code itself cannot use -- fix the JSON first, or point --file elsewhere", file=sys.stderr)
    sys.exit(1)

created = existing is None
original_bytes = target.read_bytes() if not created else None
settings = existing if existing is not None else {}

env_block = settings.get("env")
if env_block is not None and not isinstance(env_block, dict):
    print(f'otelctl: {target} has a top-level "env" key that is not an object -- refusing to replace it', file=sys.stderr)
    sys.exit(1)
if env_block is None:
    env_block = {}
    settings["env"] = env_block

inserts, unchanged, conflicts = [], [], []
for key in TELEMETRY_KEYS:
    want = desired[key]
    if key not in env_block:
        inserts.append((key, want))
    elif env_block[key] == want:
        unchanged.append((key, want))
    else:
        conflicts.append((key, env_block[key], want))

if conflicts and not force:
    print(f"otelctl: refusing to change {len(conflicts)} existing value(s) in {target}", file=sys.stderr)
    for key, old, new in conflicts:
        print(f"  ~ {key}: {json.dumps(old)} -> {json.dumps(new)}", file=sys.stderr)
    print(f"  ({len(inserts)} key(s) would be added, {len(unchanged)} already match)", file=sys.stderr)
    print("re-run with --force to overwrite, or edit the file yourself", file=sys.stderr)
    sys.exit(1)

if not inserts and not conflicts:
    print("already configured, no change")
    sys.exit(0)

applied_conflicts = list(conflicts)
for key, old, new in applied_conflicts:
    env_block[key] = new
for key, want in inserts:
    env_block[key] = want

new_text = json.dumps(settings, indent=2) + "\n"

if dry_run:
    sys.stdout.write(new_text)
    sys.exit(0)

target.parent.mkdir(parents=True, exist_ok=True)
tmp_path = target.with_name(target.name + f".otelctl-tmp-{os.getpid()}")
tmp_path.write_text(new_text)
if not created:
    try:
        shutil.copymode(target, tmp_path)
    except OSError:
        pass
else:
    os.chmod(tmp_path, 0o600)
os.replace(tmp_path, target)

# Verify (D7): re-read, and every pre-existing key -- outside our five,
# inside or outside `env` -- must still be there, deep-equal. Any mismatch
# restores the original bytes rather than leaving a bad write in place.
verify_ok = True
try:
    reread = json.loads(target.read_text())
except (OSError, json.JSONDecodeError):
    verify_ok = False
else:
    if created:
        baseline, baseline_env = {}, {}
    else:
        # Re-parsed from the pre-mutation bytes, not `existing` -- `settings`
        # (built from `existing`) was mutated in place above, so comparing
        # against `existing` would compare the write against itself.
        baseline = json.loads(original_bytes)
        baseline_env = baseline.get("env") if isinstance(baseline.get("env"), dict) else {}
    for k, v in baseline.items():
        if k != "env" and reread.get(k) != v:
            verify_ok = False
            break
    if verify_ok:
        reread_env = reread.get("env") if isinstance(reread.get("env"), dict) else {}
        for k, v in baseline_env.items():
            if k not in TELEMETRY_KEYS and reread_env.get(k) != v:
                verify_ok = False
                break

if not verify_ok:
    if created:
        try:
            target.unlink()
        except OSError:
            pass
    else:
        target.write_bytes(original_bytes)
    print(f"otelctl: internal error -- verification failed writing {target}, restored the original file. Please report this as a bug.", file=sys.stderr)
    sys.exit(1)

for key, want in inserts:
    print(f"+ {key} = {want}")
for key, old, new in applied_conflicts:
    print(f"~ {key}: {old} -> {new}")
for key, want in unchanged:
    print(f"= {key}")
print(("created " if created else "updated ") + str(target))
PYEOF
    } | SETTINGS_TARGET_KIND="$target_kind" SETTINGS_TARGET_PATH="$target_path" \
        SETTINGS_FORCE="$force" SETTINGS_DRY_RUN="$dry_run" SETTINGS_VARS="$vars" \
        "$PYTHON" -
}

settings_cmd() {
    case "${1:-}" in
        install) settings_install "${@:2}" ;;
        *) settings_print "$@" ;;
    esac
}

logs() {
    case "${1:-}" in
        collector) docker logs --tail "${2:-40}" "$CONTAINER_NAME" 2>&1 || echo "no container" ;;
        aggregate) tail -n "${2:-40}" "$AGG_LOG" 2>/dev/null || echo "no log at $AGG_LOG" ;;
        "") docker logs --tail "${2:-40}" "$CONTAINER_NAME" 2>&1 || echo "no container"
            echo "---"
            tail -n "${2:-40}" "$AGG_LOG" 2>/dev/null || echo "no log at $AGG_LOG" ;;
        *) echo "usage: otelctl.sh logs [collector|aggregate] [N]" >&2; exit 2 ;;
    esac
}

# --- doctor ---------------------------------------------------------------
#
# End-to-end health check. Design: docs/design-otel-doctor.md. Walks the
# chain shell env -> Claude Code -> :4318 receiver -> file exporter ->
# raw-metrics.jsonl -> aggregate.py --watch -> stats/, naming the first
# broken link. Diagnose only -- never starts, stops or restarts anything;
# its only writes are its own synthetic check artifacts, which it removes.

DOCTOR_OK_COUNT=0
DOCTOR_WARN_COUNT=0
DOCTOR_FAIL_COUNT=0
DOCTOR_SKIP_COUNT=0
DOCTOR_FIRST_FAIL_LABEL=""
DOCTOR_FIRST_FAIL_DETAIL=""
declare -a DOCTOR_FAIL_LABELS_ARR

# Resolved telemetry config (design: docs/design-otel-settings-config.md
# Part 2). Populated by the resolver seam before doctor_check_env runs;
# empty by default so doctor_check_env, cfg_get and cfg_src work whether or
# not the resolver ran (e.g. under the TestDoctorCheckEnv harness, which
# drives doctor_check_env directly). CFG_PATHS parallels CFG_NAMES/SRCS/VALS
# with each record's source file, empty for a shell-sourced record.
declare -a CFG_NAMES=() CFG_SRCS=() CFG_VALS=() CFG_PATHS=()
declare -a TIER_TAGS_ARR=() TIER_PATHS_ARR=() TIER_FOUND_ARR=()
DOCTOR_CFG_FROM_FILE=0
DOCTOR_SUPPRESS_SHELL_OTEL=0

doctor_report() {
    # doctor_report STATE LABEL MESSAGE [DETAIL]
    local state="$1" label="$2" message="$3" detail="${4:-}"
    local tag
    case "$state" in
        ok)   tag=" ok " ; DOCTOR_OK_COUNT=$((DOCTOR_OK_COUNT+1)) ;;
        warn) tag="warn" ; DOCTOR_WARN_COUNT=$((DOCTOR_WARN_COUNT+1)) ;;
        fail) tag="fail" ; DOCTOR_FAIL_COUNT=$((DOCTOR_FAIL_COUNT+1))
              DOCTOR_FAIL_LABELS_ARR+=("$label")
              if [ -z "$DOCTOR_FIRST_FAIL_LABEL" ]; then
                  DOCTOR_FIRST_FAIL_LABEL="$label"
                  DOCTOR_FIRST_FAIL_DETAIL="${detail:-$message}"
              fi
              ;;
        skip) tag="skip" ; DOCTOR_SKIP_COUNT=$((DOCTOR_SKIP_COUNT+1)) ;;
        *)    tag="$state" ;;
    esac
    printf '[%s] %-17s %s\n' "$tag" "$label" "$message"
    if [ -n "$detail" ]; then
        printf '                        -> %s\n' "$detail"
    fi
}

doctor_check_preflight() {
    PYTHON_OK=0
    local pyver
    if pyver=$("$PYTHON" --version 2>&1); then
        doctor_report ok python3 "$pyver ($(command -v "$PYTHON" 2>/dev/null || echo "$PYTHON"))"
        PYTHON_OK=1
    else
        doctor_report fail python3 "\$PYTHON ($PYTHON) not found or not runnable" "the pipeline itself cannot run without it either -- checks 'port' and 'round trip' will be skipped"
    fi

    if [ -d "$DATA_DIR" ]; then
        if [ -w "$DATA_DIR" ]; then
            doctor_report ok "state dir" "$DATA_DIR, writable"
        else
            doctor_report fail "state dir" "$DATA_DIR exists but is not writable"
        fi
    else
        doctor_report warn "state dir" "$DATA_DIR does not exist -- nothing has ever run here" "run \`otelctl.sh start\`"
    fi

    if [ -f /.dockerenv ]; then
        doctor_report warn "in container" "doctor is running inside a container" "127.0.0.1:$PORT here is not the collector unless the collector is in this same container -- run doctor on the host instead"
    fi
}

doctor_check_docker() {
    DOCKER_OK=0
    if ! command -v docker >/dev/null 2>&1; then
        doctor_report fail docker "\`docker\` not found on PATH" "the collector runs as a container; install Docker, then \`otelctl.sh start\`"
        doctor_report skip collector "requires docker"
        doctor_report skip mounts "requires docker"
        doctor_report skip "collector logs" "requires docker"
        return
    fi
    local info ver
    if info=$(timeout 5 docker info 2>&1); then
        ver=$(printf '%s\n' "$info" | grep -m1 '^ Server Version:' | sed 's/^ Server Version: *//')
        doctor_report ok docker "daemon reachable (server ${ver:-unknown})"
        DOCKER_OK=1
    else
        doctor_report fail docker "$(printf '%s\n' "$info" | grep -m1 -i 'cannot connect\|error' || printf '%s' "$info" | head -n1)" \
            "the collector runs as a container; start the Docker daemon (or Docker Desktop), then \`otelctl.sh start\`"
        doctor_report skip collector "requires docker"
        doctor_report skip mounts "requires docker"
        doctor_report skip "collector logs" "requires docker"
    fi
}

doctor_check_collector() {
    COLLECTOR_IS_RUNNING=0
    local status started_at
    if ! status=$(docker inspect -f '{{.State.Status}}' "$CONTAINER_NAME" 2>&1); then
        doctor_report fail collector "never started here" "run \`otelctl.sh start\`"
        doctor_report skip mounts "requires the collector container"
        doctor_report skip "collector logs" "requires the collector container"
        return
    fi

    local exitcode err restart_count image
    exitcode=$(docker inspect -f '{{.State.ExitCode}}' "$CONTAINER_NAME" 2>/dev/null || echo '?')
    err=$(docker inspect -f '{{.State.Error}}' "$CONTAINER_NAME" 2>/dev/null || true)
    restart_count=$(docker inspect -f '{{.State.RestartCount}}' "$CONTAINER_NAME" 2>/dev/null || echo 0)
    image=$(docker inspect -f '{{.Config.Image}}' "$CONTAINER_NAME" 2>/dev/null || echo '?')
    started_at=$(docker inspect -f '{{.State.StartedAt}}' "$CONTAINER_NAME" 2>/dev/null || true)

    if [ "$status" != "running" ]; then
        local detail="status=$status exit_code=$exitcode"
        if [ -n "$err" ]; then detail="$detail error=$err"; fi
        doctor_report fail collector "container exists but is not running ($status)" "$detail -- see \`otelctl.sh logs collector\`"
    else
        COLLECTOR_IS_RUNNING=1
        local uptime="" started_epoch now_epoch delta
        # date -d is GNU-only; -j -f is the BSD/macOS equivalent. Strip the
        # fractional seconds and trailing Z from Docker's RFC3339Nano
        # timestamp first, since BSD date's -f format has no fraction verb.
        started_epoch=$(date -d "$started_at" +%s 2>/dev/null \
            || date -j -u -f "%Y-%m-%dT%H:%M:%S" "${started_at%%.*}" +%s 2>/dev/null || true)
        if [ -n "$started_epoch" ]; then
            now_epoch=$(date +%s)
            delta=$((now_epoch - started_epoch))
            uptime=" up $((delta/3600))h$(((delta%3600)/60))m"
        fi
        if [ "${restart_count:-0}" -gt 0 ] 2>/dev/null; then
            doctor_report warn collector "$CONTAINER_NAME$uptime ($image) -- restarted $restart_count time(s)" "a crash-looping collector looks 'running' between restarts; check \`otelctl.sh logs collector\`"
        else
            doctor_report ok collector "$CONTAINER_NAME$uptime ($image)"
        fi
    fi

    local data_src cfg_src expect_data expect_cfg
    data_src=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$CONTAINER_NAME" 2>/dev/null || true)
    cfg_src=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/etc/otelcol-contrib/config.yaml"}}{{.Source}}{{end}}{{end}}' "$CONTAINER_NAME" 2>/dev/null || true)
    expect_data="$DATA_DIR/data"
    expect_cfg="$HERE/otel-collector-config.yaml"
    if [ "$data_src" != "$expect_data" ]; then
        doctor_report fail mounts "collector's /data is mounted from $data_src, but \$CLD_OTEL_DIR is now $DATA_DIR" "the collector is not writing where aggregate.py is reading -- \`otelctl.sh restart\`, or fix \$CLD_OTEL_DIR"
    elif [ "$cfg_src" != "$expect_cfg" ]; then
        doctor_report warn mounts "config mounted from $cfg_src, expected $expect_cfg"
    elif [ ! -f "$cfg_src" ]; then
        doctor_report ok mounts "/data -> $data_src, config -> $cfg_src"
    elif [ -z "$started_epoch" ]; then
        # Not an ok: an ok here would silently claim "not edited since
        # start" on a platform where the start time was never parsed.
        doctor_report skip mounts "config-edited-since-start check" "collector start time could not be parsed on this platform"
    else
        local cfg_mtime
        # stat -c is GNU-only; -f %m is the BSD/macOS equivalent.
        cfg_mtime=$(stat -c %Y "$cfg_src" 2>/dev/null || stat -f %m "$cfg_src" 2>/dev/null || true)
        if [ -z "$cfg_mtime" ]; then
            doctor_report skip mounts "config-edited-since-start check" "could not read $cfg_src's mtime on this platform"
        elif [ "$cfg_mtime" -gt "$started_epoch" ] 2>/dev/null; then
            doctor_report warn mounts "$expect_cfg edited since collector started" "run \`otelctl.sh restart\` to apply"
        else
            doctor_report ok mounts "/data -> $data_src, config -> $cfg_src"
        fi
    fi

    local matches
    matches=$(docker logs --tail 200 "$CONTAINER_NAME" 2>&1 | grep -iE 'error|permission denied|address already in use' || true)
    if [ -n "$matches" ]; then
        local count last
        count=$(printf '%s\n' "$matches" | grep -c .)
        last=$(printf '%s\n' "$matches" | tail -n1)
        doctor_report warn "collector logs" "$count matching line(s) in last 200; most recent: $last" "see \`otelctl.sh logs collector\`"
    else
        doctor_report ok "collector logs" "no errors in last 200 lines"
    fi
}

# --- check 4: telemetry cfg (settings files + shell) ----------------------
#
# Design: docs/design-otel-settings-config.md Part 2. Once telemetry config
# can live in Claude Code's settings.json instead of (or as well as) the
# shell, check 4 must resolve the same way Claude Code itself does (F3:
# per-variable, highest-precedence tier wins) before it can say anything
# true. The resolver below is a second invocation of doctor's existing
# python seam (docs/design-otel-doctor.md D6), run *before* check 4 so its
# output is ready when check 4 wants to print. doctor_cfg_get/doctor_cfg_src
# fall back to the process environment (source tag "shell") when the
# resolver found nothing for a name -- see D14 -- which is also what makes
# every pre-existing TestDoctorCheckEnv assertion keep testing the same
# logic: that harness drives doctor_check_env directly, the resolver never
# runs, CFG_NAMES stays empty, and every lookup falls through to the shell.
#
# Protocol on top of doctor_report's existing `report|STATE|LABEL|MSG|DETAIL`
# (docs/design-otel-doctor.md D6): two more record kinds, read the same way
# (process substitution, never a pipe, or the array appends below happen in
# a subshell and vanish -- docs/design-otel-doctor.md's own gotcha, and
# D13's restatement of it for this seam):
#   cfg|NAME|SOURCE_TAG|VALUE|PATH   -- one variable's resolved value
#   tier|TAG|PATH|FOUND              -- one settings-file tier, for the
#                                        "resolved from" legend line
_doctor_cfg_resolver_py() {
    cat <<'PYEOF'
import os

DOCTOR_CFG_NAMES = [
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_METRICS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_RESOURCE_ATTRIBUTES",
    "OTEL_METRICS_INCLUDE_SESSION_ID",
    "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE",
    "OTEL_METRIC_EXPORT_INTERVAL",
    "OTEL_EXPORTER_OTLP_HEADERS",
]


def _clean(s):
    return str(s).replace("|", "/").replace("\n", " ").strip()


def cfg(name, tag, value, path=""):
    print(f"cfg|{_clean(name)}|{_clean(tag)}|{_clean(value)}|{_clean(path)}", flush=True)


def report(state, label, message, detail=""):
    print(f"report|{_clean(state)}|{_clean(label)}|{_clean(message)}|{_clean(detail)}", flush=True)


def tier_record(tag, path, found):
    print(f"tier|{_clean(tag)}|{_clean(path)}|{1 if found else 0}", flush=True)


def _resolve():
    cwd = os.getcwd()
    tiers = tier_paths(cwd)  # from _settings_lib_py: managed, local, project, user

    # Pass 1: locate + parse every tier. A file that exists but doesn't parse
    # (or isn't readable) is a `report|fail` per D18 -- none of its settings
    # apply, telemetry or otherwise -- and contributes nothing to the merge.
    parsed = {}
    for tag, path in tiers:
        try:
            data = read_settings_file(path)
        except SettingsFileError as e:
            report("fail", "telemetry cfg", str(e),
                   "Claude Code applies none of that file's settings until it parses")
            tier_record(tag, str(path), False)
            continue
        if data is None:
            tier_record(tag, str(path), False)
            continue
        tier_record(tag, str(path), True)
        env_block = data.get("env")
        if env_block is None:
            continue
        if not isinstance(env_block, dict):
            report("warn", "telemetry cfg",
                   f'{path} has a top-level "env" key that is not an object',
                   "Claude Code will reject or ignore it")
            continue
        parsed[tag] = (path, env_block)

    # Pass 2: per-variable merge, highest-precedence tier wins (F3), first
    # tier in `tiers` order that sets a name is that name's winner. An empty
    # string is an explicit unset (F9/D19), tagged so check 4 can word it
    # as one.
    resolved = {}
    for tag, path in tiers:
        if tag not in parsed:
            continue
        _, env_block = parsed[tag]
        for name in DOCTOR_CFG_NAMES:
            if name in resolved or name not in env_block:
                continue
            value = env_block[name]
            if not isinstance(value, str):
                report("warn", "telemetry cfg",
                       f'{path} sets {name} to a non-string value ({value!r}) -- env values must be strings',
                       "treated as str(value) for the rest of this report")
                value = str(value)
            resolved[name] = (f"{tag}:cleared" if value == "" else tag, value, str(path))

    # F11/D20: a managed *generic* endpoint or protocol removes every
    # lower-tier *per-signal* value at Claude Code startup, so reporting the
    # per-signal value here (F16's ordinary preference) would be reporting a
    # value Claude Code has already discarded.
    managed_env = parsed.get("managed", (None, {}))[1]
    for generic, per_signal in (
        ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"),
        ("OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL"),
    ):
        if generic in managed_env and managed_env.get(generic, "") != "" and per_signal in resolved \
                and not resolved[per_signal][0].startswith("managed"):
            dropped_tag, dropped_value, _ = resolved.pop(per_signal)
            report("warn", "telemetry cfg",
                   f"{per_signal}={dropped_value} from {dropped_tag} is dropped at startup -- "
                   f"managed settings set the generic {generic}",
                   "managed settings remove developer-set per-signal OTLP variables when the generic "
                   "one is set (this changed in v2.1.217 -- see the monitoring-usage docs for your version)")

    for name, (tag, value, path) in resolved.items():
        cfg(name, tag, value, path)


# The resolver must never fail the run (design's implementation notes): an
# exception here would otherwise cut the process-substitution stream short
# mid-parse, silently starving doctor_check_env of some or all CFG_* records
# with no indication why. Report it and move on -- doctor_check_env falls
# back to the shell for anything left unresolved, same as it does today for
# a name the resolver legitimately found nothing for.
try:
    _resolve()
except Exception as e:
    report("warn", "telemetry cfg", f"settings-file resolver failed unexpectedly: {e}",
           "falling back to shell environment only for this run")
PYEOF
}

# Effective value + source tag for NAME: a resolver record if the settings
# files supplied one, else the process environment ("shell"), except that a
# Claude Code child session (doctor_check_env sets DOCTOR_SUPPRESS_SHELL_OTEL)
# must never treat a missing OTEL_* shell value as evidence of anything --
# per F6 it was deliberately withheld, not left unset (D16).
doctor_cfg_get() {
    local name="$1" i
    for ((i = 0; i < ${#CFG_NAMES[@]}; i++)); do
        if [ "${CFG_NAMES[$i]}" = "$name" ]; then
            echo "${CFG_VALS[$i]}"
            return 0
        fi
    done
    if [ "$DOCTOR_SUPPRESS_SHELL_OTEL" = "1" ] && [[ "$name" == OTEL_* ]]; then
        echo ""
        return 0
    fi
    echo "${!name:-}"
}

doctor_cfg_src() {
    local name="$1" i
    for ((i = 0; i < ${#CFG_NAMES[@]}; i++)); do
        if [ "${CFG_NAMES[$i]}" = "$name" ]; then
            echo "${CFG_SRCS[$i]}"
            return 0
        fi
    done
    if [ "$DOCTOR_SUPPRESS_SHELL_OTEL" = "1" ] && [[ "$name" == OTEL_* ]]; then
        echo ""
        return 0
    fi
    if [ -n "${!name:-}" ]; then echo "shell"; else echo ""; fi
}

# Path a cfg record's source came from, "" for shell/absent.
doctor_cfg_path() {
    local name="$1" i
    for ((i = 0; i < ${#CFG_NAMES[@]}; i++)); do
        if [ "${CFG_NAMES[$i]}" = "$name" ]; then
            echo "${CFG_PATHS[$i]}"
            return 0
        fi
    done
    echo ""
}

# Runs the resolver and populates CFG_NAMES/CFG_SRCS/CFG_VALS/CFG_PATHS and
# TIER_TAGS_ARR/TIER_PATHS_ARR/TIER_FOUND_ARR. A no-op (arrays stay empty)
# without python -- doctor_check_env's process-environment path still works,
# per D15's PYTHON_OK row.
doctor_check_cfg_resolve() {
    CFG_NAMES=(); CFG_SRCS=(); CFG_VALS=(); CFG_PATHS=()
    TIER_TAGS_ARR=(); TIER_PATHS_ARR=(); TIER_FOUND_ARR=()
    if [ "${PYTHON_OK:-0}" != "1" ]; then
        return
    fi
    while IFS='|' read -r kind a b c d; do
        case "$kind" in
            report) doctor_report "$a" "$b" "$c" "${d:-}" ;;
            cfg)    CFG_NAMES+=("$a"); CFG_SRCS+=("$b"); CFG_VALS+=("$c"); CFG_PATHS+=("${d:-}") ;;
            tier)   TIER_TAGS_ARR+=("$a"); TIER_PATHS_ARR+=("$b"); TIER_FOUND_ARR+=("${c:-0}") ;;
        esac
    done < <({ _settings_lib_py; _doctor_cfg_resolver_py; } | "$PYTHON" -)
}

doctor_cfg_report_legend() {
    local parts="" i tag path found count j v shell_count=0
    for ((i = 0; i < ${#TIER_TAGS_ARR[@]}; i++)); do
        tag="${TIER_TAGS_ARR[$i]}"; path="${TIER_PATHS_ARR[$i]}"; found="${TIER_FOUND_ARR[$i]}"
        [ "$found" = "1" ] || continue
        count=0
        for ((j = 0; j < ${#CFG_SRCS[@]}; j++)); do
            case "${CFG_SRCS[$j]}" in
                "$tag"|"$tag:cleared") count=$((count + 1)) ;;
            esac
        done
        [ -n "$parts" ] && parts="$parts, "
        parts="${parts}${tag}=${path} (${count} value(s))"
    done
    for v in CLAUDE_CODE_ENABLE_TELEMETRY OTEL_METRICS_EXPORTER OTEL_EXPORTER_OTLP_PROTOCOL \
             OTEL_EXPORTER_OTLP_METRICS_PROTOCOL OTEL_EXPORTER_OTLP_ENDPOINT \
             OTEL_EXPORTER_OTLP_METRICS_ENDPOINT OTEL_RESOURCE_ATTRIBUTES \
             OTEL_METRICS_INCLUDE_SESSION_ID OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE \
             OTEL_METRIC_EXPORT_INTERVAL OTEL_EXPORTER_OTLP_HEADERS; do
        [ "$(doctor_cfg_src "$v")" = "shell" ] && shell_count=$((shell_count + 1))
    done
    [ -n "$parts" ] && parts="$parts, "
    parts="${parts}shell (${shell_count} value(s))"
    doctor_report ok "telemetry cfg" "resolved from: $parts" \
        "not visible to doctor: \`claude --settings\`, MDM/registry/server-managed policy, managed-settings.d/ drop-ins, and any settings file outside \$PWD"
}

# D17: a shell export shadowed by a higher-precedence settings-file value is
# dead (F2) -- confusing, but never a `fail`: the file's value, already
# validated above, is the one actually in effect.
doctor_cfg_report_contradictions() {
    local names="" detail="" count=0 v src shell_val val path plural verb
    for v in CLAUDE_CODE_ENABLE_TELEMETRY OTEL_METRICS_EXPORTER OTEL_EXPORTER_OTLP_PROTOCOL \
             OTEL_EXPORTER_OTLP_METRICS_PROTOCOL OTEL_EXPORTER_OTLP_ENDPOINT \
             OTEL_EXPORTER_OTLP_METRICS_ENDPOINT OTEL_RESOURCE_ATTRIBUTES \
             OTEL_METRICS_INCLUDE_SESSION_ID OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE \
             OTEL_METRIC_EXPORT_INTERVAL OTEL_EXPORTER_OTLP_HEADERS; do
        src="$(doctor_cfg_src "$v")"
        case "$src" in "" | shell) continue ;; esac
        if [ "$DOCTOR_SUPPRESS_SHELL_OTEL" = "1" ] && [[ "$v" == OTEL_* ]]; then continue; fi
        shell_val="${!v:-}"
        [ -n "$shell_val" ] || continue
        val="$(doctor_cfg_get "$v")"
        [ "$shell_val" = "$val" ] && continue
        path="$(doctor_cfg_path "$v")"
        count=$((count + 1))
        [ -n "$names" ] && names="$names, "
        names="${names}${v}"
        [ -n "$detail" ] && detail="$detail; "
        detail="${detail}${v}: ${path} wins with \"${val}\", the shell export \"${shell_val}\" is dead"
    done
    if [ "$count" -gt 0 ]; then
        plural="s"; verb="have"; [ "$count" = 1 ] && { plural=""; verb="has"; }
        doctor_report warn "telemetry cfg" \
            "$count shell export$plural shadowed by a settings-file value and $verb no effect: $names" \
            "settings files override shell exports (F2) -- $detail"
    fi
}

doctor_check_env() {
    local is_child=0 child_signal=""
    if [ "${CLAUDE_CODE_CHILD_SESSION:-}" = "1" ]; then
        is_child=1; child_signal="CLAUDE_CODE_CHILD_SESSION"
    elif [ "${CLAUDECODE:-}" = "1" ]; then
        is_child=1; child_signal="CLAUDECODE -- an older Claude Code (< v2.1.172); weaker signal than CLAUDE_CODE_CHILD_SESSION"
    fi
    DOCTOR_SUPPRESS_SHELL_OTEL=$is_child
    DOCTOR_CFG_FROM_FILE=0
    [ "${#CFG_NAMES[@]}" -gt 0 ] && DOCTOR_CFG_FROM_FILE=1

    # any_set is keyed on *source*, not value -- an explicit "" (cleared, F9)
    # is meaningful config and must be validated (and reported as cleared),
    # not treated as if nothing were configured at all.
    #
    # otel_source_visible is narrower: only the 6 OTEL_* names, since
    # CLAUDE_CODE_ENABLE_TELEMETRY is passed through to a Claude Code child
    # process even though OTEL_* is withheld (F6) -- so it alone being
    # visible must never satisfy the "can validate the pipeline" gate below,
    # or every OTEL_* var falsely reads as "not set" (D16 case 2, the
    # regression this whole resolver seam exists to fix).
    local v any_set=0 otel_source_visible=0
    for v in CLAUDE_CODE_ENABLE_TELEMETRY OTEL_METRICS_EXPORTER OTEL_EXPORTER_OTLP_PROTOCOL \
             OTEL_EXPORTER_OTLP_METRICS_PROTOCOL OTEL_EXPORTER_OTLP_ENDPOINT \
             OTEL_EXPORTER_OTLP_METRICS_ENDPOINT OTEL_RESOURCE_ATTRIBUTES; do
        if [ -n "$(doctor_cfg_src "$v")" ]; then
            any_set=1
            [[ "$v" == OTEL_* ]] && otel_source_visible=1
        fi
    done

    if [ "$is_child" = "1" ] && [ "$otel_source_visible" = "0" ]; then
        # D16/D11 case 2: OTEL_* is withheld from this process on purpose (F6),
        # so its absence here is not evidence of anything, even if
        # CLAUDE_CODE_ENABLE_TELEMETRY (which IS passed through) is visible --
        # must never fail or warn about individual variables, just say why
        # nothing conclusive can be seen from here.
        doctor_report warn "telemetry cfg" \
            "running inside Claude Code -- OTEL_* is withheld from tool subprocesses ($child_signal)" \
            "this session's telemetry config cannot be read from here; run \`otelctl.sh doctor\` in a plain terminal, or check the settings files above"
        return
    fi

    if [ "$any_set" = "0" ]; then
        doctor_report warn "telemetry cfg" "no telemetry configuration found" \
            "checked: managed settings, .claude/settings.local.json, .claude/settings.json, \${CLAUDE_CONFIG_DIR:-~/.claude}/settings.json, and this shell's exports -- a wrapper (cld, systemd, an IDE) may set these somewhere doctor cannot see"
        return
    fi

    doctor_cfg_report_legend

    local val src path
    val="$(doctor_cfg_get CLAUDE_CODE_ENABLE_TELEMETRY)"; src="$(doctor_cfg_src CLAUDE_CODE_ENABLE_TELEMETRY)"
    path="$(doctor_cfg_path CLAUDE_CODE_ENABLE_TELEMETRY)"
    if [ "$val" = "1" ]; then
        doctor_report ok "telemetry cfg" "telemetry enabled [$src]"
    elif [ -z "$val" ]; then
        if [[ "$src" == *:cleared ]]; then
            doctor_report fail "telemetry cfg" "CLAUDE_CODE_ENABLE_TELEMETRY is cleared to \"\" by $path" \
                "an empty value counts as unset (F9); remove the entry or set it to \"1\""
        else
            doctor_report fail "telemetry cfg" "CLAUDE_CODE_ENABLE_TELEMETRY is not set" \
                "export CLAUDE_CODE_ENABLE_TELEMETRY=1, or set it in settings.json's env block"
        fi
    else
        doctor_report warn "telemetry cfg" "CLAUDE_CODE_ENABLE_TELEMETRY=${val} -- only \"1\" is documented [$src]" "set it to \"1\""
    fi

    val="$(doctor_cfg_get OTEL_METRICS_EXPORTER)"; src="$(doctor_cfg_src OTEL_METRICS_EXPORTER)"
    path="$(doctor_cfg_path OTEL_METRICS_EXPORTER)"
    if [ -z "$val" ]; then
        if [[ "$src" == *:cleared ]]; then
            doctor_report fail "telemetry cfg" "OTEL_METRICS_EXPORTER is cleared to \"\" by $path" \
                "an empty value counts as unset (F9); remove the entry or set it to \"otlp\""
        else
            doctor_report fail "telemetry cfg" "OTEL_METRICS_EXPORTER is not set" \
                "export OTEL_METRICS_EXPORTER=otlp, or set it in settings.json's env block"
        fi
    elif ! printf '%s' ",$val," | grep -q ',otlp,'; then
        doctor_report fail "telemetry cfg" "OTEL_METRICS_EXPORTER=$val does not include otlp [$src]" "set it to otlp"
    else
        doctor_report ok "telemetry cfg" "OTEL_METRICS_EXPORTER includes otlp [$src]"
    fi

    local protocol proto_src
    protocol="$(doctor_cfg_get OTEL_EXPORTER_OTLP_METRICS_PROTOCOL)"
    proto_src="$(doctor_cfg_src OTEL_EXPORTER_OTLP_METRICS_PROTOCOL)"
    path="$(doctor_cfg_path OTEL_EXPORTER_OTLP_METRICS_PROTOCOL)"
    if [ -z "$protocol" ]; then
        protocol="$(doctor_cfg_get OTEL_EXPORTER_OTLP_PROTOCOL)"
        proto_src="$(doctor_cfg_src OTEL_EXPORTER_OTLP_PROTOCOL)"
        path="$(doctor_cfg_path OTEL_EXPORTER_OTLP_PROTOCOL)"
    fi
    if [ -z "$protocol" ]; then
        if [[ "$proto_src" == *:cleared ]]; then
            doctor_report fail "telemetry cfg" "OTEL_EXPORTER_OTLP_PROTOCOL is cleared to \"\" by $path" \
                "an empty value counts as unset (F9); remove the entry or set it to \"http/json\""
        else
            doctor_report fail "telemetry cfg" "OTEL_EXPORTER_OTLP_PROTOCOL is not set" \
                "Claude Code has no default protocol, so nothing is exported; export OTEL_EXPORTER_OTLP_PROTOCOL=http/json, or set it in settings.json's env block"
        fi
    elif [ "$protocol" != "http/json" ] && [ "$protocol" != "http/protobuf" ]; then
        doctor_report fail "telemetry cfg" "OTEL_EXPORTER_OTLP_PROTOCOL=$protocol -- the collector only opens an HTTP receiver [$proto_src]" \
            "set it to http/json"
    elif [ "$protocol" = "http/protobuf" ]; then
        doctor_report ok "telemetry cfg" "protocol $protocol (fine -- the file exporter re-marshals to JSON regardless) [$proto_src]"
    else
        doctor_report ok "telemetry cfg" "protocol $protocol [$proto_src]"
    fi

    local endpoint ep_src
    endpoint="$(doctor_cfg_get OTEL_EXPORTER_OTLP_METRICS_ENDPOINT)"
    ep_src="$(doctor_cfg_src OTEL_EXPORTER_OTLP_METRICS_ENDPOINT)"
    path="$(doctor_cfg_path OTEL_EXPORTER_OTLP_METRICS_ENDPOINT)"
    if [ -z "$endpoint" ]; then
        endpoint="$(doctor_cfg_get OTEL_EXPORTER_OTLP_ENDPOINT)"
        ep_src="$(doctor_cfg_src OTEL_EXPORTER_OTLP_ENDPOINT)"
        path="$(doctor_cfg_path OTEL_EXPORTER_OTLP_ENDPOINT)"
    fi
    if [ -z "$endpoint" ]; then
        if [[ "$ep_src" == *:cleared ]]; then
            doctor_report fail "telemetry cfg" "OTEL_EXPORTER_OTLP_ENDPOINT is cleared to \"\" by $path" \
                "an empty value counts as unset (F9); remove the entry or set it to http://localhost:$PORT"
        else
            doctor_report fail "telemetry cfg" "OTEL_EXPORTER_OTLP_ENDPOINT is not set" \
                "export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:$PORT, or set it in settings.json's env block"
        fi
    else
        local scheme rest host ep_host ep_port
        scheme="${endpoint%%://*}"
        rest="${endpoint#*://}"
        host="${rest%%/*}"
        ep_host="${host%%:*}"
        if [[ "$host" == *:* ]]; then ep_port="${host##*:}"; else ep_port=80; fi
        if [ "$scheme" != "http" ]; then
            doctor_report fail "telemetry cfg" "OTEL_EXPORTER_OTLP_ENDPOINT uses scheme \"$scheme\" -- no TLS is configured [$ep_src]" \
                "set it to http://localhost:$PORT"
        elif [ "$ep_port" != "$PORT" ]; then
            doctor_report fail "telemetry cfg" "OTEL_EXPORTER_OTLP_ENDPOINT port $ep_port != collector port $PORT [$ep_src]" \
                "set it to http://$ep_host:$PORT"
        elif [ "$ep_host" = "localhost" ] || [ "$ep_host" = "127.0.0.1" ] || [ "$ep_host" = "::1" ] \
             || [ "$ep_host" = "host.docker.internal" ] || [[ "$ep_host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            # Accepted set first, no resolution probe -- host.docker.internal is the
            # *correct* endpoint for a containerised session and does not resolve on
            # the host doctor itself runs on, so resolution-testing it would report
            # the recommended config as broken. A literal IP needs no resolving.
            doctor_report ok "telemetry cfg" "endpoint http://$ep_host:$PORT [$ep_src]"
        elif [ "${PYTHON_OK:-0}" = "1" ]; then
            # getent is glibc-only and absent on macOS, a first-class target for this
            # folder -- treating a missing getent as "unresolvable" would fail every
            # macOS user's endpoint. socket.getaddrinfo is portable.
            if "$PYTHON" -c "import socket,sys; socket.getaddrinfo(sys.argv[1], None)" "$ep_host" >/dev/null 2>&1; then
                doctor_report fail "telemetry cfg" "OTEL_EXPORTER_OTLP_ENDPOINT host \"$ep_host\" is neither loopback, host.docker.internal, nor a literal IP [$ep_src]" \
                    "the collector only binds loopback and its docker bridge gateway IP -- this endpoint may point at a different machine's collector"
            else
                doctor_report fail "telemetry cfg" "OTEL_EXPORTER_OTLP_ENDPOINT host \"$ep_host\" does not resolve [$ep_src]" \
                    "set it to http://localhost:$PORT"
            fi
        else
            doctor_report skip "telemetry cfg" "endpoint host \"$ep_host\" resolution" "cannot resolve without python3"
        fi
    fi

    local attrs attrs_src
    attrs="$(doctor_cfg_get OTEL_RESOURCE_ATTRIBUTES)"
    attrs_src="$(doctor_cfg_src OTEL_RESOURCE_ATTRIBUTES)"
    if [ -z "$attrs" ] || [[ "$attrs" != *"service.name="* ]]; then
        doctor_report warn "telemetry cfg" "OTEL_RESOURCE_ATTRIBUTES has no service.name" \
            "aggregate.py keys output on service.name and skips exports lacking it"
    elif [[ "$attrs" == *" "* ]]; then
        doctor_report fail "telemetry cfg" "OTEL_RESOURCE_ATTRIBUTES contains a space -- invalid [$attrs_src]" \
            "OTEL_RESOURCE_ATTRIBUTES must be a comma list with no spaces"
    else
        doctor_report ok "telemetry cfg" "resource attributes: $attrs [$attrs_src]"
    fi

    if [ "$(doctor_cfg_get OTEL_METRICS_INCLUDE_SESSION_ID)" = "false" ]; then
        doctor_report warn "telemetry cfg" "OTEL_METRICS_INCLUDE_SESSION_ID=false -- every session collapses into unknown-session.json [$(doctor_cfg_src OTEL_METRICS_INCLUDE_SESSION_ID)]"
    fi

    local temporality
    temporality="$(doctor_cfg_get OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE)"
    if [ "$(printf '%s' "$temporality" | tr '[:upper:]' '[:lower:]')" = "cumulative" ]; then
        doctor_report fail "telemetry cfg" "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative -- aggregate.py sums every point as a delta [$(doctor_cfg_src OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE)]" \
            "unset it, or set it to delta"
    fi

    local interval
    interval="$(doctor_cfg_get OTEL_METRIC_EXPORT_INTERVAL)"
    [ -z "$interval" ] && interval=60000
    local relaunch_detail="a new session's first stats file can take that long to appear"
    if [ "$DOCTOR_CFG_FROM_FILE" = "1" ]; then
        relaunch_detail="$relaunch_detail (F5: settings-file values are read once at startup -- relaunch \`claude\` to apply a change)"
    fi
    doctor_report ok "telemetry cfg" "export interval ${interval}ms" "$relaunch_detail"

    if [ -n "$(doctor_cfg_get OTEL_EXPORTER_OTLP_HEADERS)" ]; then
        doctor_report ok "telemetry cfg" "OTEL_EXPORTER_OTLP_HEADERS set (this collector needs no auth) [$(doctor_cfg_src OTEL_EXPORTER_OTLP_HEADERS)]"
    fi

    doctor_cfg_report_contradictions
}

doctor_summary() {
    local parts=() all_fail_is_cfg=1 l
    if [ "$DOCTOR_OK_COUNT" -gt 0 ]; then parts+=("$DOCTOR_OK_COUNT ok"); fi
    if [ "$DOCTOR_WARN_COUNT" -gt 0 ]; then
        if [ "$DOCTOR_WARN_COUNT" = 1 ]; then parts+=("1 warning"); else parts+=("$DOCTOR_WARN_COUNT warnings"); fi
    fi
    if [ "$DOCTOR_FAIL_COUNT" -gt 0 ]; then
        if [ "$DOCTOR_FAIL_COUNT" = 1 ]; then parts+=("1 failure"); else parts+=("$DOCTOR_FAIL_COUNT failures"); fi
    fi
    if [ "$DOCTOR_SKIP_COUNT" -gt 0 ]; then parts+=("$DOCTOR_SKIP_COUNT skipped"); fi

    # D15/D22: softening now hinges on whether the effective config came from
    # a settings file (DOCTOR_CFG_FROM_FILE, set in doctor_check_env) rather
    # than unconditionally on the label -- a file is authoritative and read
    # once at Claude Code startup, so a broken file is a real, strict failure;
    # a shell-only gap is still just "this terminal won't export" (unchanged
    # behavior, D15 row 1 vs row 2).
    local verdict
    if [ "$DOCTOR_FAIL_COUNT" -eq 0 ]; then
        verdict="telemetry is flowing"
    else
        for l in "${DOCTOR_FAIL_LABELS_ARR[@]}"; do
            [ "$l" = "telemetry cfg" ] || { all_fail_is_cfg=0; break; }
        done
        if [ "$all_fail_is_cfg" = "1" ] && [ "${DOCTOR_CFG_FROM_FILE:-0}" != "1" ]; then
            verdict="the pipeline is healthy, but this shell will not export to it"
        elif [ "$all_fail_is_cfg" = "1" ]; then
            verdict="Claude Code's telemetry config is broken"
        else
            verdict="telemetry is NOT being collected"
        fi
    fi

    local joined="" p
    for p in "${parts[@]}"; do
        if [ -n "$joined" ]; then joined="$joined, $p"; else joined="$p"; fi
    done
    echo "$joined -- $verdict"
    if [ "$DOCTOR_FAIL_COUNT" -gt 0 ]; then echo "next: $DOCTOR_FIRST_FAIL_DETAIL"; fi
}

doctor() {
    local timeout_secs=10 no_synthetic=0 keep_artifacts=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --timeout) timeout_secs="${2:-}"; shift 2 ;;
            --no-synthetic) no_synthetic=1; shift ;;
            --keep-check-artifacts) keep_artifacts=1; shift ;;
            *) echo "usage: otelctl.sh doctor [--timeout SECS] [--no-synthetic] [--keep-check-artifacts]" >&2; exit 2 ;;
        esac
    done

    echo "otelctl doctor -- state $DATA_DIR, collector port $PORT"
    echo

    doctor_check_preflight
    doctor_check_docker
    if [ "$DOCKER_OK" = "1" ]; then
        doctor_check_collector
    fi
    doctor_check_cfg_resolve
    doctor_check_env

    local agg_pid
    agg_pid=$(agg_running 2>/dev/null || true)

    if [ "$PYTHON_OK" = "1" ]; then
        while IFS='|' read -r state label message detail; do
            doctor_report "$state" "$label" "$message" "$detail"
        done < <(
            DOCTOR_PORT="$PORT" DOCTOR_DATA_DIR="$DATA_DIR" DOCTOR_HERE="$HERE" \
            DOCTOR_CONTAINER="$CONTAINER_NAME" DOCTOR_TIMEOUT="$timeout_secs" \
            DOCTOR_NO_SYNTHETIC="$no_synthetic" DOCTOR_KEEP_ARTIFACTS="$keep_artifacts" \
            DOCTOR_COLLECTOR_RUNNING="${COLLECTOR_IS_RUNNING:-0}" DOCTOR_AGG_PID="$agg_pid" \
            "$PYTHON" - <<'PYEOF'
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT = int(os.environ["DOCTOR_PORT"])
DATA_DIR = Path(os.environ["DOCTOR_DATA_DIR"])
HERE = Path(os.environ["DOCTOR_HERE"])
CONTAINER = os.environ.get("DOCTOR_CONTAINER", "cld-otel-collector")
TIMEOUT = float(os.environ.get("DOCTOR_TIMEOUT") or "10")
NO_SYNTHETIC = os.environ.get("DOCTOR_NO_SYNTHETIC", "0") == "1"
KEEP_ARTIFACTS = os.environ.get("DOCTOR_KEEP_ARTIFACTS", "0") == "1"
COLLECTOR_RUNNING = os.environ.get("DOCTOR_COLLECTOR_RUNNING", "0") == "1"
AGG_PID = os.environ.get("DOCTOR_AGG_PID", "").strip()


def report(state, label, message, detail=""):
    def clean(s):
        return str(s).replace("|", "/").replace("\n", " ").strip()
    print(f"{state}|{clean(label)}|{clean(message)}|{clean(detail)}", flush=True)


def docker_fmt(fmt, name=CONTAINER, timeout=5):
    try:
        out = subprocess.run(["docker", "inspect", "-f", fmt, name],
                              capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


# --- check 3: port ---------------------------------------------------------

published = None
if COLLECTOR_RUNNING:
    published = docker_fmt(
        '{{range $p,$c := .NetworkSettings.Ports}}{{if eq $p "%d/tcp"}}'
        '{{range $c}}{{.HostIp}}:{{.HostPort}} {{end}}{{end}}{{end}}' % PORT)


def tcp_connect(host, port, timeout=2.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


tcp_ok = tcp_connect("127.0.0.1", PORT)

probe_status = None
probe_body = b""
if tcp_ok:
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/v1/metrics",
        data=b"{", headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            probe_status = resp.status
            probe_body = resp.read()
    except urllib.error.HTTPError as e:
        probe_status = e.code
        probe_body = e.read()
    except Exception as e:
        probe_body = str(e).encode()

receiver_ok = False
if not tcp_ok:
    detail = ""
    if COLLECTOR_RUNNING:
        detail = "the container is up but its port is not published -- was it started with a different CLD_OTEL_PORT?"
    report("fail", f"port {PORT}", f"nothing listening on 127.0.0.1:{PORT}", detail)
elif probe_status == 404:
    report("fail", f"port {PORT}",
           f"something is answering on {PORT} but /v1/metrics is not there -- is that really an OTel collector?")
elif probe_status is not None and (probe_status == 200 or 400 <= probe_status < 500):
    pub = f"bound on {published.strip()}; " if published else ""
    report("ok", f"port {PORT}", f"{pub}OTLP receiver answering")
    receiver_ok = True
else:
    msg = "port is bound but the receiver did not answer" if probe_status is None \
        else f"port is bound but the receiver returned HTTP {probe_status}"
    report("fail", f"port {PORT}", msg)

if COLLECTOR_RUNNING:
    try:
        gw_out = subprocess.run(
            ["docker", "network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}"],
            capture_output=True, text=True, timeout=5)
        current_gw = gw_out.stdout.strip() if gw_out.returncode == 0 else None
    except Exception:
        current_gw = None
    if current_gw and published and current_gw not in published:
        report("warn", "bridge gateway",
               f"collector published on {published.strip()}, but the bridge gateway is now {current_gw}",
               "docker likely restarted and renumbered it -- run `otelctl.sh restart`")

# --- check 5: synthetic round trip -----------------------------------------

if NO_SYNTHETIC:
    report("skip", "round trip", "--no-synthetic")
elif not receiver_ok:
    report("skip", "round trip", "requires a reachable OTLP receiver")
else:
    epoch = int(time.time())
    session_id = f"doctorcheck-{epoch}"
    service_name = "otelctl-doctor-check"
    now_ns = str(int(time.time() * 1e9))

    def sess_attr():
        return {"key": "session.id", "value": {"stringValue": session_id}}

    def type_attr(t):
        return {"key": "type", "value": {"stringValue": t}}

    def dp(*extra_attrs, **val):
        d = {"attributes": [sess_attr(), *extra_attrs],
             "startTimeUnixNano": now_ns, "timeUnixNano": now_ns}
        d.update(val)
        return d

    payload = {"resourceMetrics": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service_name}}]},
        "scopeMetrics": [{
            "scope": {"name": "otelctl.doctor"},
            "metrics": [
                {"name": "claude_code.cost.usage", "unit": "USD",
                 "sum": {"aggregationTemporality": 1, "isMonotonic": True,
                         "dataPoints": [dp(asDouble=0.000001)]}},
                {"name": "claude_code.token.usage", "unit": "tokens",
                 "sum": {"aggregationTemporality": 1, "isMonotonic": True,
                         "dataPoints": [
                             dp(type_attr("input"), asInt="1"),
                             dp(type_attr("output"), asInt="2"),
                             dp(type_attr("cacheRead"), asInt="3"),
                             dp(type_attr("cacheCreation"), asInt="4"),
                         ]}},
                {"name": "claude_code.active_time.total", "unit": "s",
                 "sum": {"aggregationTemporality": 1, "isMonotonic": True,
                         "dataPoints": [dp(type_attr("cli"), asDouble=0.001)]}},
            ],
        }],
    }]}

    stats_paths = [
        DATA_DIR / "stats" / service_name / "session-doctorcheck.json",
        DATA_DIR / "stats" / "session-doctorcheck.json",
    ]

    def clean_artifacts():
        for p in stats_paths:
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        svc_dir = DATA_DIR / "stats" / service_name
        try:
            if not any(svc_dir.iterdir()):
                svc_dir.rmdir()
        except (FileNotFoundError, NotADirectoryError, OSError):
            pass

    clean_artifacts()

    raw_path = DATA_DIR / "data" / "raw-metrics.jsonl"
    try:
        size_before = raw_path.stat().st_size
    except FileNotFoundError:
        size_before = 0

    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/v1/metrics", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.monotonic()
    post_status = None
    post_body = b""
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            post_status = resp.status
            post_body = resp.read()
    except urllib.error.HTTPError as e:
        post_status = e.code
        post_body = e.read()
    except Exception as e:
        post_body = str(e).encode()

    rejected = 0
    err_msg = ""
    if post_body:
        try:
            parsed = json.loads(post_body)
            ps = parsed.get("partialSuccess") or {}
            rejected = ps.get("rejectedDataPoints") or 0
            err_msg = ps.get("errorMessage") or ""
        except json.JSONDecodeError:
            pass

    if post_status != 200 or rejected:
        suffix = f": {err_msg}" if err_msg else ""
        report("fail", "round trip", f"POST rejected (HTTP {post_status}){suffix}")
        if not KEEP_ARTIFACTS:
            clean_artifacts()
    else:
        found_line = None
        deadline = time.monotonic() + 5
        while found_line is None and time.monotonic() < deadline:
            try:
                with raw_path.open("r") as f:
                    f.seek(size_before)
                    for line in f:
                        if not line.endswith("\n"):
                            continue
                        if service_name in line and session_id in line:
                            found_line = line
                            break
            except FileNotFoundError:
                pass
            if found_line is None:
                time.sleep(0.2)
        raw_elapsed = time.monotonic() - t0

        if found_line is None:
            report("fail", "round trip",
                   "the collector accepted the metric but never wrote it to raw-metrics.jsonl",
                   "the file exporter is broken, likely the bind-mount permission trap -- "
                   "see check 'collector logs' and `otelctl.sh logs collector`")
        else:
            def sentinel_ok(data):
                try:
                    return (abs(float(data.get("cost_usd", -1)) - 0.000001) < 1e-9
                            and data.get("tokens") == {"input": 1, "output": 2, "cache_read": 3, "cache_creation": 4}
                            and abs(float(data.get("active_time_seconds", -1)) - 0.001) < 1e-9)
                except (TypeError, ValueError):
                    return False

            t1 = time.monotonic()
            stats_deadline = t1 + TIMEOUT
            matched = None
            while matched is None and time.monotonic() < stats_deadline:
                for p in stats_paths:
                    try:
                        data = json.loads(p.read_text())
                    except (FileNotFoundError, json.JSONDecodeError):
                        continue
                    if data.get("session_id") == session_id:
                        matched = data
                        break
                if matched is None:
                    time.sleep(0.2)
            stats_elapsed = time.monotonic() - t1

            if matched is not None and sentinel_ok(matched):
                watcher_note = f", live watcher pid {AGG_PID}" if AGG_PID else ""
                report("ok", "round trip",
                       f"POST accepted -> raw-metrics.jsonl ({raw_elapsed:.1f}s) -> "
                       f"stats file ({stats_elapsed:.1f}s){watcher_note}")
            elif matched is not None:
                report("fail", "round trip",
                       "stats file present but not from this run -- a leftover artifact that could not be pre-deleted?")
            else:
                tmpdir = Path(tempfile.mkdtemp(prefix="otelctl-doctor-"))
                try:
                    (tmpdir / "raw-metrics.jsonl").write_text(found_line)
                    replay_out = tmpdir / "stats"
                    proc = subprocess.run(
                        [sys.executable, str(HERE / "aggregate.py"),
                         "--input", str(tmpdir / "raw-metrics.jsonl"),
                         "--output-dir", str(replay_out)],
                        capture_output=True, text=True, timeout=15)
                    replay_path = replay_out / service_name / "session-doctorcheck.json"
                    replay_ok = False
                    if replay_path.is_file():
                        try:
                            rdata = json.loads(replay_path.read_text())
                            replay_ok = sentinel_ok(rdata) and rdata.get("session_id") == session_id
                        except json.JSONDecodeError:
                            replay_ok = False

                    if not AGG_PID:
                        if replay_ok:
                            report("warn", "round trip",
                                   "no aggregator running; aggregate.py converts the metric correctly, so "
                                   "raw data is being captured and will be aggregated as soon as one starts",
                                   "run `otelctl.sh start`")
                        else:
                            report("fail", "round trip",
                                   "no aggregator running, and aggregate.py could not convert the metric in isolation",
                                   (proc.stderr or "no output").strip()[:200])
                    else:
                        if replay_ok:
                            offset_path = DATA_DIR / "stats" / ".raw-metrics.jsonl.offset"
                            try:
                                offset_val = offset_path.read_text().strip()
                            except FileNotFoundError:
                                offset_val = "?"
                            try:
                                raw_size = raw_path.stat().st_size
                            except FileNotFoundError:
                                raw_size = "?"
                            argv = ""
                            try:
                                psout = subprocess.run(["ps", "-p", AGG_PID, "-o", "args="],
                                                        capture_output=True, text=True, timeout=3)
                                argv = psout.stdout.strip()
                            except Exception:
                                pass
                            detail = f"offset={offset_val} raw-size={raw_size}"
                            if argv:
                                detail += f" argv={argv}"
                            detail += " -- see `otelctl.sh logs aggregate`"
                            report("fail", "round trip",
                                   f"aggregator alive (pid {AGG_PID}) but did not pick up the metric in "
                                   f"{TIMEOUT:.0f}s, while aggregate.py handles the same line fine in isolation -- "
                                   "the watcher is stalled or watching something else",
                                   detail)
                        else:
                            report("fail", "round trip",
                                   "aggregate.py could not turn the received metric into stats",
                                   (proc.stderr or "no output").strip()[:200])
                finally:
                    shutil.rmtree(tmpdir, ignore_errors=True)

        if KEEP_ARTIFACTS:
            report("ok", "cleanup", f"kept stats/{service_name}/ (--keep-check-artifacts)",
                   "1 synthetic line remains in raw-metrics.jsonl (append-only, cannot be cleaned)")
        else:
            clean_artifacts()
            report("ok", "cleanup", f"removed stats/{service_name}/",
                   "1 synthetic line remains in raw-metrics.jsonl (append-only, cannot be cleaned)")
PYEOF
        )
    else
        doctor_report skip "port $PORT" "requires python3"
        doctor_report skip "round trip" "requires python3"
    fi

    echo
    doctor_summary
    [ "$DOCTOR_FAIL_COUNT" -eq 0 ]
}

case "${1:-}" in
    start)          start ;;
    restart)        restart ;;
    stop|shutdown)  stop ;;
    status)         status ;;
    logs)           logs "${2:-}" "${3:-40}" ;;
    env)            env_cmd "${2:-}" ;;
    settings)       settings_cmd "${@:2}" ;;
    doctor)         doctor "${@:2}" ;;
    *) echo "usage: otelctl.sh {start|restart|stop|status|logs [collector|aggregate] [N]|env [--docker]|settings [--docker] [--service-name NAME]|settings install ...|doctor}" >&2; exit 2 ;;
esac
