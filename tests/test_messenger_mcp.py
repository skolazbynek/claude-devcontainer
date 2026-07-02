"""Tests for cld.mcp.messenger MCP tools (identity + mailbox wiring)."""

from unittest.mock import patch

import pytest

import cld.mcp.messenger as messenger_mod
from cld.mcp.messenger import archive, list_agents, list_inbox, read_message, send


@pytest.fixture(autouse=True)
def _mailbox_env(tmp_path, monkeypatch):
    monkeypatch.delenv("SESSION_NAME", raising=False)
    monkeypatch.setattr(messenger_mod, "_mailbox_root", lambda: tmp_path)
    yield tmp_path


class TestSend:
    def test_requires_session_name(self):
        result = send(to="cld_agent_repoB", subject="hi", body="b")
        assert "error" in result

    def test_sends_to_full_container_name(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SESSION_NAME", "cld_master_repoA_abcd1234")
        with patch("cld.messenger.mailbox.list_containers", return_value=[
            {"name": "cld_agent_repoB", "kind": "agent", "repo": "/x/repoB", "status": "running"},
        ]):
            result = send(to="cld_agent_repoB", subject="hi", body="review this")
        assert "id" in result
        inbox_files = list((tmp_path / "cld_agent_repoB" / "inbox").glob("*.json"))
        assert len(inbox_files) == 1

    def test_unresolvable_recipient_returns_error(self, monkeypatch):
        monkeypatch.setenv("SESSION_NAME", "cld_master_repoA_abcd1234")
        with patch("cld.messenger.mailbox.list_containers", return_value=[]):
            result = send(to="nonexistent", subject="hi", body="b")
        assert "error" in result


class TestListInboxAndRead:
    def test_round_trip(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SESSION_NAME", "cld_agent_repoB")
        with patch("cld.messenger.mailbox.list_containers", return_value=[
            {"name": "cld_agent_repoB", "kind": "agent", "repo": "/x/repoB", "status": "running"},
        ]):
            sent = send(to="cld_agent_repoB", subject="hi", body="body text")

        entries = list_inbox()
        assert len(entries) == 1
        assert entries[0]["id"] == sent["id"]

        full = read_message(id=sent["id"])
        assert full["subject"] == "hi"
        assert full["body"] == "body text"

    def test_list_inbox_without_session_name_returns_error_entry(self):
        result = list_inbox()
        assert result[0]["error"]

    def test_read_missing_message_returns_error(self, monkeypatch):
        monkeypatch.setenv("SESSION_NAME", "cld_agent_repoB")
        result = read_message(id="nonexistent")
        assert "error" in result


class TestArchive:
    def test_archives_own_message(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SESSION_NAME", "cld_agent_repoB")
        with patch("cld.messenger.mailbox.list_containers", return_value=[
            {"name": "cld_agent_repoB", "kind": "agent", "repo": "/x/repoB", "status": "running"},
        ]):
            sent = send(to="cld_agent_repoB", subject="hi", body="b")
        result = archive(id=sent["id"])
        assert result == {"ok": True}
        assert list_inbox() == []

    def test_missing_message_returns_error(self, monkeypatch):
        monkeypatch.setenv("SESSION_NAME", "cld_agent_repoB")
        result = archive(id="nonexistent")
        assert "error" in result


class TestListAgents:
    def test_delegates_to_mailbox_list_containers(self):
        with patch("cld.messenger.mailbox.list_containers", return_value=[{"name": "x"}]) as m:
            result = list_agents(kind="agent")
        m.assert_called_once_with("agent")
        assert result == [{"name": "x"}]

    def test_empty_kind_passes_none(self):
        with patch("cld.messenger.mailbox.list_containers", return_value=[]) as m:
            list_agents()
        m.assert_called_once_with(None)
