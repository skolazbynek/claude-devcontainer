"""Host Docker access seam: local daemon on the host, host broker inside master.

On the host, cld talks to the Docker daemon directly. Inside a `cld master`
container there is no docker socket (removed by design -- see
docs/design-host-test-running.md); the only host channel is the SSH broker,
reached via the `host-run` wrapper installed by container-init.sh. This module
centralizes that host-vs-broker decision so call sites stay backend-agnostic.

The set of functions here is deliberately the entire surface reachable from
inside a container -- each maps 1:1 to a broker action in
`host-broker/host-broker.sh`. Anything not exposed here simply cannot be done
from inside master, by construction.
"""

import shutil
import subprocess
from pathlib import Path

from cld.docker import in_master_container
from cld.log import get_logger

log = get_logger(__name__)

# container-init.sh installs the wrapper here (only when the broker is
# configured). Prefer the absolute path so callers with a minimal PATH -- e.g.
# the messenger MCP server subprocess -- still find it; its presence doubles as
# the broker-availability signal.
_HOST_RUN = "/tmp/bin/host-run"


def broker_available() -> bool:
    """True when the host-run broker wrapper is installed in this container."""
    return Path(_HOST_RUN).exists() or shutil.which("host-run") is not None


def _host_run_bin() -> str:
    return _HOST_RUN if Path(_HOST_RUN).exists() else "host-run"


def _run_host_run(action: str, *args: str, capture: bool) -> subprocess.CompletedProcess:
    """Invoke the in-container `host-run` wrapper for *action* with *args*.

    `host-run --action <action> <args...>` base64-encodes the argv and ships it
    to the broker over SSH (see container-init.sh / host-broker.sh). With
    ``capture=False`` stdout/stderr are inherited so the broker's output streams
    straight to the user (lifecycle ops); with ``capture=True`` stdout is
    captured for parsing (enumeration).
    """
    cmd = [_host_run_bin(), "--action", action, *args]
    log.debug("host_docker: dispatching to broker: %s", " ".join(cmd))
    try:
        return subprocess.run(cmd, capture_output=capture, text=True)
    except FileNotFoundError:
        # `host-run` is installed only when the host broker is configured
        # (master-only, stage_host_broker). Without it there is no host channel.
        log.warning(
            "host-run wrapper not found -- the host broker is not configured for "
            "this container, so '%s' cannot reach the host.", action,
        )
        return subprocess.CompletedProcess(cmd, 127, stdout="", stderr="host-run not available")


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

    Host: reads the local daemon. Inside master: routes through the broker's
    ``list-containers`` action. *kind* filters to ``"agent"``/``"master"``.
    """
    if not in_master_container():
        return _list_via_local_docker(kind)

    result = _run_host_run("list-containers", *( [kind] if kind else [] ), capture=True)
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

    Only meaningful inside master (the host runs `cld agent` directly). Streams
    the broker-invoked `cld agent` output to the user and returns its exit code.
    *op* is one of start/restart/shutdown/status/logs; *extra_args* are forwarded
    verbatim (e.g. ``-m``/``-r`` for start, ``--all`` for shutdown).
    """
    result = _run_host_run("agent", target, op, *(extra_args or []), capture=False)
    return result.returncode


def broker_task_agent_op(target: str, op: str, extra_args: list[str] | None = None) -> int:
    """Delegate a `cld task-agent <op>` for *target* to the host broker.

    Same seam as ``broker_agent_op``, separate action: the broker's task-agent action
    has its own op set and enforces the argv rules a container must not be able to
    bypass -- it denies ``--force`` and a caller-supplied ``--parent``, and stamps the
    calling master's session as the parent itself (see host-broker/host-broker.sh).
    """
    result = _run_host_run("task-agent", target, op, *(extra_args or []), capture=False)
    return result.returncode
