#!/usr/bin/env bash
# otelctl -- run the whole standalone observability pipeline: an
# OpenTelemetry Collector receiving Claude Code telemetry from any
# container/host pointed at it, plus aggregate.py continuously turning its
# raw output into per-session stats.json files. One script, one thing to put
# in your startup scripts.
#
#     otelctl.sh start | restart | stop | status | logs [collector|aggregate] [N] | env [--docker]
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

# --- combined commands ---------------------------------------------------

start() {
    mkdir -p "$DATA_DIR/data"
    collector_start
    agg_start
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

case "${1:-}" in
    start)          start ;;
    restart)        restart ;;
    stop|shutdown)  stop ;;
    status)         status ;;
    logs)           logs "${2:-}" "${3:-40}" ;;
    env)            env_cmd "${2:-}" ;;
    *) echo "usage: otelctl.sh {start|restart|stop|status|logs [collector|aggregate] [N]|env [--docker]}" >&2; exit 2 ;;
esac
