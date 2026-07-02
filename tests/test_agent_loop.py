"""Tests for cld.messenger.agent_loop: the repo agent supervisor state machine.

Drives AgentSupervisor bare-metal (no Docker) against a stub `claude` binary
and a temp mailbox root, per the design doc's suggested manual-test approach.
"""

import json
from pathlib import Path

import pytest

from cld.messenger import mailbox
from cld.messenger.agent_loop import AgentSupervisor, _extract_cost

_STUB_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "stub-messenger-agent" / "claude"


class TestExtractCost:
    def test_prefers_total_cost_usd(self):
        assert _extract_cost({"total_cost_usd": 1.5, "cost_usd": 9.9}) == 1.5

    def test_falls_back_to_cost_usd(self):
        assert _extract_cost({"cost_usd": 0.5}) == 0.5

    def test_missing_returns_zero(self):
        assert _extract_cost({}) == 0.0

    def test_non_numeric_falls_through(self):
        assert _extract_cost({"total_cost_usd": "n/a", "cost_usd": 0.25}) == 0.25


@pytest.fixture
def persona_path(tmp_path):
    p = tmp_path / "repo-agent.md"
    p.write_text("Repo: ${REPO_BASENAME} at ${REPO_ABS_PATH}, turns=${MAX_TURNS}, container=${CONTAINER_NAME}\n")
    return p


@pytest.fixture
def supervisor(tmp_path, persona_path, monkeypatch):
    mailbox_root = tmp_path / "mailboxes"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    argv_log = tmp_path / "argv.log"
    monkeypatch.setenv("STUB_ARGV_LOG", str(argv_log))
    monkeypatch.setenv("STUB_MAILBOX_ROOT", str(mailbox_root))
    monkeypatch.setenv("STUB_SESSION_NAME", "cld_agent_repoA")
    sup = AgentSupervisor(
        session_name="cld_agent_repoA",
        repo_root=repo_root,
        mailbox_root=mailbox_root,
        persona_path=persona_path,
        claude_bin=str(_STUB_CLAUDE),
        max_turns=7,
    )
    sup._argv_log = argv_log
    return sup


