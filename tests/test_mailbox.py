"""Tests for cld.messenger.mailbox: pure filesystem transport."""

from unittest.mock import patch

import pytest

from cld.messenger.mailbox import (
    archive_message,
    ensure_mailbox,
    list_containers,
    list_inbox,
    mailbox_dir,
    oldest_inbox_id,
    outbox_changed_since,
    outbox_snapshot,
    read_message,
    resolve_recipient,
    write_message,
)


class TestEnsureMailbox:
    def test_creates_subdirs(self, tmp_path):
        ensure_mailbox(tmp_path, "cld_agent_repoA")
        base = mailbox_dir(tmp_path, "cld_agent_repoA")
        assert (base / "tmp").is_dir()
        assert (base / "inbox").is_dir()
        assert (base / "archive").is_dir()


class TestWriteMessage:
    def test_delivers_to_inbox(self, tmp_path):
        msg = write_message(tmp_path, "sender", "recipient", "hi", "body text")
        inbox_file = mailbox_dir(tmp_path, "recipient") / "inbox" / f"{msg['id']}.json"
        assert inbox_file.is_file()
        assert msg["from"] == "sender"
        assert msg["to"] == "recipient"
        assert msg["subject"] == "hi"
        assert msg["body"] == "body text"
        assert "ts" in msg

    def test_no_leftover_tmp_file(self, tmp_path):
        msg = write_message(tmp_path, "sender", "recipient", "hi", "body")
        tmp_file = mailbox_dir(tmp_path, "recipient") / "tmp" / f"{msg['id']}.json"
        assert not tmp_file.exists()

    def test_appends_outbox_log(self, tmp_path):
        write_message(tmp_path, "sender", "recipient", "hi", "body")
        outbox = mailbox_dir(tmp_path, "sender") / "outbox.log"
        assert outbox.is_file()
        assert len(outbox.read_text().strip().splitlines()) == 1

    def test_creates_recipient_mailbox_if_missing(self, tmp_path):
        write_message(tmp_path, "sender", "new-recipient", "hi", "body")
        assert (mailbox_dir(tmp_path, "new-recipient") / "inbox").is_dir()


class TestListInbox:
    def test_lists_unread_sorted_by_ts(self, tmp_path):
        m1 = write_message(tmp_path, "a", "recipient", "first", "b1")
        m2 = write_message(tmp_path, "a", "recipient", "second", "b2")
        entries = list_inbox(tmp_path, "recipient")
        assert [e["id"] for e in entries] == [m1["id"], m2["id"]]
        assert entries[0]["subject"] == "first"

    def test_unread_only_excludes_archived(self, tmp_path):
        msg = write_message(tmp_path, "a", "recipient", "hi", "body")
        archive_message(tmp_path, "recipient", msg["id"])
        assert list_inbox(tmp_path, "recipient") == []

    def test_archive_listing(self, tmp_path):
        msg = write_message(tmp_path, "a", "recipient", "hi", "body")
        archive_message(tmp_path, "recipient", msg["id"])
        entries = list_inbox(tmp_path, "recipient", unread_only=False)
        assert len(entries) == 1
        assert entries[0]["id"] == msg["id"]

    def test_empty_mailbox_returns_empty_list(self, tmp_path):
        assert list_inbox(tmp_path, "nobody") == []


class TestReadMessage:
    def test_reads_from_inbox(self, tmp_path):
        msg = write_message(tmp_path, "a", "recipient", "hi", "body")
        result = read_message(tmp_path, "recipient", msg["id"])
        assert result == msg

    def test_reads_from_archive(self, tmp_path):
        msg = write_message(tmp_path, "a", "recipient", "hi", "body")
        archive_message(tmp_path, "recipient", msg["id"])
        result = read_message(tmp_path, "recipient", msg["id"])
        assert result == msg

    def test_missing_id_returns_none(self, tmp_path):
        ensure_mailbox(tmp_path, "recipient")
        assert read_message(tmp_path, "recipient", "nonexistent") is None


class TestArchiveMessage:
    def test_moves_file(self, tmp_path):
        msg = write_message(tmp_path, "a", "recipient", "hi", "body")
        assert archive_message(tmp_path, "recipient", msg["id"]) is True
        base = mailbox_dir(tmp_path, "recipient")
        assert not (base / "inbox" / f"{msg['id']}.json").exists()
        assert (base / "archive" / f"{msg['id']}.json").exists()

    def test_noop_if_already_archived(self, tmp_path):
        msg = write_message(tmp_path, "a", "recipient", "hi", "body")
        archive_message(tmp_path, "recipient", msg["id"])
        assert archive_message(tmp_path, "recipient", msg["id"]) is True

    def test_missing_id_returns_false(self, tmp_path):
        ensure_mailbox(tmp_path, "recipient")
        assert archive_message(tmp_path, "recipient", "nonexistent") is False


