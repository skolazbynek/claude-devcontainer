#!/usr/bin/env bash
# otelctl -- run the whole standalone observability pipeline: an
# OpenTelemetry Collector receiving Claude Code telemetry from any
# container/host pointed at it, plus aggregate.py continuously turning its
# raw output into per-session stats.json files. One script, one thing to put
# in your startup scripts.
#
#     otelctl.sh start | restart | stop | status | logs [collector|aggregate] [N] | env [--docker] | doctor
#
# Nothing here depends on cld: it's an ordinary OTel collector plus a stdlib
# Python script, shareable with your team as-is (point any Claude Code
# session's OTEL_EXPORTER_OTLP_ENDPOINT at it). `env` prints the export
# lines to do that persistently -- see its own comment below.
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

collector_start() {
    if collector_running; then echo "collector: already running"; return 0; fi
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
    docker run -d --name "$CONTAINER_NAME" \
        --user "$(id -u):$(id -g)" \
        -p "${gateway}:${PORT}:4318" \
        -p "127.0.0.1:${PORT}:4318" \
        -v "$HERE/otel-collector-config.yaml:/etc/otelcol-contrib/config.yaml:ro" \
        -v "$DATA_DIR/data:/data" \
        "$IMAGE" >/dev/null
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
    rm -f "$AGG_PIDFILE" 2>/dev/null || true
    nohup "$PYTHON" "$HERE/aggregate.py" --watch >>"$AGG_LOG" 2>&1 &
    disown
    echo $! > "$AGG_PIDFILE"
    sleep 0.3
    if pid=$(agg_running); then
        echo "aggregate: started (pid $pid), logging to $AGG_LOG"
    else
        echo "aggregate: failed to start -- check $AGG_LOG" >&2
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

# Prints the same export lines documented in README.md/QUICK-START.md,
# sourceable directly (`eval "$(./otelctl.sh env)"`) or appendable to a
# shell rc / `.envrc` (`./otelctl.sh env >> ~/.bashrc`). Defaults to
# localhost -- the common case of a Claude Code session running directly on
# this host; pass --docker for a session running inside a docker container
# instead, which needs host.docker.internal to reach the collector.
env_cmd() {
    local host="localhost"
    case "${1:-}" in
        --docker) host="host.docker.internal" ;;
        "") ;;
        -h|--help)
            echo "usage: otelctl.sh env [--docker]" >&2
            echo "  (no flag)  host-side Claude Code session (default) -- localhost" >&2
            echo "  --docker   Claude Code session running inside a docker container -- host.docker.internal" >&2
            return 0 ;;
        *) echo "usage: otelctl.sh env [--docker]" >&2; exit 2 ;;
    esac
    cat <<EOF
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=http://${host}:${PORT}
# edit this to identify the session -- keys the per-session stats output
export OTEL_RESOURCE_ATTRIBUTES=service.name=my-session
EOF
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

