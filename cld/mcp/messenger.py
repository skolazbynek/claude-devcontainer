"""MCP server exposing the mailbox transport: send / list_inbox / read_message / archive / list_agents.

Every tool operates on the *calling* container's own mailbox, identified by
the ``SESSION_NAME`` env var (already set for both master and agent
containers by the launcher/entrypoint).
"""

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from cld.config import Config
from cld.docker import MAILBOX_MOUNT
from cld.log import setup_logging, get_logger
from cld.messenger import mailbox

log = get_logger(__name__)

mcp = FastMCP("messenger")


def _own_name() -> str:
    name = os.environ.get("SESSION_NAME", "")
    if not name:
        raise RuntimeError("SESSION_NAME is not set -- cannot determine mailbox identity")
    return name


def _mailbox_root() -> Path:
    """In-container mount point of the shared mailbox tree (see cld.docker.MAILBOX_MOUNT).

    Not ``cfg.mailbox_root`` -- that field is the *host-side* source path used
    only when building the ``-v`` mount; inside the container the mailbox is
    always bind-mounted at this fixed path.
    """
    return Path(MAILBOX_MOUNT)


@mcp.tool()
def send(to: str, subject: str, body: str, expects_reply: bool = False, answers: str = "") -> dict:
    """Send a message to another container's mailbox.

    to: a full container name, or a repo-basename shortname when exactly one agent or
    master owns that repo. Peers and task-agents must be addressed by full name -- with
    several task-agents per repo a basename identifies nothing.

    expects_reply: set it only when you cannot proceed without an answer. It obliges the
    recipient to reply, and the recipient is obliged by nothing else -- so a message you
    send without it will not be acknowledged, which is the point. Prefer stating an
    assumption over asking.

    answers: the id of the message you are answering. Set it whenever you reply, so the
    question it asked is recorded as settled. A reply may set expects_reply too (answering
    one thing while asking another is normal); an acknowledgment should set neither.

    On an agent-to-agent edge the message counts against that edge's hop budget, and the
    return carries {"hops": n, "limit": m}, plus {"open_asks", "ask_limit"} while
    anything on it is unanswered. Two refusals, both {"error": ...}: past the hop limit
    nothing is delivered over the edge at all, and past the ask limit a message with
    expects_reply is turned away while answers and plain informs still go through. Either
    way, escalate to your master instead of retrying -- that channel is never budgeted.
    """
    log.info("MCP tool: send (to=%s, subject=%s, expects_reply=%s)", to, subject, expects_reply)
    try:
        frm = _own_name()
    except RuntimeError as e:
        return {"error": str(e)}
    cfg = Config.from_env()
    return mailbox.gated_send(
        _mailbox_root(), frm, to, subject, body,
        default_limit=cfg.peer_absolute_limit,
        ask_limit=cfg.root_ask_limit,
        answers=answers,
        expects_reply=expects_reply,
    )


@mcp.tool()
def list_inbox(unread_only: bool = True) -> list[dict]:
    """List own inbox (unread_only=True, default) or archive, sorted by ts."""
    log.info("MCP tool: list_inbox (unread_only=%s)", unread_only)
    try:
        name = _own_name()
    except RuntimeError as e:
        return [{"error": str(e)}]
    return mailbox.list_inbox(_mailbox_root(), name, unread_only=unread_only)


@mcp.tool()
def read_message(id: str) -> dict:
    """Full read of one message by id. Searches inbox, then archive."""
    log.info("MCP tool: read_message (id=%s)", id)
    try:
        name = _own_name()
    except RuntimeError as e:
        return {"error": str(e)}
    msg = mailbox.read_message(_mailbox_root(), name, id)
    if msg is None:
        return {"error": f"Message not found: {id}"}
    return msg


@mcp.tool()
def archive(id: str) -> dict:
    """Move a message from inbox to archive. No-op if already archived."""
    log.info("MCP tool: archive (id=%s)", id)
    try:
        name = _own_name()
    except RuntimeError as e:
        return {"error": str(e)}
    if not mailbox.archive_message(_mailbox_root(), name, id):
        return {"error": f"Message not found in inbox: {id}"}
    return {"ok": True}


@mcp.tool()
def fleet_digest() -> list[dict]:
    """One cheap row per task-agent you spawned: who moved, how far, what it cost.

    Call this at the start of a turn to reconcile your fleet, then read_mailbox() only
    for the members whose msg_count or last_activity changed since you last looked.
    Rows are {name, task, phase, msg_count, cost_usd_total, unread, last_activity} --
    no message bodies, so it stays cheap enough to call every turn -- plus
    {open_asks, open_with, oldest_open}: questions this agent's peer edges are still
    waiting on. A rising open_asks with an old oldest_open is a stalling exchange,
    usually because the task you gave one of them was under-specified; step in and rule
    on it rather than waiting for the ask budget to refuse them.

    Scoped to agents whose recorded parent is you; empty if you have no fleet.
    """
    log.info("MCP tool: fleet_digest")
    try:
        name = _own_name()
    except RuntimeError as e:
        return [{"error": str(e)}]
    return mailbox.fleet_digest(_mailbox_root(), name)


@mcp.tool()
def read_mailbox(name: str, since: str = "") -> list[dict]:
    """Full exchange for one of your task-agents: what it received and what it sent.

    Received covers inbox/ *and* archive/ -- an agent archives each message within about
    a second of processing it, so anything else would show almost nothing. Sent comes
    from its outbox log, so peer-to-peer traffic on edges you drew is visible here too.
    Entries are oldest-first and carry a `hops` stamp on budgeted edges.

    since: exclusive -- pass the `ts` of the last entry you saw to get only what is new.
    Only mailboxes whose recorded parent is you can be read; a reaped agent's archived
    mailbox still can be.
    """
    log.info("MCP tool: read_mailbox (name=%s, since=%s)", name, since or "<all>")
    try:
        own = _own_name()
    except RuntimeError as e:
        return [{"error": str(e)}]
    root = _mailbox_root()
    meta = mailbox.read_meta_resolved(root, name)
    if meta is None:
        return [{"error": f"No spawn facts for '{name}' -- not a task-agent mailbox"}]
    if meta.get("parent") != own:
        return [{"error":
                 f"'{name}' is not in your fleet (its parent is "
                 f"{meta.get('parent') or '<none>'})"}]
    entries = mailbox.transcript(root, name)
    return [e for e in entries if e.get("ts", "") > since] if since else entries


@mcp.tool()
def list_agents(kind: str = "") -> list[dict]:
    """List cld containers via Docker labels.

    kind: 'master', 'agent' (the standing per-repo agent) or 'task-agent' (one per task,
    see docs/design-task-agents.md); omit for all of them.
    """
    log.info("MCP tool: list_agents (kind=%s)", kind or "<all>")
    return mailbox.list_containers(kind or None)


if __name__ == "__main__":
    setup_logging(Config.from_env(), force_stderr=True)
    log.info("messenger MCP server starting")
    mcp.run()
