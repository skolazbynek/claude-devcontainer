"""Durable bridge state: poll cursor, seen posts, thread map, outstanding deliveries.

Everything here has to survive a restart. Without the cursor and ``seen_post_ids``
the bridge replays the channel and re-runs yesterday's tasks; without ``threads``
and ``sent`` a reply that arrives after a restart opens a new thread instead of
landing in the conversation it answers.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cld.log import get_logger

log = get_logger(__name__)

_VERSION = 1
# Ring caps: the file is rewritten every tick, so it must not grow without bound.
_SEEN_CAP = 2000
_SENT_CAP = 500

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime(_ISO_FMT)


@dataclass
class BridgeState:
    """The bridge's memory between ticks. Mutated in place, then ``save()``d."""

    path: Path
    cursor_ms: int = 0
    seen_post_ids: list[str] = field(default_factory=list)
    threads: dict[str, dict] = field(default_factory=dict)
    sent: dict[str, dict] = field(default_factory=dict)
    outstanding: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "BridgeState":
        path = path.expanduser()
        if not path.is_file():
            return cls(path=path)
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            # Starting from scratch replays the channel; refusing to start is worse
            # than the duplicate, but the operator has to know which happened.
            log.error("unreadable bridge state at %s (%s) -- starting from scratch", path, e)
            return cls(path=path)
        if data.get("version") != _VERSION:
            log.warning("bridge state version %s != %d -- starting from scratch", data.get("version"), _VERSION)
            return cls(path=path)
        return cls(
            path=path,
            cursor_ms=int(data.get("cursor_ms", 0)),
            seen_post_ids=list(data.get("seen_post_ids", [])),
            threads=dict(data.get("threads", {})),
            sent=dict(data.get("sent", {})),
            outstanding=dict(data.get("outstanding", {})),
        )

    def save(self) -> None:
        self.seen_post_ids = self.seen_post_ids[-_SEEN_CAP:]
        if len(self.sent) > _SENT_CAP:
            # dicts keep insertion order, and insertion order is send order.
            for key in list(self.sent)[: len(self.sent) - _SENT_CAP]:
                if key not in self.outstanding:
                    del self.sent[key]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({
            "version": _VERSION,
            "cursor_ms": self.cursor_ms,
            "seen_post_ids": self.seen_post_ids,
            "threads": self.threads,
            "sent": self.sent,
            "outstanding": self.outstanding,
        }, indent=2))
        tmp.rename(self.path)

    def seen(self, post_id: str) -> bool:
        return post_id in self.seen_post_ids

    def mark_seen(self, post_id: str) -> None:
        if post_id not in self.seen_post_ids:
            self.seen_post_ids.append(post_id)

    def agent_for_thread(self, root_post_id: str) -> str | None:
        entry = self.threads.get(root_post_id)
        return entry["agent"] if entry else None

    def bind_thread(self, root_post_id: str, agent: str) -> None:
        """Make replies in this thread go to *agent* -- also for threads an agent opened."""
        self.threads.setdefault(root_post_id, {"agent": agent, "opened_at": now_iso()})

    def record_sent(self, msg_id: str, root_post_id: str, agent: str) -> None:
        """Remember a delivery: its thread, and that we are still owed a reply."""
        self.bind_thread(root_post_id, agent)
        self.sent[msg_id] = {"root_post_id": root_post_id, "agent": agent, "ts": now_iso()}
        self.outstanding[msg_id] = {"agent": agent, "sent_at": now_iso(), "notified": False}

    def thread_for_reply(self, answers: str) -> str:
        """Root post id of the conversation *answers* belongs to ("" if unknown)."""
        entry = self.sent.get(answers)
        return entry["root_post_id"] if entry else ""

    def discharge(self, answers: str) -> None:
        self.outstanding.pop(answers, None)
