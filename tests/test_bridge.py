"""Tests for the Mattermost bridge: routing, target classification, state, delivery.

Everything here runs against ``tmp_path`` and a fake client -- no network, no
docker, no containers.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cld.bridge import daemon, fleet, routing
from cld.bridge.mattermost import BRIDGE_NAME, Bridge
from cld.bridge.state import BridgeState
from cld.config import Config
from cld.messenger import mailbox


# --- fixtures -----------------------------------------------------------------


class FakeClient:
    """Records posts; serves a scripted channel."""

    def __init__(self, posts: list[dict] | None = None):
        self.incoming = posts or []
        self.posted: list[dict] = []
        self._next_id = 0

    def posts_since(self, channel_id: str, since_ms: int) -> list[dict]:
        return [p for p in self.incoming if p.get("create_at", 0) > since_ms]

    def create_post(self, channel_id: str, message: str, root_id: str = "") -> dict:
        self._next_id += 1
        post = {"id": f"post{self._next_id}", "message": message, "root_id": root_id}
        self.posted.append(post)
        return post

    def whoami(self) -> dict:
        return {"id": "bot", "username": "cld-bridge"}

    @property
    def messages(self) -> list[str]:
        return [p["message"] for p in self.posted]


@pytest.fixture
def cfg(tmp_path):
    return Config(
        mailbox_root=str(tmp_path / "mailboxes"),
        mattermost_url="https://mm.example",
        mattermost_channel_id="chan",
        mattermost_allowed_user_ids=("u1",),
        mattermost_state_file=str(tmp_path / "state.json"),
        mattermost_reply_timeout=900,
    )


@pytest.fixture
def root(cfg):
    p = Path(cfg.mailbox_root)
    p.mkdir(parents=True, exist_ok=True)
    return p


def make_agent(root, name, phase="idle", task="do the thing", parent="cld_master_x"):
    mailbox.ensure_mailbox(root, name)
    mailbox.ensure_meta(root, name, parent=parent, task=task, persona="", deliverable_branch=name, anchor="abc", peers={})
    (mailbox.mailbox_dir(root, name) / "state.json").write_text(json.dumps({
        "container_name": name, "phase": phase, "msg_count": 2, "cost_usd_total": 0.5,
    }))
    return name


def make_bridge(cfg, root, client):
    return Bridge(cfg, client, root, BridgeState.load(Path(cfg.mattermost_state_file)))


def post(message="", user="u1", post_id="p1", root_id="", create_at=100, **kw):
    base = {
        "id": post_id, "message": message, "user_id": user, "channel_id": "chan",
        "root_id": root_id, "create_at": create_at, "update_at": create_at, "type": "",
    }
    return {**base, **kw}


# --- routing.rejection_reason (plan §7) ---------------------------------------


@pytest.mark.parametrize("post_kw,expected", [
    ({}, None),
    ({"user_id": "intruder"}, "sender not allowed"),
    ({"type": "system_join_channel"}, "system message"),
    ({"props": {"from_bot": "true"}}, "bot or webhook"),
    ({"props": {"from_webhook": "true"}}, "bot or webhook"),
    ({"update_at": 200}, "edited post"),
    ({"channel_id": "other"}, "other channel"),
    ({"message": "   "}, "empty message"),
], ids=["ok", "intruder", "system", "bot", "webhook", "edited", "other-channel", "empty"])
def test_rejection_reason(post_kw, expected):
    p = post(**{"message": "@agent hi", **post_kw})
    assert routing.rejection_reason(p, "chan", ("u1",), seen=False) == expected


def test_rejection_reason_seen():
    assert routing.rejection_reason(post("hi"), "chan", ("u1",), seen=True) == "already processed"


def test_bridge_never_answers_itself():
    """With a bot account the bridge is a different user, so its id is filterable."""
    own = post("-> **cld_agent_x** `abc123`", user="bot")
    assert routing.rejection_reason(own, "chan", ("u1",), seen=False, self_user_id="bot") == "our own post"


def test_own_posts_are_marked_seen_so_a_shared_account_still_works(cfg, root):
    """A personal access token makes the bridge and the user the same account.

    Filtering by user id would then reject everything the user types, so the bridge
    must recognise its own output by post id instead.
    """
    make_agent(root, "cld_agent_api_idem")
    client = FakeClient([post("@idem status?", create_at=100)])
    bridge = make_bridge(cfg, root, client)
    bridge.poll_channel({"cld_agent_api_idem"})

    ack = client.posted[-1]
    assert bridge.state.seen(ack["id"])

    # The ack comes back on the next poll, as it would from a real server.
    client.incoming.append(post(ack["message"], post_id=ack["id"], create_at=300))
    before = len(client.posted)
    bridge.poll_channel({"cld_agent_api_idem"})

    assert len(client.posted) == before, "the bridge reprocessed its own post"
    assert len(mailbox.list_inbox(root, "cld_agent_api_idem")) == 1


def test_shared_account_does_not_filter_the_user(cfg, root):
    make_agent(root, "cld_agent_api_idem")
    client = FakeClient([post("@idem status?")])
    # self_user_id="" is what build_bridge sets when the token is an allowlisted user.
    bridge = Bridge(cfg, client, root, BridgeState.load(Path(cfg.mattermost_state_file)), self_user_id="")
    bridge.poll_channel({"cld_agent_api_idem"})
    assert len(mailbox.list_inbox(root, "cld_agent_api_idem")) == 1


# --- routing.match_names / route_post -----------------------------------------


KNOWN = ["cld_agent_api_idem", "cld_agent_api_lint", "cld_agent_web"]


@pytest.mark.parametrize("token,expected", [
    ("cld_agent_web", ["cld_agent_web"]),
    ("idem", ["cld_agent_api_idem"]),
    ("cld_agent_api", ["cld_agent_api_idem", "cld_agent_api_lint"]),
    ("nope", []),
], ids=["exact", "bare-slug", "ambiguous-prefix", "unknown"])
def test_match_names(token, expected):
    assert sorted(routing.match_names(token, KNOWN)) == sorted(expected)


def test_route_mention():
    r = routing.route_post(post("@idem what is blocking you?"), None, KNOWN)
    assert (r.kind, r.target, r.text) == (routing.AGENT, "cld_agent_api_idem", "what is blocking you?")


def test_route_thread_reply_needs_no_prefix():
    r = routing.route_post(post("and the tests?", root_id="root1"), "cld_agent_web", KNOWN)
    assert (r.kind, r.target, r.root_id) == (routing.AGENT, "cld_agent_web", "root1")


def test_route_command():
    r = routing.route_post(post("!fleet"), None, KNOWN)
    assert (r.kind, r.target) == (routing.COMMAND, "fleet")


@pytest.mark.parametrize("text,fragment", [
    ("hello there", "Address an agent"),
    ("@nope hello", "No agent matches"),
    ("@cld_agent_api hello", "ambiguous"),
    ("@idem", "Nothing to send"),
], ids=["no-address", "unknown", "ambiguous", "empty-body"])
def test_route_errors(text, fragment):
    r = routing.route_post(post(text), None, KNOWN)
    assert r.kind == routing.ERROR and fragment in r.error


def test_route_unbound_thread():
    r = routing.route_post(post("hi", root_id="orphan"), None, KNOWN)
    assert r.kind == routing.ERROR and "not bound" in r.error


# --- routing.split_output -----------------------------------------------------


def test_split_output_short_is_untouched():
    assert routing.split_output("hello", 100) == ["hello"]


def test_split_output_reopens_code_fence():
    body = "```\n" + "\n".join(f"line {i}" for i in range(50)) + "\n```"
    chunks = routing.split_output(body, 120)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, chunk


def test_split_output_covers_all_lines():
    body = "\n".join(f"line {i}" for i in range(200))
    rejoined = "\n".join(routing.split_output(body, 300)).replace("```\n", "")
    for i in range(200):
        assert f"line {i}" in rejoined


# --- fleet.classify_target (plan §5) ------------------------------------------


def test_classify_ready(root):
    make_agent(root, "cld_agent_api_idem")
    t = fleet.classify_target(root, "cld_agent_api_idem", {"cld_agent_api_idem"})
    assert t.status == fleet.READY and t.ready


def test_classify_crashed_when_container_gone(root):
    make_agent(root, "cld_agent_api_idem", phase="processing")
    t = fleet.classify_target(root, "cld_agent_api_idem", set())
    assert t.status == fleet.CRASHED and "processing" in t.detail


def test_classify_master_is_attended(root):
    # A master writes no state.json -- only AgentSupervisor does -- but it is
    # still a real place to deliver a message: a human answers when they attach.
    mailbox.ensure_mailbox(root, "cld_master_api")
    t = fleet.classify_target(root, "cld_master_api", {"cld_master_api"})
    assert t.status == fleet.ATTENDED and t.ready and "attach" in t.detail


def test_classify_master_crashed_when_container_gone(root):
    mailbox.ensure_mailbox(root, "cld_master_api")
    t = fleet.classify_target(root, "cld_master_api", set())
    assert t.status == fleet.CRASHED and not t.ready


def test_classify_stopped(root):
    make_agent(root, "cld_agent_api_idem", phase="stopped")
    assert fleet.classify_target(root, "cld_agent_api_idem", {"cld_agent_api_idem"}).status == fleet.STOPPED


def test_classify_reaped(root):
    name = make_agent(root, "cld_agent_api_idem")
    mailbox.archive_mailbox(root, name)
    t = fleet.classify_target(root, name, set())
    assert t.status == fleet.REAPED and "transcript" in t.detail


def test_classify_unknown(root):
    assert fleet.classify_target(root, "nobody", set()).status == fleet.UNKNOWN


def test_classify_never_claims_crashed_without_docker(root):
    """running=None means liveness is unknown; a refusal storm would be wrong."""
    make_agent(root, "cld_agent_api_idem")
    assert fleet.classify_target(root, "cld_agent_api_idem", None).status == fleet.READY


def test_fleet_rows_spans_every_repo(root):
    make_agent(root, "cld_agent_api_idem")
    make_agent(root, "cld_agent_web_nav")
    mailbox.ensure_mailbox(root, BRIDGE_NAME)
    names = [t.name for t in fleet.fleet_rows(root, None, exclude=BRIDGE_NAME)]
    assert names == ["cld_agent_api_idem", "cld_agent_web_nav"]


# --- BridgeState --------------------------------------------------------------


def test_state_round_trip(tmp_path):
    s = BridgeState.load(tmp_path / "s.json")
    s.cursor_ms = 42
    s.record_sent("msg1", "root1", "cld_agent_x")
    s.mark_seen("p1")
    s.save()

    again = BridgeState.load(tmp_path / "s.json")
    assert again.cursor_ms == 42
    assert again.agent_for_thread("root1") == "cld_agent_x"
    assert again.thread_for_reply("msg1") == "root1"
    assert again.seen("p1") and "msg1" in again.outstanding


def test_state_seen_ids_are_capped(tmp_path):
    s = BridgeState.load(tmp_path / "s.json")
    for i in range(2500):
        s.mark_seen(f"p{i}")
    s.save()
    assert len(BridgeState.load(tmp_path / "s.json").seen_post_ids) == 2000


def test_state_unreadable_file_starts_fresh(tmp_path):
    (tmp_path / "s.json").write_text("{not json")
    assert BridgeState.load(tmp_path / "s.json").cursor_ms == 0


def test_discharge_clears_obligation_but_keeps_thread(tmp_path):
    s = BridgeState.load(tmp_path / "s.json")
    s.record_sent("msg1", "root1", "cld_agent_x")
    s.discharge("msg1")
    assert "msg1" not in s.outstanding
    assert s.thread_for_reply("msg1") == "root1"


# --- Bridge: delivery ---------------------------------------------------------


def test_delivery_reaches_the_agent_and_acks(cfg, root):
    make_agent(root, "cld_agent_api_idem")
    client = FakeClient([post("@idem status?")])
    bridge = make_bridge(cfg, root, client)

    bridge.poll_channel({"cld_agent_api_idem"})

    inbox = mailbox.list_inbox(root, "cld_agent_api_idem")
    assert len(inbox) == 1
    delivered = mailbox.read_message(root, "cld_agent_api_idem", inbox[0]["id"])
    assert delivered["from"] == BRIDGE_NAME
    assert delivered["expects_reply"] is True and delivered["body"] == "status?"
    assert "cld_agent_api_idem" in client.messages[0]


def test_dead_agent_is_refused_immediately_and_nothing_is_delivered(cfg, root):
    make_agent(root, "cld_agent_api_idem", phase="processing")
    client = FakeClient([post("@idem status?")])
    bridge = make_bridge(cfg, root, client)

    bridge.poll_channel(set())

    assert mailbox.list_inbox(root, "cld_agent_api_idem") == []
    assert "cannot deliver" in client.messages[0] and "container is gone" in client.messages[0]


def test_master_delivery_queues_for_a_human_to_answer(cfg, root):
    mailbox.ensure_mailbox(root, "cld_master_api")
    client = FakeClient([post("@cld_master_api hello")])
    make_bridge(cfg, root, client).poll_channel({"cld_master_api"})

    inbox = mailbox.list_inbox(root, "cld_master_api")
    assert len(inbox) == 1
    assert mailbox.read_message(root, "cld_master_api", inbox[0]["id"])["body"] == "hello"
    assert "cld_master_api" in client.messages[0]


def test_reply_lands_in_the_original_thread(cfg, root):
    make_agent(root, "cld_agent_api_idem")
    client = FakeClient([post("@idem status?", post_id="rootpost")])
    bridge = make_bridge(cfg, root, client)
    bridge.poll_channel({"cld_agent_api_idem"})

    sent_id = next(iter(bridge.state.sent))
    mailbox.write_message(root, "cld_agent_api_idem", BRIDGE_NAME, "Re: status?", "all good", answers=sent_id)

    bridge.drain_inbox()

    reply = client.posted[-1]
    assert "all good" in reply["message"] and reply["root_id"] == "rootpost"
    assert sent_id not in bridge.state.outstanding
    assert mailbox.list_inbox(root, BRIDGE_NAME) == []


def test_reply_names_its_sender(cfg, root):
    """Any container can write to us, so an unexpected sender must be visible."""
    make_agent(root, "cld_agent_api_idem")
    client = FakeClient()
    bridge = make_bridge(cfg, root, client)
    mailbox.write_message(root, "cld_agent_web_nav", BRIDGE_NAME, "heads up", "blocked on you")
    bridge.drain_inbox()
    assert "cld_agent_web_nav" in client.messages[0]


def test_unsolicited_message_opens_a_thread_you_can_reply_in(cfg, root):
    make_agent(root, "cld_agent_web_nav")
    client = FakeClient()
    bridge = make_bridge(cfg, root, client)
    mailbox.write_message(root, "cld_agent_web_nav", BRIDGE_NAME, "heads up", "blocked on you")
    bridge.drain_inbox()

    opened = client.posted[0]["id"]
    assert bridge.state.agent_for_thread(opened) == "cld_agent_web_nav"

    client.incoming = [post("unblocked, go ahead", post_id="p2", root_id=opened, create_at=200)]
    bridge.poll_channel({"cld_agent_web_nav"})

    inbox = mailbox.list_inbox(root, "cld_agent_web_nav")
    assert len(inbox) == 1
    assert mailbox.read_message(root, "cld_agent_web_nav", inbox[0]["id"])["body"] == "unblocked, go ahead"


def test_fleet_command_does_not_message_any_agent(cfg, root):
    make_agent(root, "cld_agent_api_idem")
    client = FakeClient([post("!fleet")])
    make_bridge(cfg, root, client).poll_channel({"cld_agent_api_idem"})
    assert mailbox.list_inbox(root, "cld_agent_api_idem") == []
    assert "cld_agent_api_idem" in client.messages[0]


def test_fleet_lists_live_agents_and_attended_masters(cfg, root):
    make_agent(root, "cld_agent_api_live")
    make_agent(root, "cld_agent_api_dead", phase="processing")
    make_agent(root, "cld_agent_api_done", phase="stopped")
    mailbox.ensure_mailbox(root, "cld_master_api")
    reaped = make_agent(root, "cld_agent_api_gone")
    mailbox.archive_mailbox(root, reaped)

    client = FakeClient([post("!fleet")])
    make_bridge(cfg, root, client).poll_channel({"cld_agent_api_live", "cld_master_api"})

    listed = client.messages[0]
    assert "cld_agent_api_live" in listed
    assert "cld_master_api" in listed and "attended" in listed
    for absent in ("cld_agent_api_dead", "cld_agent_api_done", "cld_agent_api_gone"):
        assert absent not in listed, f"{absent} should not be in the roster"


def test_a_dead_agent_is_still_addressable_with_its_reason(cfg, root):
    """Hidden from !fleet, but naming it must explain itself, not say "no match"."""
    make_agent(root, "cld_agent_api_dead", phase="processing")
    client = FakeClient([post("@dead status?")])
    make_bridge(cfg, root, client).poll_channel(set())
    assert "container is gone" in client.messages[0]


def test_fleet_with_nothing_live_says_so(cfg, root):
    make_agent(root, "cld_agent_api_dead", phase="processing")
    client = FakeClient([post("!fleet")])
    make_bridge(cfg, root, client).poll_channel(set())
    assert "No live agents" in client.messages[0] and "1 mailbox(es) present" in client.messages[0]


def test_restart_does_not_redeliver(cfg, root):
    make_agent(root, "cld_agent_api_idem")
    posts = [post("@idem status?")]

    bridge = make_bridge(cfg, root, FakeClient(posts))
    bridge.poll_channel({"cld_agent_api_idem"})
    bridge.state.save()

    revived = make_bridge(cfg, root, FakeClient(posts))
    revived.poll_channel({"cld_agent_api_idem"})

    assert len(mailbox.list_inbox(root, "cld_agent_api_idem")) == 1


# --- Bridge: the failures after acceptance (plan §8) --------------------------


def test_crash_after_acceptance_is_reported_within_one_tick(cfg, root):
    make_agent(root, "cld_agent_api_idem")
    client = FakeClient([post("@idem status?")])
    bridge = make_bridge(cfg, root, client)
    bridge.poll_channel({"cld_agent_api_idem"})

    bridge.check_outstanding(set())

    assert "died before replying" in client.messages[-1]
    assert bridge.state.outstanding == {}


def test_crash_notice_fires_once(cfg, root):
    make_agent(root, "cld_agent_api_idem")
    client = FakeClient([post("@idem status?")])
    bridge = make_bridge(cfg, root, client)
    bridge.poll_channel({"cld_agent_api_idem"})

    bridge.check_outstanding(set())
    before = len(client.posted)
    bridge.check_outstanding(set())
    assert len(client.posted) == before


def test_no_notice_while_the_agent_is_alive_and_within_timeout(cfg, root):
    make_agent(root, "cld_agent_api_idem", phase="processing")
    client = FakeClient([post("@idem status?")])
    bridge = make_bridge(cfg, root, client)
    bridge.poll_channel({"cld_agent_api_idem"})
    before = len(client.posted)

    bridge.check_outstanding({"cld_agent_api_idem"})

    assert len(client.posted) == before, "silence means working -- no progress reporting (D10)"


def test_unknown_liveness_produces_no_crash_notice(cfg, root):
    make_agent(root, "cld_agent_api_idem")
    client = FakeClient([post("@idem status?")])
    bridge = make_bridge(cfg, root, client)
    bridge.poll_channel(None)
    before = len(client.posted)
    bridge.check_outstanding(None)
    assert len(client.posted) == before


def test_master_timeout_notice_names_the_missing_supervisor(cfg, root):
    """A master's silence means no one has attached, not a hung supervisor."""
    mailbox.ensure_mailbox(root, "cld_master_api")
    client = FakeClient([post("@cld_master_api hello")])
    bridge = make_bridge(cfg, root, client)
    bridge.poll_channel({"cld_master_api"})

    msg_id = next(iter(bridge.state.outstanding))
    bridge.state.outstanding[msg_id]["sent_at"] = "2000-01-01T00:00:00Z"

    bridge.check_outstanding({"cld_master_api"})

    assert "no autonomous supervisor" in client.messages[-1]
    assert bridge.state.outstanding == {}


