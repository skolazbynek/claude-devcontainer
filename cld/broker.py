"""The cld broker seam: local docker on the host, SSH to the host broker in a container.

On the host, cld talks to the Docker daemon directly. Inside a container there is no
docker socket (removed by design -- see docs/design-cld-broker.md); the only host
channel is the broker: an sshd whose ForceCommand is `broker/cld-broker.sh`, reached
here by building the ssh argv ourselves. This module centralizes that host-vs-broker
decision so call sites stay backend-agnostic.

The set of functions here is deliberately the entire surface reachable from inside a
container -- each maps 1:1 to an `action_<name>` in the broker script. Anything not
exposed here simply cannot be done from a container, by construction.
"""

import base64
import os
import subprocess
from pathlib import Path

from cld.docker import in_master_container
from cld.log import get_logger

log = get_logger(__name__)

# Where stage_broker (cld/docker.py) mounts the restricted key and the pinned
# known_hosts. Their presence, with an endpoint, is the availability signal.
KEY_MOUNT = "/run/secrets/broker-key"
KNOWN_HOSTS_MOUNT = "/run/secrets/broker-known-hosts"

# Login user when `broker_endpoint` carries no `user@`.
_DEFAULT_USER = "zet"


def broker_available() -> bool:
    """True when this container has an endpoint, the restricted key AND the known_hosts.

    All three, because ``run_action`` always ssh's with ``StrictHostKeyChecking=yes``
    against ``KNOWN_HOSTS_MOUNT``: without that mount the connection can only fail on
    the host-key check. ``stage_broker`` merely warns host-side when
    ``broker_known_hosts`` is unset or missing, so counting it here is what turns a raw
    ssh error into the callers' actionable "set broker_key (and broker_known_hosts)".
    """
    return (
        bool(os.environ.get("CLD_BROKER_ENDPOINT"))
        and Path(KEY_MOUNT).exists()
        and Path(KNOWN_HOSTS_MOUNT).exists()
    )


def _endpoint() -> tuple[str, str, str]:
    """Split ``CLD_BROKER_ENDPOINT`` ([user@]host:port) into (user, host, port)."""
    endpoint = os.environ.get("CLD_BROKER_ENDPOINT", "")
    user, sep, hostport = endpoint.rpartition("@")
    host, _, port = hostport.partition(":")
    return (user if sep else _DEFAULT_USER), host, (port or "2222")


def run_action(action: str, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
    """Run *action* on the host broker with *args*, over ssh.

    The wire format is `<action> <session> <base64-argv>`; the argv is
    base64(NUL-joined) so it can only ever become arguments to the action's command,
    never a host command (the broker never evals it). With ``capture=False``
    stdout/stderr are inherited so the broker's output streams straight to the user
    (lifecycle ops); with ``capture=True`` stdout is captured for parsing.
    """
    if not broker_available():
        log.warning(
            "the cld broker is not configured for this container, so '%s' cannot "
            "reach the host.", action,
        )
        return subprocess.CompletedProcess([], 127, stdout="", stderr="broker not available")

    user, host, port = _endpoint()
    payload = base64.b64encode(b"".join(a.encode() + b"\0" for a in args)).decode("ascii")
    session = os.environ.get("SESSION_NAME", "")
    cmd = [
        "ssh", "-i", KEY_MOUNT,
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS_MOUNT}",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "IdentitiesOnly=yes",
        "-p", port, f"{user}@{host}",
        "--", f"{action} {session} {payload}",
    ]
    log.debug("broker: %s %s (%d args)", action, session, len(args))
    return subprocess.run(cmd, capture_output=capture, text=True)


def _parse_container_line(line: str) -> dict | None:
    """Parse one tab-separated `list-containers` broker line into a record.

    Line shape: ``name<TAB>kind<TAB>repo<TAB>raw-status`` (raw-status is the
    ``docker ps`` Status column, e.g. "Up 3 minutes"). Mirrors the mapping the
    local-docker path applies so both backends return identical records.
    """
    parts = line.split("\t")
    if len(parts) < 4:
        return None
    name, kind, repo, raw_status = parts[0], parts[1], parts[2], parts[3]
    if not name:
        return None
    return {
        "name": name,
        "kind": kind,
        "repo": repo,
        "status": "running" if raw_status.lower().startswith("up") else "stopped",
    }


def _list_via_local_docker(kind: str | None) -> list[dict]:
    """Enumerate cld containers with the local Docker daemon (host path)."""
    filters = ["--filter", f"label=org.cld.kind={kind}"] if kind else ["--filter", "label=org.cld.kind"]
    result = subprocess.run(
        ["docker", "ps", "-a", *filters, "--format", "{{.Names}}\t{{.Status}}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.warning("docker ps failed: %s", result.stderr.strip())
        return []

    containers = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        name, _, status = line.partition("\t")
        inspect = subprocess.run(
            ["docker", "inspect", name, "--format",
             '{{index .Config.Labels "org.cld.kind"}}|{{index .Config.Labels "org.cld.repo-root"}}'],
            capture_output=True, text=True,
        )
        if inspect.returncode != 0:
            continue
        parts = inspect.stdout.strip().split("|", 1)
        containers.append({
            "name": name,
            "kind": parts[0] if parts else "",
            "repo": parts[1] if len(parts) > 1 else "",
            "status": "running" if status.lower().startswith("up") else "stopped",
        })
    return containers


def list_cld_containers(kind: str | None = None) -> list[dict]:
    """Enumerate cld containers, returning ``{name, kind, repo, status}`` records.

    Host: reads the local daemon. Inside a container: the broker's
    ``list-containers`` action. *kind* filters to one ``org.cld.kind`` label value:
    ``"master"``, ``"agent"`` or ``"task-agent"``.
    """
    if not in_master_container():
        return _list_via_local_docker(kind)

    result = run_action("list-containers", *([kind] if kind else []), capture=True)
    if result.returncode != 0:
        log.warning("broker list-containers failed: %s", (result.stderr or "").strip())
        return []
    records = []
    for line in result.stdout.strip().splitlines():
        rec = _parse_container_line(line)
        if rec:
            records.append(rec)
    return records


def broker_agent_op(target: str, op: str, extra_args: list[str] | None = None) -> int:
    """Delegate a `cld agent <op>` for *target* to the host broker.

    Only meaningful in a container (the host runs `cld agent` directly). Streams the
    broker-invoked `cld agent` output to the user and returns its exit code. *op* is
    one of start/restart/shutdown/status/logs; *extra_args* are forwarded verbatim
    (e.g. ``-m``/``-r`` for start, ``--all`` for shutdown).
    """
    return run_action("agent", target, op, *(extra_args or [])).returncode


def broker_task_agent_op(target: str, op: str, extra_args: list[str] | None = None) -> int:
    """Delegate a `cld task-agent <op>` for *target* to the host broker.

    Same seam as ``broker_agent_op``, separate action: the broker's task-agent action
    has its own op set and enforces the argv rules a container must not be able to
    bypass -- it denies ``--force`` and a caller-supplied ``--parent``, and stamps the
    calling master's session as the parent itself (see broker/cld-broker.sh).
    """
    return run_action("task-agent", target, op, *(extra_args or [])).returncode
