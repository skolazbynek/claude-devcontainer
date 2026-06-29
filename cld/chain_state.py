"""Chain run state serialisation for detached background execution."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass
class ChainState:
    schema_version: int
    kind: str
    chain_name: str
    chain_session: str
    chain_branch: str
    chain_file: str
    anchor_hash: str
    pid: int
    started_at: str
    finished_at: str | None
    log_file: str
    status: str          # running, success, failed, interrupted
    total_steps: int
    current_index: int
    current_kind: str    # pending, step, parallel
    current_step_name: str
    current_step_sessions: list[str]
    completed_steps: list[dict[str, Any]]
    total_cost_usd: float
    failure_reason: str
    inputs: dict[str, str]

    @classmethod
    def load(cls, path: Path) -> "ChainState":
        data = json.loads(path.read_text())
        return cls(**data)

    def to_dict(self) -> dict:
        return asdict(self)


def write_state(path: Path, data: dict) -> None:
    """Atomically rewrite state.json."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


class StateWriter:
    """Single writer bound to a specific state.json path."""

    def __init__(self, path: Path, initial: ChainState) -> None:
        self._path = path
        self._state = initial

    def _save(self) -> None:
        write_state(self._path, self._state.to_dict())

    def update(self, **fields) -> None:
        for k, v in fields.items():
            object.__setattr__(self._state, k, v)
        self._save()

    def set_anchor(self, anchor_hash: str) -> None:
        self.update(anchor_hash=anchor_hash)

    def mark_step_start(
        self,
        idx: int,
        kind: str,
        name: str,
        sessions: list[str],
    ) -> None:
        self.update(
            current_index=idx,
            current_kind=kind,
            current_step_name=name,
            current_step_sessions=sessions,
        )

    def mark_step_done(
        self,
        name: str,
        status: str,
        duration: float,
        cost: float,
    ) -> None:
        self._state.completed_steps.append({
            "name": name,
            "status": status,
            "duration_seconds": round(duration, 1),
            "cost_usd": round(cost, 6),
        })
        self._state.total_cost_usd = round(
            sum(s["cost_usd"] for s in self._state.completed_steps), 6
        )
        self._save()

    def mark_finished(self, status: str, reason: str = "") -> None:
        self.update(
            status=status,
            finished_at=_utcnow_iso(),
            failure_reason=reason,
            current_kind="done",
        )


def _utcnow_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