# --- the tripwire (plan §13) --------------------------------------------------


def test_bridge_edges_are_unbudgeted(cfg, root):
    """The bridge writes no meta.json, so no hop or ask budget applies to it.

    If someone gives it one, every fleet conversation silently becomes budgeted
    and starts getting refused mid-exchange. This is the tripwire for that.
    """
    name = make_agent(root, "cld_agent_api_idem")
    assert mailbox.read_meta(root, BRIDGE_NAME) is None

    for i in range(cfg.peer_absolute_limit + 5):
        result = mailbox.gated_send(
            root, BRIDGE_NAME, name, f"q{i}", "body",
            default_limit=cfg.peer_absolute_limit, ask_limit=cfg.root_ask_limit,
            expects_reply=True,
        )
        assert "error" not in result, f"refused at message {i}: {result.get('error')}"


# --- daemon process control (mirrors broker/cld-brokerctl.sh) -----------------


@pytest.fixture
def bridge_home(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "_BRIDGE_DIR", tmp_path / "bridge")
    return tmp_path / "bridge"


def test_running_pid_is_none_without_a_pidfile(bridge_home):
    assert daemon.running_pid() is None


def test_stale_pidfile_is_cleared(bridge_home):
    daemon.pid_file().write_text("999999")
    assert daemon.running_pid() is None
    assert not daemon.pid_file().exists()