class TestOldestInboxId:
    def test_returns_none_when_empty(self, tmp_path):
        ensure_mailbox(tmp_path, "recipient")
        assert oldest_inbox_id(tmp_path, "recipient") is None

    def test_returns_oldest_by_mtime(self, tmp_path):
        m1 = write_message(tmp_path, "a", "recipient", "first", "b1")
        m2 = write_message(tmp_path, "a", "recipient", "second", "b2")
        inbox = mailbox_dir(tmp_path, "recipient") / "inbox"
        import os
        import time
        now = time.time()
        os.utime(inbox / f"{m1['id']}.json", (now + 100, now + 100))
        os.utime(inbox / f"{m2['id']}.json", (now, now))
        assert oldest_inbox_id(tmp_path, "recipient") == m2["id"]


class TestOutboxSnapshot:
    def test_zero_when_missing(self, tmp_path):
        assert outbox_snapshot(tmp_path, "sender") == 0

    def test_counts_lines(self, tmp_path):
        write_message(tmp_path, "sender", "r1", "hi", "b")
        write_message(tmp_path, "sender", "r2", "hi", "b")
        assert outbox_snapshot(tmp_path, "sender") == 2

    def test_changed_since_detects_growth(self, tmp_path):
        snap = outbox_snapshot(tmp_path, "sender")
        assert outbox_changed_since(tmp_path, "sender", snap) is False
        write_message(tmp_path, "sender", "r1", "hi", "b")
        assert outbox_changed_since(tmp_path, "sender", snap) is True


class TestListContainers:
    """mailbox.list_containers delegates to the host-docker seam; on the host
    (in_master_container False) that hits the local daemon."""

    def test_parses_docker_output(self, tmp_path):
        ps_result = type("R", (), {
            "returncode": 0,
            "stdout": "cld_agent_repoA\tUp 2 hours\ncld_master_repoB_abcd1234\tExited (0) 1 hour ago\n",
            "stderr": "",
        })()
        inspect_results = [
            type("R", (), {"returncode": 0, "stdout": "agent|/home/u/repoA\n"})(),
            type("R", (), {"returncode": 0, "stdout": "master|/home/u/repoB\n"})(),
        ]
        with patch("cld.host_docker.in_master_container", return_value=False), \
             patch("cld.host_docker.subprocess.run", side_effect=[ps_result, *inspect_results]):
            containers = list_containers()
        assert containers == [
            {"name": "cld_agent_repoA", "kind": "agent", "repo": "/home/u/repoA", "status": "running"},
            {"name": "cld_master_repoB_abcd1234", "kind": "master", "repo": "/home/u/repoB", "status": "stopped"},
        ]

    def test_docker_failure_returns_empty(self, tmp_path):
        fail_result = type("R", (), {"returncode": 1, "stdout": "", "stderr": "docker not found"})()
        with patch("cld.host_docker.in_master_container", return_value=False), \
             patch("cld.host_docker.subprocess.run", return_value=fail_result):
            assert list_containers() == []


class TestResolveRecipient:
    _CONTAINERS = [
        {"name": "cld_agent_repoA", "kind": "agent", "repo": "/home/u/repoA", "status": "running"},
        {"name": "cld_master_repoA_abcd1234", "kind": "master", "repo": "/home/u/repoA", "status": "running"},
        {"name": "cld_master_repoB_ef567890", "kind": "master", "repo": "/x/repoB", "status": "running"},
    ]

    def test_full_name_used_verbatim(self):
        assert resolve_recipient("cld_master_repoB_ef567890", self._CONTAINERS) == "cld_master_repoB_ef567890"

    def test_existing_mailbox_short_circuits_without_enumeration(self, tmp_path):
        # Reply path: `to` names an existing mailbox dir -> return verbatim and
        # never call list_containers (so agents can reply with no host channel).
        (tmp_path / "cld_agent_repoA").mkdir()
        with patch("cld.messenger.mailbox.list_containers", side_effect=AssertionError("enumerated")):
            assert resolve_recipient("cld_agent_repoA", root=tmp_path) == "cld_agent_repoA"

    def test_shortname_prefers_agent_over_master(self):
        assert resolve_recipient("repoA", self._CONTAINERS) == "cld_agent_repoA"

    def test_shortname_resolves_to_only_match(self):
        assert resolve_recipient("repoB", self._CONTAINERS) == "cld_master_repoB_ef567890"

    def test_unknown_shortname_raises(self):
        with pytest.raises(ValueError, match="No container found"):
            resolve_recipient("nonexistent", self._CONTAINERS)

    def test_ambiguous_shortname_raises(self):
        containers = self._CONTAINERS + [
            {"name": "cld_master_repoA2_zz", "kind": "master", "repo": "/other/repoA", "status": "running"},
        ]
        with pytest.raises(ValueError, match="Ambiguous shortname"):
            resolve_recipient("repoA", containers)
