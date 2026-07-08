"""Agent supervisor daemon: ``python -m cld.messenger.agent_loop``.

State machine: KICKOFF (once, on process start) -> IDLE (poll inbox every
``poll_interval`` seconds) -> PROCESSING (one message, strict FIFO) -> IDLE,
until SIGTERM.

Empirically verified against a live Claude Code 2.1.198 invocation (see
docs/design-agent-messaging.md #19): `claude -p --output-format json` returns
a flat object with `session_id`, `result` (final assistant text), and
`total_cost_usd` -- not `cost_usd` as some older stub fixtures in this repo
assume. `_extract_cost` below accepts either, preferring `total_cost_usd`.
`--max-turns` and `--resume` are both accepted despite `--max-turns` being
absent from `claude --help` output in this build.
"""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from string import Template

from cld.config import Config
from cld.docker import MAILBOX_MOUNT
from cld.log import get_logger, setup_logging
from cld.messenger import mailbox

log = get_logger(__name__)

_DEFAULT_POLL_INTERVAL = 1.0
_CLD_ROOT = Path("/opt/cld")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_cost(data: dict) -> float:
    for key in ("total_cost_usd", "cost_usd"):
        value = data.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0.0


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(path)


class AgentSupervisor:
    """Owns the one persistent Claude session for a repo agent container."""

    def __init__(
        self,
        *,
        session_name: str,
        repo_root: Path,
        mailbox_root: Path,
        persona_path: Path,
        model: str = "",
        max_turns: int = 30,
        claude_bin: str = "claude",
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
    ):
        self.session_name = session_name
        self.repo_root = repo_root
        self.mailbox_root = mailbox_root
        self.persona_path = persona_path
        self.model = model
        self.max_turns = max_turns
        self.claude_bin = claude_bin
        self.poll_interval = poll_interval

        self.session_id: str | None = None
        self.msg_count = 0
        self.cost_usd_total = 0.0
        self.started_at = _now_iso()
        self._stop = False

        mailbox.ensure_mailbox(mailbox_root, session_name)
        self.state_path = mailbox.mailbox_dir(mailbox_root, session_name) / "state.json"

    def _write_state(self, phase: str, current: dict | None = None) -> None:
        state = {
            "container_name": self.session_name,
            "repo_root": str(self.repo_root),
            "phase": phase,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "msg_count": self.msg_count,
            "cost_usd_total": round(self.cost_usd_total, 6),
            "current": current,
        }
        _atomic_write_json(self.state_path, state)

    def _run_claude(self, prompt: str, *, resume: bool) -> dict:
        cmd = [
            self.claude_bin, "-p",
            "--output-format", "json",
            "--max-turns", str(self.max_turns),
            "--dangerously-skip-permissions",
        ]
        if resume:
            if not self.session_id:
                raise RuntimeError("cannot --resume: no session_id recorded from kickoff")
            cmd += ["--resume", self.session_id]
        if self.model:
            cmd += ["--model", self.model]

        log.info("invoking claude (resume=%s, prompt_size=%d): %s", resume, len(prompt), " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True, input=prompt, cwd=str(self.repo_root))
        if result.returncode != 0:
            # In --output-format json, claude reports errors as a JSON envelope on stdout;
            # stderr is usually empty. Emit both streams to the container log before raising
            # so `docker logs` / `cld agent logs` has the full context.
            log.error("claude exited %d\ncmd: %s\nstdout:\n%s\nstderr:\n%s",
                      result.returncode, " ".join(cmd), result.stdout, result.stderr)
            raise RuntimeError(
                f"claude exited {result.returncode}\n"
                f"stderr: {result.stderr[-2000:]}\n"
                f"stdout: {result.stdout[-4000:]}"
            )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as e:
            log.error("could not parse claude JSON output (%s)\ncmd: %s\nstdout:\n%s\nstderr:\n%s",
                      e, " ".join(cmd), result.stdout, result.stderr)
            raise RuntimeError(f"could not parse claude JSON output ({e}): {result.stdout[-2000:]}")

    def kickoff(self) -> None:
        self._write_state("kickoff")
        prompt = Template(self.persona_path.read_text()).safe_substitute(
            REPO_BASENAME=self.repo_root.name,
            REPO_ABS_PATH=str(self.repo_root),
            MAX_TURNS=str(self.max_turns),
            CONTAINER_NAME=self.session_name,
            AGENT_ANCHOR_HASH=os.environ.get("AGENT_ANCHOR_HASH", ""),
        )
        data = self._run_claude(prompt, resume=False)
        self.session_id = data.get("session_id")
        self.cost_usd_total += _extract_cost(data)
        log.info("kickoff complete: session_id=%s, cost_so_far=$%.4f", self.session_id, self.cost_usd_total)
        self._write_state("idle")

    def process_one(self, msg_id: str) -> None:
        msg = mailbox.read_message(self.mailbox_root, self.session_name, msg_id)
        if msg is None:
            log.warning("message %s vanished before processing -- skipping", msg_id)
            return

        snapshot = mailbox.outbox_snapshot(self.mailbox_root, self.session_name)
        self._write_state("processing", current={
            "id": msg["id"],
            "from": msg["from"],
            "subject": msg["subject"],
            "started_at": _now_iso(),
        })
        log.info("processing message %s from %s: %s", msg["id"], msg["from"], msg["subject"])

        prompt = (
            f"New message from {msg['from']} (id: {msg['id']}):\n"
            f"Subject: {msg['subject']}\n\n{msg['body']}"
        )

        try:
            data = self._run_claude(prompt, resume=True)
        except RuntimeError as e:
            log.error("claude invocation failed for message %s: %s", msg_id, e)
            mailbox.write_message(
                self.mailbox_root, self.session_name, msg["from"],
                f"Re: {msg['subject']}", f"failed: {e}",
            )
        else:
            self.cost_usd_total += _extract_cost(data)
            if not mailbox.outbox_changed_since(self.mailbox_root, self.session_name, snapshot):
                last_text = data.get("result", "")
                log.warning("message %s: no reply sent by Claude -- synthesizing fallback", msg_id)
                mailbox.write_message(
                    self.mailbox_root, self.session_name, msg["from"],
                    f"Re: {msg['subject']}",
                    f"(no reply produced; last text: {last_text})",
                )

        mailbox.archive_message(self.mailbox_root, self.session_name, msg_id)
        self.msg_count += 1
        self._write_state("idle")

    def request_stop(self, *_args) -> None:
        log.info("stop requested (SIGTERM)")
        self._stop = True

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.request_stop)
        self.kickoff()
        log.info("supervisor idle, polling %s every %.1fs", self.session_name, self.poll_interval)
        while not self._stop:
            msg_id = mailbox.oldest_inbox_id(self.mailbox_root, self.session_name)
            if msg_id is None:
                time.sleep(self.poll_interval)
                continue
            self.process_one(msg_id)
        self._forget_session_bookmark()
        self._write_state("stopped")
        log.info("supervisor stopped cleanly")

    def _forget_session_bookmark(self) -> None:
        """Peer self-cleanup: drop the session bookmark from the origin store on
        exit so `cld agent shutdown` yields a fresh lifecycle on next start.
        See docs/design-master-sibling-launch.md (Shutdown / bookmark cleanup).
        """
        origin = os.environ.get("WORKSPACE_ORIGIN", "/workspace/origin")
        try:
            result = subprocess.run(
                ["jj", "bookmark", "forget", self.session_name],
                cwd=origin, capture_output=True, text=True,
            )
        except OSError as e:
            log.warning("could not run jj bookmark forget: %s", e)
            return
        if result.returncode != 0:
            log.warning(
                "jj bookmark forget %s failed (rc=%d): %s",
                self.session_name, result.returncode, (result.stderr or "").strip(),
            )


def main() -> None:
    cfg = Config.from_env()
    setup_logging(cfg, force_stderr=True)

    session_name = os.environ.get("SESSION_NAME", "")
    if not session_name:
        log.error("SESSION_NAME must be set")
        sys.exit(1)

    repo_root = Path(os.environ.get("WORKSPACE_CURRENT", "/workspace/current"))
    persona_path = _CLD_ROOT / "prompts" / "personas" / f"{cfg.agent_kickoff_persona}.md"
    if not persona_path.is_file():
        log.error("kickoff persona not found: %s", persona_path)
        sys.exit(1)

    supervisor = AgentSupervisor(
        session_name=session_name,
        repo_root=repo_root,
        mailbox_root=Path(MAILBOX_MOUNT),
        persona_path=persona_path,
        model=os.environ.get("AGENT_MODEL", ""),
        max_turns=cfg.agent_max_turns,
    )
    supervisor.run()


if __name__ == "__main__":
    main()
