#!/usr/bin/env bash
# cld-brokerctl -- operate the cld host broker sshd. First-time setup is manual
# (see ../broker-setup-home.md); day-to-day this is all you need:
#
#     cld-brokerctl start | restart | shutdown | status | logs [N] | graphql-sweep
#
# Everything lives under $CLD_BROKER_DIR (default ~/.cld/broker): the sshd config,
# its PID file, and its log. No sshd flags to remember.
set -euo pipefail

BROKER="${CLD_BROKER_DIR:-$HOME/.cld/broker}"
CONF="$BROKER/sshd_cld_broker.conf"
PIDFILE="$BROKER/sshd.pid"
LOG="$BROKER/sshd.log"
SSHD="$(command -v sshd || echo /usr/sbin/sshd)"

# Echo the running pid (and succeed) only if the PID file names a live process.
running() {
    local pid
    pid=$(cat "$PIDFILE" 2>/dev/null) || return 1
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
    echo "$pid"
}

start() {
    local pid
    if pid=$(running); then echo "already running (pid $pid)"; return 0; fi
    [ -r "$CONF" ] || { echo "no broker config at $CONF -- run first-time setup" >&2; exit 1; }
    rm -f "$PIDFILE" 2>/dev/null || true          # clear any stale pidfile
    # No -D: sshd daemonizes, detaching from this shell. -E logs to the file.
    if ! "$SSHD" -f "$CONF" -E "$LOG"; then
        echo "sshd failed to start. Last log lines:" >&2
        tail -n 15 "$LOG" 2>/dev/null >&2 || true
        echo "(if it mentions /run/sshd: 'sudo mkdir -p /run/sshd' once, or start via sudo)" >&2
        exit 1
    fi
    if pid=$(running); then echo "started (pid $pid)"; else
        echo "sshd exited immediately -- check $LOG" >&2; exit 1; fi
}

shutdown() {
    local pid
    if pid=$(running); then kill "$pid" && echo "stopped (pid $pid)"; else echo "not running"; fi
    rm -f "$PIDFILE" 2>/dev/null || true
}

restart() { shutdown; sleep 0.3; start; }

# Duplicates cld-broker.sh's sweep_gql_orphans() rather than sourcing that
# file: cld-broker.sh's own tail unconditionally dispatches on $SSH_ORIGINAL_COMMAND
# once sourced, which would run (or fail oddly) here. Reaps any `graphql`
# broker-action server whose owning session container is gone -- useful after
# a host reboot, without needing to start any session first. Cross-reference:
# broker/cld-broker.sh:sweep_gql_orphans.
graphql-sweep() {
    local c s r
    for c in $(docker ps -a --filter label=org.cld.gql-session --format '{{.Names}}'); do
        s=$(docker inspect "$c" --format '{{index .Config.Labels "org.cld.gql-session"}}' 2>/dev/null) || continue
        docker inspect "$s" >/dev/null 2>&1 && continue   # owning session alive -- keep
        r=$(docker inspect "$c" --format '{{index .Config.Labels "org.cld.gql-repo"}}' 2>/dev/null) || true
        echo "reaping orphaned graphql server $c (session $s gone)"
        docker rm -f "$c" >/dev/null 2>&1 || true
        [ -n "$r" ] && jj -R "$r" --ignore-working-copy workspace forget "gql-$s" >/dev/null 2>&1 || true
    done
}

status() {
    local pid
    if pid=$(running); then
        echo "running (pid $pid)"
        grep -E '^(ListenAddress|Port|AllowUsers)' "$CONF" 2>/dev/null | sed 's/^/  /' || true
    else
        echo "stopped"
    fi
}

case "${1:-}" in
    start)            start ;;
    restart)          restart ;;
    shutdown|stop)    shutdown ;;
    status)           status ;;
    logs)             tail -n "${2:-40}" "$LOG" 2>/dev/null || echo "no log at $LOG" ;;
    graphql-sweep)    graphql-sweep ;;
    *) echo "usage: cld-brokerctl {start|restart|shutdown|status|logs [N]|graphql-sweep}" >&2; exit 2 ;;
esac
