"""Filesystem mailbox transport shared by the messenger MCP server and the agent supervisor.

Layout under a mailbox root (``~/.cld/mailboxes`` by default, see ``cld.config``):

    <root>/<container_name>/tmp/       -- write-here-first staging
    <root>/<container_name>/inbox/     -- unread messages, one <id>.json per message
    <root>/<container_name>/archive/   -- dealt-with messages
    <root>/<container_name>/outbox.log -- append-only audit trail of sent messages

All mutations are same-filesystem ``rename()`` calls, so no reader ever observes
a partial write. Pure filesystem + subprocess (docker) code -- no MCP/FastMCP
imports here so it stays unit-testable with ``tmp_path``.
"""

import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cld.log import get_logger

log = get_logger(__name__)

_TMP = "tmp"
_INBOX = "inbox"
_ARCHIVE = "archive"
_OUTBOX_LOG = "outbox.log"


def _now_iso() -> str:
    # Microsecond precision (still valid RFC3339) so list_inbox's sort-by-ts
    # stays correctly ordered even when two messages land in the same second.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def mailbox_dir(root: Path, name: str) -> Path:
    """Return the mailbox directory for container *name* under *root*."""
    return root / name


def ensure_mailbox(root: Path, name: str) -> None:
    """Create ``tmp/``, ``inbox/``, ``archive/`` under the mailbox for *name*."""
    base = mailbox_dir(root, name)
    for sub in (_TMP, _INBOX, _ARCHIVE):
        (base / sub).mkdir(parents=True, exist_ok=True)


def write_message(root: Path, frm: str, to: str, subject: str, body: str) -> dict:
    """Atomically deliver a message into *to*'s inbox; record it in *frm*'s outbox log.

    Writes ``<to>/tmp/<id>.json`` then ``rename()``s into ``<to>/inbox/<id>.json``.
    Returns the full message dict (including the generated ``id`` and ``ts``).
    """
    ensure_mailbox(root, to)
    ensure_mailbox(root, frm)

    msg = {
        "id": uuid.uuid4().hex,
        "from": frm,
        "to": to,
        "subject": subject,
        "body": body,
        "ts": _now_iso(),
    }

    to_dir = mailbox_dir(root, to)
    tmp_path = to_dir / _TMP / f"{msg['id']}.json"
    inbox_path = to_dir / _INBOX / f"{msg['id']}.json"
    tmp_path.write_text(json.dumps(msg, indent=2))
    tmp_path.rename(inbox_path)
    log.info("delivered message %s: %s -> %s (%s)", msg["id"], frm, to, subject)

    outbox_path = mailbox_dir(root, frm) / _OUTBOX_LOG
    with outbox_path.open("a") as f:
        f.write(json.dumps({"id": msg["id"], "to": to, "ts": msg["ts"]}) + "\n")

    return msg


def _read_dir_messages(dir_path: Path) -> list[dict]:
    messages = []
    if not dir_path.is_dir():
        return messages
    for f in dir_path.glob("*.json"):
        try:
            messages.append(json.loads(f.read_text()))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("skipping unreadable mailbox file %s: %s", f, e)
    return messages


def list_inbox(root: Path, name: str, unread_only: bool = True) -> list[dict]:
    """List messages in *name*'s inbox (unread) or archive, sorted by ``ts``.

    Each entry is ``{id, from, subject, ts}``.
    """
    sub = _INBOX if unread_only else _ARCHIVE
    messages = _read_dir_messages(mailbox_dir(root, name) / sub)
    messages.sort(key=lambda m: m.get("ts", ""))
    return [{"id": m["id"], "from": m["from"], "subject": m["subject"], "ts": m["ts"]} for m in messages]


def read_message(root: Path, name: str, msg_id: str) -> dict | None:
    """Full read of one message by id. Searches inbox, then archive."""
    base = mailbox_dir(root, name)
    for sub in (_INBOX, _ARCHIVE):
        path = base / sub / f"{msg_id}.json"
        if path.is_file():
            try:
                return json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as e:
                log.warning("could not read message %s: %s", path, e)
                return None
    return None


def archive_message(root: Path, name: str, msg_id: str) -> bool:
    """Move ``inbox/<id>.json`` to ``archive/<id>.json``. No-op (True) if already archived."""
    base = mailbox_dir(root, name)
    archive_path = base / _ARCHIVE / f"{msg_id}.json"
    if archive_path.is_file():
        return True
    inbox_path = base / _INBOX / f"{msg_id}.json"
    if not inbox_path.is_file():
        return False
    (base / _ARCHIVE).mkdir(parents=True, exist_ok=True)
    inbox_path.rename(archive_path)
    log.info("archived message %s for %s", msg_id, name)
    return True


def oldest_inbox_id(root: Path, name: str) -> str | None:
    """Return the id of the oldest unread message (min mtime) for *name*, or None."""
    inbox = mailbox_dir(root, name) / _INBOX
    if not inbox.is_dir():
        return None
    files = list(inbox.glob("*.json"))
    if not files:
        return None
    oldest = min(files, key=lambda f: f.stat().st_mtime)
    return oldest.stem


def outbox_snapshot(root: Path, name: str) -> int:
    """Return the current line count of *name*'s outbox.log (0 if it doesn't exist yet)."""
    path = mailbox_dir(root, name) / _OUTBOX_LOG
    if not path.is_file():
        return 0
    with path.open() as f:
        return sum(1 for _ in f)


def outbox_changed_since(root: Path, name: str, snapshot: int) -> bool:
    """True if *name* has sent at least one message since *snapshot* was taken."""
    return outbox_snapshot(root, name) > snapshot


def list_containers(kind: str | None = None) -> list[dict]:
    """Enumerate cld containers via Docker labels.

    *kind* filters to ``"agent"`` or ``"master"``; omit for both. Each entry is
    ``{name, kind, repo, status}``. Uses ``docker ps -a`` so stopped masters are
    included (agent containers run ``--rm`` and disappear once exited).
    """
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


def resolve_recipient(to: str, containers: list[dict] | None = None) -> str:
    """Resolve a shortname (repo basename) or full container name to a full container name.

    Prefers an ``agent`` over a ``master`` when both exist for the same basename.
    Raises ValueError if *to* is a shortname matching containers from two different
    repo roots (ambiguous), or if it isn't found at all.
    """
    all_containers = containers if containers is not None else list_containers()

    for c in all_containers:
        if c["name"] == to:
            return to

    matches = [c for c in all_containers if Path(c["repo"]).name == to]
    if not matches:
        raise ValueError(f"No container found for '{to}' (not a known shortname or container name)")

    repos = {c["repo"] for c in matches}
    if len(repos) > 1:
        raise ValueError(
            f"Ambiguous shortname '{to}': matches containers from multiple repos: {sorted(repos)}"
        )

    for c in matches:
        if c["kind"] == "agent":
            return c["name"]
    return matches[0]["name"]
