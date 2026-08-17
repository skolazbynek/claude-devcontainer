"""Tests for the messenger CLI verbs.

Only `send` for now, and only what matters most about it: it is the path the
`messenger-send` skill tells agents to run, so the hop gate has to cover it exactly
like the MCP tool (docs/design-task-agents.md §10, "both send paths").
"""

import sys
from unittest.mock import patch

import pytest

from cld.messenger import send as send_cli
from cld.messenger.mailbox import ensure_mailbox, ensure_meta, list_inbox, read_message

_SPAWN = {
    "parent": "cld_master_repoA_abcd1234",
    "task": "add oauth login",
    "persona": "implementer",
    "deliverable_branch": "add-oauth",
    "anchor": "abc123",
    "peers": {},
}


def _invoke(monkeypatch, tmp_path, to: str, frm: str = "agent-a") -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text("hello\n")
    monkeypatch.setattr(sys, "argv", [
        "send", "--to", to, "--subject", "hi", "--body-file", str(body_file),
    ])
    monkeypatch.setattr(send_cli, "resolve_self", lambda: (frm, tmp_path))
    send_cli.main()


class TestSendCli:
    def test_peer_send_is_counted_and_reported(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("CLD_PEER_ABSOLUTE_LIMIT", "2")
        ensure_meta(tmp_path, "agent-a", **_SPAWN)
        ensure_meta(tmp_path, "agent-b", **_SPAWN)
        _invoke(monkeypatch, tmp_path, "agent-b")
        assert "(hop 1/2)" in capsys.readouterr().out

    def test_master_send_reports_no_hops(self, monkeypatch, tmp_path, capsys):
        ensure_meta(tmp_path, "agent-a", **_SPAWN)
        ensure_mailbox(tmp_path, "cld_master_repoA_abcd1234")
        _invoke(monkeypatch, tmp_path, "cld_master_repoA_abcd1234")
        assert "hop" not in capsys.readouterr().out

    def test_refused_send_exits_1_and_delivers_nothing(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("CLD_PEER_ABSOLUTE_LIMIT", "1")
        ensure_meta(tmp_path, "agent-a", **_SPAWN)
        ensure_meta(tmp_path, "agent-b", **_SPAWN)
        _invoke(monkeypatch, tmp_path, "agent-b")
        capsys.readouterr()
        with pytest.raises(SystemExit) as exc:
            _invoke(monkeypatch, tmp_path, "agent-b")
        assert exc.value.code == 1
        assert "hop budget spent" in capsys.readouterr().err
        assert len(list_inbox(tmp_path, "agent-b")) == 1

    def test_peer_resolution_needs_no_container_enumeration(self, monkeypatch, tmp_path):
        """An agent container has no host channel, so enumerating would fail (§A.3)."""
        ensure_meta(tmp_path, "agent-a", **_SPAWN)
        ensure_meta(tmp_path, "agent-b", **_SPAWN)
        with patch("cld.messenger.mailbox.list_containers", side_effect=AssertionError("enumerated")):
            _invoke(monkeypatch, tmp_path, "agent-b")
        assert len(list_inbox(tmp_path, "agent-b")) == 1

    def test_inline_body_is_delivered(self, monkeypatch, tmp_path):
        ensure_meta(tmp_path, "agent-a", **_SPAWN)
        ensure_meta(tmp_path, "agent-b", **_SPAWN)
        monkeypatch.setattr(sys, "argv", [
            "send", "--to", "agent-b", "--subject", "hi", "--body", "hello inline",
        ])
        monkeypatch.setattr(send_cli, "resolve_self", lambda: ("agent-a", tmp_path))
        send_cli.main()
        [msg] = list_inbox(tmp_path, "agent-b")
        assert read_message(tmp_path, "agent-b", msg["id"])["body"] == "hello inline"

    def test_body_and_body_file_are_mutually_exclusive(self, monkeypatch, tmp_path):
        body_file = tmp_path / "body.md"
        body_file.write_text("hello\n")
        monkeypatch.setattr(sys, "argv", [
            "send", "--to", "agent-b", "--subject", "hi",
            "--body", "hello inline", "--body-file", str(body_file),
        ])
        monkeypatch.setattr(send_cli, "resolve_self", lambda: ("agent-a", tmp_path))
        with pytest.raises(SystemExit) as exc:
            send_cli.main()
        assert exc.value.code == 2
