"""Who can answer, and who cannot -- the bridge's pre-flight check (plan §5).

A mailbox directory is not an agent. The root holds masters (no supervisor, so
they never reply), crashed containers (mailbox intact, container gone), reaped
agents (moved under ``_archive/``) and the bridge's own mailbox. Delivering into
any of those is a message that will never be answered, so the bridge classifies
before it sends and refuses in-channel with the reason.
"""

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cld.log import get_logger
from cld.messenger import mailbox

log = get_logger(__name__)

READY = "ready"
REAPED = "reaped"
UNKNOWN = "unknown"
UNATTENDED = "unattended"
STOPPED = "stopped"
CRASHED = "crashed"


@dataclass(frozen=True)
class Target:
    """A mailbox and whether a message sent to it can ever come back."""

    name: str
    status: str
    detail: str
    meta: dict | None = None
    state: dict | None = None

    @property
    def ready(self) -> bool:
        return self.status == READY


def running_containers() -> set[str] | None:
    """Names of running cld containers, or None when docker cannot be reached.

    None is a real state, not an error: on a daemon restart we must not conclude
    that every agent crashed and flood the channel with refusals.
    """
    result = subprocess.run(
        ["docker", "ps", "--filter", "label=org.cld.kind", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.warning("docker ps failed, liveness unknown this tick: %s", result.stderr.strip())
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def classify_target(root: Path, name: str, running: set[str] | None) -> Target:
    """Decide whether *name* can answer. First match wins (plan §5)."""
    if mailbox.mailbox_reaped(root, name):
        slug = name.rsplit("_", 1)[-1]
        return Target(name, REAPED, f"reaped -- read its conversation with `cld task-agent transcript {slug}`")

    if not mailbox.mailbox_dir(root, name).is_dir():
        return Target(name, UNKNOWN, "no mailbox by that name")

    state = mailbox.read_state(root, name)
    meta = mailbox.read_meta(root, name)

    if state is None:
        detail = (
            "a master has no supervisor -- it will read this the next time you attach"
            if name.startswith("cld_master_")
            else "supervisor never wrote its state -- check `cld agent logs`"
        )
        return Target(name, UNATTENDED, detail, meta, state)

    if state.get("phase") == "stopped":
        return Target(name, STOPPED, "supervisor exited cleanly", meta, state)

    if running is not None and name not in running:
        last = state.get("phase", "unknown")
        return Target(
            name, CRASHED,
            f"container is gone; its mailbox last said `{last}`. "
            "Work may be recoverable from the origin store",
            meta, state,
        )

    return Target(name, READY, state.get("phase", "unknown"), meta, state)


def _last_activity(base: Path) -> str:
    mtimes = [p.stat().st_mtime for p in (base / "state.json", base / "outbox.log", base / "inbox") if p.exists()]
    if not mtimes:
        return ""
    return datetime.fromtimestamp(max(mtimes), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fleet_rows(root: Path, running: set[str] | None, exclude: str = "") -> list[Target]:
    """Every mailbox on the host, classified. Unscoped by repo (D11).

    Deliberately *not* filtered to live agents: this is also the name-resolution list,
    and a crashed or reaped agent has to stay addressable so that writing to it yields
    the reason it cannot answer rather than "no agent matches". `!fleet` filters for
    display (``render_fleet``); addressing does not.
    """
    if not root.is_dir():
        return []
    names = sorted(
        e.name for e in root.iterdir()
        if e.is_dir() and not e.name.startswith("_") and e.name != exclude
    )
    archived_root = root / "_archive"
    if archived_root.is_dir():
        names += sorted(e.name for e in archived_root.iterdir() if e.is_dir() and e.name not in names)
    return [classify_target(root, n, running) for n in names]


def render_fleet(root: Path, rows: list[Target]) -> str:
    """`!fleet`: the agents you can actually talk to, one block each.

    Live-only. Listing crashed, stopped, reaped and unattended mailboxes turned the
    roster into a graveyard you had to read past to find the two agents that could
    answer -- and `!fleet` exists to let you name one. Addressing a dead agent still
    reports exactly why it cannot answer (``classify_target``), so nothing is hidden
    that you would otherwise have to guess at.

    One block per agent rather than a markdown table: tables wrap badly on mobile.
    """
    live = [t for t in rows if t.ready]
    if not live:
        return (
            "No live agents. Start one with `cld task-agent start` or `cld agent`"
            f"{f' ({len(rows)} mailbox(es) present but none can answer)' if rows else ''}."
        )

    lines = []
    for t in sorted(live, key=lambda r: r.name):
        state = t.state or {}
        inbox = mailbox.mailbox_dir(root, t.name) / "inbox"
        unread = len(list(inbox.glob("*.json"))) if inbox.is_dir() else 0
        head = (
            f"**{t.name}** -- {state.get('phase', '?')}"
            f" -- {state.get('msg_count', 0)} msgs"
            f" -- ${state.get('cost_usd_total', 0.0):.2f}"
        )
        if unread:
            head += f" -- {unread} unread"
        lines.append(head)
        if t.meta and t.meta.get("task"):
            lines.append(f"    {mailbox.task_summary(t.meta['task'], width=120)}")
    return "\n".join(lines)
