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
def send(to: str, subject: str, body: str) -> dict:
    """Send a message to another container's mailbox.

    to: shortname (repo basename) or full container name. Resolved against
    running/known cld containers; an agent is preferred over a master when
    both exist for the same basename.
    """
    log.info("MCP tool: send (to=%s, subject=%s)", to, subject)
    try:
        frm = _own_name()
        resolved = mailbox.resolve_recipient(to)
        msg = mailbox.write_message(_mailbox_root(), frm, resolved, subject, body)
        return {"id": msg["id"]}
    except (RuntimeError, ValueError) as e:
        return {"error": str(e)}


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
def list_agents(kind: str = "") -> list[dict]:
    """List cld containers via Docker labels. kind: 'agent' or 'master'; omit for both."""
    log.info("MCP tool: list_agents (kind=%s)", kind or "<all>")
    return mailbox.list_containers(kind or None)


if __name__ == "__main__":
    setup_logging(Config.from_env(), force_stderr=True)
    log.info("messenger MCP server starting")
    mcp.run()