def _read_argv_log(path: Path) -> list[list[str]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


class TestKickoff:
    def test_sets_session_id_and_cost(self, supervisor, monkeypatch):
        monkeypatch.setenv("STUB_COST", "0.25")
        supervisor.kickoff()
        assert supervisor.session_id == "kickoff-session-id"
        assert supervisor.cost_usd_total == pytest.approx(0.25)

    def test_writes_idle_state(self, supervisor):
        supervisor.kickoff()
        state = json.loads(supervisor.state_path.read_text())
        assert state["phase"] == "idle"
        assert state["session_id"] == "kickoff-session-id"
        assert state["msg_count"] == 0
        assert state["current"] is None

    def test_no_resume_flag_on_first_call(self, supervisor):
        supervisor.kickoff()
        argv = _read_argv_log(supervisor._argv_log)
        assert "--resume" not in argv[0]

    def test_persona_substituted_into_prompt(self, supervisor):
        supervisor.kickoff()
        argv = _read_argv_log(supervisor._argv_log)[0]
        prompt = argv[argv.index("-p") + 1]
        assert "repo" in prompt  # REPO_BASENAME
        assert "turns=7" in prompt
        assert "container=cld_agent_repoA" in prompt

    def test_max_turns_flag_passed(self, supervisor):
        supervisor.kickoff()
        argv = _read_argv_log(supervisor._argv_log)[0]
        assert argv[argv.index("--max-turns") + 1] == "7"


class TestProcessOne:
    def test_reply_sent_no_fallback(self, supervisor, monkeypatch):
        supervisor.kickoff()
        msg = mailbox.write_message(supervisor.mailbox_root, "sender", supervisor.session_name, "hi", "please help")

        monkeypatch.setenv("STUB_SEND_REPLY", "1")
        monkeypatch.setenv("STUB_REPLY_TO", "sender")
        supervisor.process_one(msg["id"])

        sender_inbox = mailbox.list_inbox(supervisor.mailbox_root, "sender")
        assert len(sender_inbox) == 1
        assert sender_inbox[0]["subject"] == "Re: test"

        # Original message archived, not left unread
        assert mailbox.list_inbox(supervisor.mailbox_root, supervisor.session_name) == []
        assert supervisor.msg_count == 1

    def test_uses_resume_flag(self, supervisor):
        supervisor.kickoff()
        msg = mailbox.write_message(supervisor.mailbox_root, "sender", supervisor.session_name, "hi", "body")
        supervisor.process_one(msg["id"])
        argv = _read_argv_log(supervisor._argv_log)[1]
        assert argv[argv.index("--resume") + 1] == "kickoff-session-id"

    def test_no_reply_synthesizes_fallback(self, supervisor, monkeypatch):
        supervisor.kickoff()
        msg = mailbox.write_message(supervisor.mailbox_root, "sender", supervisor.session_name, "hi", "body")

        monkeypatch.setenv("STUB_RESULT_TEXT", "I looked into it but did nothing")
        supervisor.process_one(msg["id"])

        sender_inbox = mailbox.list_inbox(supervisor.mailbox_root, "sender")
        assert len(sender_inbox) == 1
        full = mailbox.read_message(supervisor.mailbox_root, "sender", sender_inbox[0]["id"])
        assert "no reply produced" in full["body"]
        assert "I looked into it but did nothing" in full["body"]

    def test_claude_failure_synthesizes_failure_reply(self, supervisor, monkeypatch):
        supervisor.kickoff()
        msg = mailbox.write_message(supervisor.mailbox_root, "sender", supervisor.session_name, "hi", "body")

        monkeypatch.setenv("STUB_FAIL", "1")
        supervisor.process_one(msg["id"])

        sender_inbox = mailbox.list_inbox(supervisor.mailbox_root, "sender")
        assert len(sender_inbox) == 1
        full = mailbox.read_message(supervisor.mailbox_root, "sender", sender_inbox[0]["id"])
        assert full["body"].startswith("failed:")
        # Message still archived and counted even on failure
        assert mailbox.list_inbox(supervisor.mailbox_root, supervisor.session_name) == []
        assert supervisor.msg_count == 1

    def test_accumulates_cost_across_messages(self, supervisor, monkeypatch):
        monkeypatch.setenv("STUB_COST", "0.10")
        supervisor.kickoff()
        msg1 = mailbox.write_message(supervisor.mailbox_root, "sender", supervisor.session_name, "a", "b")
        monkeypatch.setenv("STUB_COST", "0.20")
        supervisor.process_one(msg1["id"])
        assert supervisor.cost_usd_total == pytest.approx(0.30)

    def test_writes_processing_state_with_current(self, supervisor, monkeypatch, tmp_path):
        supervisor.kickoff()
        msg = mailbox.write_message(supervisor.mailbox_root, "sender", supervisor.session_name, "urgent", "body")

        # Patch _run_claude to snapshot state.json mid-flight before returning.
        captured = {}
        original = supervisor._run_claude

        def spy(prompt, *, resume):
            captured["state"] = json.loads(supervisor.state_path.read_text())
            return original(prompt, resume=resume)

        supervisor._run_claude = spy
        supervisor.process_one(msg["id"])

        assert captured["state"]["phase"] == "processing"
        assert captured["state"]["current"]["from"] == "sender"
        assert captured["state"]["current"]["subject"] == "urgent"


class TestRequestStop:
    def test_sets_stop_flag(self, supervisor):
        assert supervisor._stop is False
        supervisor.request_stop()
        assert supervisor._stop is True


class TestRun:
    def test_stops_cleanly_and_writes_stopped_state(self, supervisor, monkeypatch):
        calls = {"n": 0}

        def fake_sleep(_seconds):
            calls["n"] += 1
            supervisor._stop = True

        monkeypatch.setattr("cld.messenger.agent_loop.time.sleep", fake_sleep)
        supervisor.run()

        assert calls["n"] == 1
        state = json.loads(supervisor.state_path.read_text())
        assert state["phase"] == "stopped"

    def test_processes_pending_message_before_polling(self, supervisor, monkeypatch):
        mailbox.write_message(supervisor.mailbox_root, "sender", supervisor.session_name, "hi", "body")

        def fake_sleep(_seconds):
            supervisor._stop = True

        monkeypatch.setattr("cld.messenger.agent_loop.time.sleep", fake_sleep)
        supervisor.run()

        assert supervisor.msg_count == 1
