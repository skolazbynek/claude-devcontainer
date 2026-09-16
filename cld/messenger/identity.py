"""Resolve ``(own_container_name, mailbox_root)`` for a messenger CLI verb.

In a cld container the identity is trivial: ``SESSION_NAME`` and the fixed
``MAILBOX_MOUNT``. On the host we act *as* a container -- reads and writes
target that mailbox, and outgoing messages list it as the sender. Which one
(design-ticket-containers.md section 6.5): an explicit ticket (``CLD_TICKET``
env or the *ticket* argument) wins; else the single running ticket container,
if there is exactly one; else the v1 fallback, the cwd repo's master. A
cwd-walk mapping cwd to tickets is deliberately not attempted -- several
tickets can mount one repo, so cwd is not an identity.
"""

import os
from pathlib import Path

from cld.broker import list_cld_containers
from cld.config import Config
from cld.docker import MAILBOX_MOUNT, master_container_name, ticket_container_name
from cld.vcs import get_backend


def resolve_self(ticket: str = "") -> tuple[str, Path]:
    session = os.environ.get("SESSION_NAME", "")
    if session:
        return session, Path(MAILBOX_MOUNT)

    mailbox_root = Path(Config.from_env().mailbox_root).expanduser()
    ticket = ticket or os.environ.get("CLD_TICKET", "")
    if ticket:
        return ticket_container_name(ticket), mailbox_root

    running = [c["name"] for c in list_cld_containers("ticket") if c["status"] == "running"]
    if len(running) == 1:
        return running[0], mailbox_root

    try:
        repo_root = get_backend().repo_root
    except RuntimeError:
        listing = ", ".join(running) if running else "none"
        raise RuntimeError(
            "cannot resolve messenger identity: cwd is not a repo and "
            f"{len(running)} ticket containers are running ({listing}) -- "
            "set CLD_TICKET=<ticket> to act as one"
        ) from None
    return master_container_name(repo_root), mailbox_root