def test_corrupt_pidfile_is_cleared(bridge_home):
    daemon.pid_file().write_text("not-a-pid")
    assert daemon.running_pid() is None
    assert not daemon.pid_file().exists()


def test_pid_reuse_by_a_foreign_process_is_not_claimed(bridge_home, monkeypatch):
    """A recycled pid belonging to something else must not be reported as ours."""
    daemon.pid_file().write_text(str(os.getpid()))
    monkeypatch.setattr(daemon, "_is_ours", lambda pid: False)
    assert daemon.running_pid() is None


def test_stop_when_not_running(bridge_home):
    assert daemon.stop() is None


def _fake_popen(monkeypatch, script: str):
    """Swap the spawned argv, keeping the real Popen and the real detach flags."""
    real_popen = subprocess.Popen
    monkeypatch.setattr(
        daemon.subprocess, "Popen",
        lambda _argv, **kw: real_popen([sys.executable, "-c", script], **kw),
    )


def test_start_stop_round_trip(bridge_home, monkeypatch):
    """The real detach path: spawn, confirm it survives, then SIGTERM it."""
    _fake_popen(monkeypatch, "import time; time.sleep(30)")
    pid = daemon.start()
    assert daemon.running_pid() == pid
    assert daemon.stop() == pid
    assert daemon.running_pid() is None


def test_start_refuses_when_already_running(bridge_home, monkeypatch):
    _fake_popen(monkeypatch, "import time; time.sleep(30)")
    daemon.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            daemon.start()
    finally:
        daemon.stop()


def test_start_reports_a_daemon_that_dies_immediately(bridge_home, monkeypatch):
    _fake_popen(monkeypatch, "import sys; print('boom'); sys.exit(3)")
    with pytest.raises(RuntimeError, match="exited immediately"):
        daemon.start()
    assert not daemon.pid_file().exists()


def test_tail_log_without_a_log(bridge_home):
    assert "no log at" in daemon.tail_log(10)
