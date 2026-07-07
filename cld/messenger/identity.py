"""Resolve ``(own_container_name, mailbox_root)`` for a messenger CLI verb.

In a cld container the identity is trivial: ``SESSION_NAME`` and the fixed
``MAILBOX_MOUNT``. On the host we act *as* the master container of the caller's
current-cwd repo -- reads and writes target that mailbox, and outgoing messages
list the master as the sender.
"""

import os
from pathlib import Path

from cld.config import Config
from cld.docker import MAILBOX_MOUNT, find_repo_root, master_container_name


def resolve_self() -> tuple[str, Path]:
    session = os.environ.get("SESSION_NAME", "")
    if session:
        return session, Path(MAILBOX_MOUNT)

    repo_root = find_repo_root()
    name = master_container_name(repo_root)
    mailbox_root = Path(Config.from_env().mailbox_root).expanduser()
    return name, mailbox_root