doctor_check_env() {
    local v any_set=0
    for v in CLAUDE_CODE_ENABLE_TELEMETRY OTEL_METRICS_EXPORTER OTEL_EXPORTER_OTLP_PROTOCOL \
             OTEL_EXPORTER_OTLP_METRICS_PROTOCOL OTEL_EXPORTER_OTLP_ENDPOINT \
             OTEL_EXPORTER_OTLP_METRICS_ENDPOINT OTEL_RESOURCE_ATTRIBUTES; do
        if [ -n "${!v:-}" ]; then any_set=1; fi
    done

    if [ "$any_set" = "0" ]; then
        doctor_report warn "shell env" "no telemetry variables set in this shell" \
            "doctor sees only exported variables; a wrapper (cld, systemd, an IDE) sets them in its own environment, which may be fine"
        return
    fi

    if [ "${CLAUDE_CODE_ENABLE_TELEMETRY:-}" = "1" ]; then
        doctor_report ok "shell env" "telemetry enabled"
    elif [ -z "${CLAUDE_CODE_ENABLE_TELEMETRY:-}" ]; then
        doctor_report fail "shell env" "CLAUDE_CODE_ENABLE_TELEMETRY is not set" "export CLAUDE_CODE_ENABLE_TELEMETRY=1"
    else
        doctor_report warn "shell env" "CLAUDE_CODE_ENABLE_TELEMETRY=${CLAUDE_CODE_ENABLE_TELEMETRY} -- only \"1\" is documented" "export CLAUDE_CODE_ENABLE_TELEMETRY=1"
    fi

    local exporter="${OTEL_METRICS_EXPORTER:-}"
    if [ -z "$exporter" ]; then
        doctor_report fail "shell env" "OTEL_METRICS_EXPORTER is not set" "export OTEL_METRICS_EXPORTER=otlp"
    elif ! printf '%s' ",$exporter," | grep -q ',otlp,'; then
        doctor_report fail "shell env" "OTEL_METRICS_EXPORTER=$exporter does not include otlp" "export OTEL_METRICS_EXPORTER=otlp"
    else
        doctor_report ok "shell env" "OTEL_METRICS_EXPORTER includes otlp"
    fi

    local protocol="${OTEL_EXPORTER_OTLP_METRICS_PROTOCOL:-${OTEL_EXPORTER_OTLP_PROTOCOL:-}}"
    if [ -z "$protocol" ]; then
        doctor_report fail "shell env" "OTEL_EXPORTER_OTLP_PROTOCOL is not set" \
            "Claude Code has no default protocol, so nothing is exported; export OTEL_EXPORTER_OTLP_PROTOCOL=http/json"
    elif [ "$protocol" != "http/json" ] && [ "$protocol" != "http/protobuf" ]; then
        doctor_report fail "shell env" "OTEL_EXPORTER_OTLP_PROTOCOL=$protocol -- the collector only opens an HTTP receiver" \
            "export OTEL_EXPORTER_OTLP_PROTOCOL=http/json"
    elif [ "$protocol" = "http/protobuf" ]; then
        doctor_report ok "shell env" "protocol $protocol (fine -- the file exporter re-marshals to JSON regardless)"
    else
        doctor_report ok "shell env" "protocol $protocol"
    fi

    local endpoint="${OTEL_EXPORTER_OTLP_METRICS_ENDPOINT:-${OTEL_EXPORTER_OTLP_ENDPOINT:-}}"
    if [ -z "$endpoint" ]; then
        doctor_report fail "shell env" "OTEL_EXPORTER_OTLP_ENDPOINT is not set" "export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:$PORT"
    else
        local scheme rest host ep_host ep_port
        scheme="${endpoint%%://*}"
        rest="${endpoint#*://}"
        host="${rest%%/*}"
        ep_host="${host%%:*}"
        if [[ "$host" == *:* ]]; then ep_port="${host##*:}"; else ep_port=80; fi
        if [ "$scheme" != "http" ]; then
            doctor_report fail "shell env" "OTEL_EXPORTER_OTLP_ENDPOINT uses scheme \"$scheme\" -- no TLS is configured" \
                "export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:$PORT"
        elif [ "$ep_port" != "$PORT" ]; then
            doctor_report fail "shell env" "OTEL_EXPORTER_OTLP_ENDPOINT port $ep_port != collector port $PORT" \
                "export OTEL_EXPORTER_OTLP_ENDPOINT=http://$ep_host:$PORT"
        elif [ "$ep_host" = "localhost" ] || [ "$ep_host" = "127.0.0.1" ] || [ "$ep_host" = "::1" ] \
             || [ "$ep_host" = "host.docker.internal" ] || [[ "$ep_host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            # Accepted set first, no resolution probe -- host.docker.internal is the
            # *correct* endpoint for a containerised session and does not resolve on
            # the host doctor itself runs on, so resolution-testing it would report
            # the recommended config as broken. A literal IP needs no resolving.
            doctor_report ok "shell env" "endpoint http://$ep_host:$PORT"
        elif [ "${PYTHON_OK:-0}" = "1" ]; then
            # getent is glibc-only and absent on macOS, a first-class target for this
            # folder -- treating a missing getent as "unresolvable" would fail every
            # macOS user's endpoint. socket.getaddrinfo is portable.
            if "$PYTHON" -c "import socket,sys; socket.getaddrinfo(sys.argv[1], None)" "$ep_host" >/dev/null 2>&1; then
                doctor_report fail "shell env" "OTEL_EXPORTER_OTLP_ENDPOINT host \"$ep_host\" is neither loopback, host.docker.internal, nor a literal IP" \
                    "the collector only binds loopback and its docker bridge gateway IP -- this endpoint may point at a different machine's collector"
            else
                doctor_report fail "shell env" "OTEL_EXPORTER_OTLP_ENDPOINT host \"$ep_host\" does not resolve" \
                    "export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:$PORT"
            fi
        else
            doctor_report skip "shell env" "endpoint host \"$ep_host\" resolution" "cannot resolve without python3"
        fi
    fi

    local attrs="${OTEL_RESOURCE_ATTRIBUTES:-}"
    if [ -z "$attrs" ] || [[ "$attrs" != *"service.name="* ]]; then
        doctor_report warn "shell env" "OTEL_RESOURCE_ATTRIBUTES has no service.name" \
            "aggregate.py keys output on service.name and skips exports lacking it"
    elif [[ "$attrs" == *" "* ]]; then
        doctor_report fail "shell env" "OTEL_RESOURCE_ATTRIBUTES contains a space -- invalid" \
            "OTEL_RESOURCE_ATTRIBUTES must be a comma list with no spaces"
    else
        doctor_report ok "shell env" "resource attributes: $attrs"
    fi

    if [ "${OTEL_METRICS_INCLUDE_SESSION_ID:-}" = "false" ]; then
        doctor_report warn "shell env" "OTEL_METRICS_INCLUDE_SESSION_ID=false -- every session collapses into unknown-session.json"
    fi

    local temporality="${OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE:-}"
    if [ "$(printf '%s' "$temporality" | tr '[:upper:]' '[:lower:]')" = "cumulative" ]; then
        doctor_report fail "shell env" "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative -- aggregate.py sums every point as a delta" \
            "unset it, or set it to delta"
    fi

    local interval="${OTEL_METRIC_EXPORT_INTERVAL:-60000}"
    doctor_report ok "shell env" "export interval ${interval}ms" "a new session's first stats file can take that long to appear"

    if [ -n "${OTEL_EXPORTER_OTLP_HEADERS:-}" ]; then
        doctor_report ok "shell env" "OTEL_EXPORTER_OTLP_HEADERS set (this collector needs no auth)"
    fi
}

doctor_summary() {
    local parts=() all_fail_is_shell_env=1 l
    if [ "$DOCTOR_OK_COUNT" -gt 0 ]; then parts+=("$DOCTOR_OK_COUNT ok"); fi
    if [ "$DOCTOR_WARN_COUNT" -gt 0 ]; then
        if [ "$DOCTOR_WARN_COUNT" = 1 ]; then parts+=("1 warning"); else parts+=("$DOCTOR_WARN_COUNT warnings"); fi
    fi
    if [ "$DOCTOR_FAIL_COUNT" -gt 0 ]; then
        if [ "$DOCTOR_FAIL_COUNT" = 1 ]; then parts+=("1 failure"); else parts+=("$DOCTOR_FAIL_COUNT failures"); fi
    fi
    if [ "$DOCTOR_SKIP_COUNT" -gt 0 ]; then parts+=("$DOCTOR_SKIP_COUNT skipped"); fi

    local verdict
    if [ "$DOCTOR_FAIL_COUNT" -eq 0 ]; then
        verdict="telemetry is flowing"
    else
        for l in "${DOCTOR_FAIL_LABELS_ARR[@]}"; do
            [ "$l" = "shell env" ] || { all_fail_is_shell_env=0; break; }
        done
        if [ "$all_fail_is_shell_env" = "1" ]; then
            verdict="the pipeline is healthy, but this shell will not export to it"
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
    doctor)         doctor "${@:2}" ;;
    *) echo "usage: otelctl.sh {start|restart|stop|status|logs [collector|aggregate] [N]|env [--docker]|doctor}" >&2; exit 2 ;;
esac
