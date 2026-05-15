"""Shared agent-lifecycle helpers (wait, cost, formatting)."""

import json
import subprocess
import time

from cld.config import Config
from cld.vcs import VcsBackend


def wait_for_agent(session_name: str, vcs: VcsBackend, cfg: Config) -> dict:
    start = time.monotonic()
    while time.monotonic() - start < cfg.agent_timeout:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name=^{session_name}$", "--format", "{{.Status}}"],
            capture_output=True, text=True,
        )
        if not result.stdout.strip():
            break
        time.sleep(cfg.poll_interval)
    else:
        subprocess.run(["docker", "stop", session_name], capture_output=True, text=True)
        return {"status": "timeout", "session_name": session_name}

    summary_raw = vcs.file_show(
        session_name, f"agent-output-{session_name}/summary.json",
    )
    if not summary_raw:
        return {"status": "unknown", "error": "No summary.json found"}
    try:
        return json.loads(summary_raw)
    except json.JSONDecodeError:
        return {"status": "unknown", "error": "Invalid summary.json"}


def read_agent_cost(session: str, vcs: VcsBackend) -> float | None:
    raw = vcs.file_show(session, f"agent-output-{session}/result.json")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        cost = data.get("cost_usd")
        return float(cost) if cost is not None else None
    except (json.JSONDecodeError, ValueError):
        return None


def format_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"
