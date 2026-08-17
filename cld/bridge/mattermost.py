"""The Mattermost bridge daemon (docs/impl-mattermost-bridge-plan.md).

One responsibility: deliver messages from the channel to an agent, and its
replies back. The contract is that every message you send either produces a
reply in the thread or a notice saying why it never will -- §8's three failures
are the whole of the notice logic, and there is deliberately no progress
reporting in between.

One loop, one tick: drain our own inbox (local filesystem, cheap), poll the
channel, then check what we are still owed.
"""

import calendar
import signal
import time
from pathlib import Path

from cld.bridge import fleet, routing
from cld.bridge.client import HttpMattermostClient, MattermostClient, read_token
from cld.bridge.state import BridgeState
from cld.config import Config
from cld.log import get_logger
from cld.messenger import mailbox

log = get_logger(__name__)

BRIDGE_NAME = "mattermost"
_INBOX_SCAN_LIMIT = 25


def _elapsed_seconds(iso_ts: str) -> float:
    whole = iso_ts.split(".", 1)[0].rstrip("Z") + "Z"
    return time.time() - calendar.timegm(time.strptime(whole, "%Y-%m-%dT%H:%M:%SZ"))


class Bridge:
    """Owns one channel, one mailbox identity, and the state file between them."""

    def __init__(
        self, cfg: Config, client: MattermostClient, mailbox_root: Path, state: BridgeState,
        self_user_id: str = "",
    ):
        self.cfg = cfg
        self.client = client
        self.root = mailbox_root
        self.state = state
        self.channel = cfg.mattermost_channel_id
        self.self_user_id = self_user_id
        self._stop = False
        mailbox.ensure_mailbox(self.root, BRIDGE_NAME)

    # --- posting ---------------------------------------------------------------

    def _post(self, message: str, root_id: str = "") -> str:
        """Post, chunking if needed. Returns the first post's id (the thread root).

        Every post we create is marked seen immediately. That is what stops the bridge
        reading its own output back in, and unlike a user-id check it works when the
        token belongs to the human it is talking to (a personal access token), where
        the bridge and the user are literally the same account.
        """
        first = ""
        for chunk in routing.split_output(message, self.cfg.mattermost_max_post_chars):
            created = self.client.create_post(self.channel, chunk, root_id or first)
            self.state.mark_seen(created.get("id", ""))
            first = first or created.get("id", "")
        return first

    # --- outbound: our inbox -> the channel ------------------------------------

    def drain_inbox(self) -> int:
        """Post every reply waiting for us, oldest first, then archive it."""
        # list_inbox yields summaries; the body and `answers` need a full read.
        summaries = mailbox.list_inbox(self.root, BRIDGE_NAME)[:_INBOX_SCAN_LIMIT]
        messages = [m for s in summaries if (m := mailbox.read_message(self.root, BRIDGE_NAME, s["id"]))]
        for msg in messages:
            answers = msg.get("answers", "")
            root_id = self.state.thread_for_reply(answers)
            # The sender is named on every post: any container with the mailbox
            # mounted can write to us, so an unexpected one must be obvious.
            body = f"**{msg['from']}**\n\n{msg['body']}"
            posted = self._post(body, root_id)
            # An unsolicited message opens a thread of its own; bind it to the sender
            # so replying in it reaches them instead of "thread not bound to an agent".
            if not root_id and posted:
                self.state.bind_thread(posted, msg["from"])
            self.state.discharge(answers)
            mailbox.archive_message(self.root, BRIDGE_NAME, msg["id"])
        return len(messages)

    # --- inbound: the channel -> an agent --------------------------------------

    def poll_channel(self, running: set[str] | None) -> int:
        posts = self.client.posts_since(self.channel, self.state.cursor_ms)
        handled = 0
        for post in posts:
            self.state.cursor_ms = max(self.state.cursor_ms, post.get("create_at", 0))
            reason = routing.rejection_reason(
                post, self.channel, self.cfg.mattermost_allowed_user_ids,
                self.state.seen(post.get("id", "")), self.self_user_id,
            )
            self.state.mark_seen(post.get("id", ""))
            if reason:
                log.debug("ignoring post %s: %s", post.get("id"), reason)
                continue
            self._handle(post, running)
            handled += 1
        return handled

    def _handle(self, post: dict, running: set[str] | None) -> None:
        known = [t.name for t in fleet.fleet_rows(self.root, running, exclude=BRIDGE_NAME)]
        route = routing.route_post(post, self.state.agent_for_thread(post.get("root_id", "")), known)

        if route.kind == routing.ERROR:
            self._post(route.error, route.root_id)
            return
        if route.kind == routing.COMMAND:
            self._command(route, running)
            return

        target = fleet.classify_target(self.root, route.target, running)
        if not target.ready:
            self._post(f"**cannot deliver to `{target.name}`** -- {target.detail}", route.root_id)
            return

        result = mailbox.gated_send(
            self.root, BRIDGE_NAME, target.name,
            routing.subject_of(route.text), route.text,
            default_limit=self.cfg.peer_absolute_limit,
            ask_limit=self.cfg.root_ask_limit,
            expects_reply=True,
        )
        if "error" in result:
            self._post(f"**delivery refused** -- {result['error']}", route.root_id)
            return

        self.state.record_sent(result["id"], route.root_id, target.name)
        self._post(f"-> **{target.name}** `{result['id'][:8]}`", route.root_id)

    def _command(self, route: routing.Route, running: set[str] | None) -> None:
        if route.target == "fleet":
            rows = fleet.fleet_rows(self.root, running, exclude=BRIDGE_NAME)
            self._post(fleet.render_fleet(self.root, rows), route.root_id)
        elif route.target == "help":
            self._post(
                "`@<agent> <message>` -- start a conversation (reply in-thread to continue)\n"
                "`!fleet` -- list every agent on the host\n"
                "`!help` -- this",
                route.root_id,
            )
        else:
            self._post(f"Unknown command `!{route.target}`. Try `!help`.", route.root_id)

    # --- the two failures that happen after acceptance (plan §8) ---------------

    def check_outstanding(self, running: set[str] | None) -> None:
        for msg_id, entry in list(self.state.outstanding.items()):
            if entry.get("notified"):
                continue
            agent = entry["agent"]
            root_id = self.state.thread_for_reply(msg_id)

            if running is not None and agent not in running:
                self._post(
                    f"**`{agent}` died before replying.** Its container is gone; work may be "
                    "recoverable from the origin store (`jj log -r 'heads(all())'`).",
                    root_id,
                )
                self._notified(msg_id)
                continue

            if _elapsed_seconds(entry["sent_at"]) < self.cfg.mattermost_reply_timeout:
                continue

            if agent.startswith("cld_master_"):
                # No supervisor, so neither "still working" nor "archived without
                # replying" (an anomaly for a supervisor's reply guarantee) applies --
                # a human simply hasn't checked in yet, and may not reply at all.
                self._post(
                    f"**`{agent}` has not replied** in "
                    f"{int(self.cfg.mattermost_reply_timeout / 60)}m. It has no autonomous "
                    "supervisor -- someone needs to attach (`cld master`) and check messages.",
                    root_id,
                )
                self._notified(msg_id)
                continue

            state = mailbox.read_state(self.root, agent) or {}
            # Specifically the inbox, not read_message -- that searches the archive
            # too, and "archived without replying" is the anomaly we are looking for.
            queued = (mailbox.mailbox_dir(self.root, agent) / "inbox" / f"{msg_id}.json").is_file()
            if not queued and state.get("phase") == "idle":
                # The supervisor archived our message without replying, which
                # agent_loop's reply guarantee is supposed to make impossible.
                self._post(
                    f"**`{agent}` archived the message without replying.** That should not "
                    "happen -- worth reporting.",
                    root_id,
                )
            else:
                self._post(
                    f"**`{agent}` has not replied** in "
                    f"{int(self.cfg.mattermost_reply_timeout / 60)}m (phase: {state.get('phase', '?')}). "
                    "It may still be working; `cld agent logs` has the detail.",
                    root_id,
                )
            self._notified(msg_id)

    def _notified(self, msg_id: str) -> None:
        """Say it once. The thread mapping stays in ``sent`` so a late reply still lands."""
        self.state.outstanding.pop(msg_id, None)

    # --- loop ------------------------------------------------------------------

    def tick(self) -> None:
        running = fleet.running_containers()
        self.drain_inbox()
        self.poll_channel(running)
        self.check_outstanding(running)
        self.state.save()

    def request_stop(self, *_args) -> None:
        log.info("stop requested")
        self._stop = True

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        log.info("bridge up: channel=%s mailbox=%s", self.channel, self.root / BRIDGE_NAME)
        backoff = 0
        while not self._stop:
            try:
                self.tick()
            except Exception as e:
                # A poll failure is usually the VPN, and it must not kill a daemon
                # holding undelivered replies. Back off, stay up, keep the state.
                backoff = min(backoff * 2 or self.cfg.mattermost_poll_interval, 300)
                log.error("tick failed (%s); retrying in %ds", e, backoff)
                time.sleep(backoff)
                continue
            backoff = 0
            time.sleep(self.cfg.mattermost_poll_interval)
        self.state.save()
        log.info("bridge stopped cleanly")


def build_bridge(cfg: Config) -> Bridge:
    """Validate config, prove the token works, and wire the daemon up."""
    if not cfg.mattermost_url:
        raise RuntimeError("mattermost_url is not set (see docs/impl-mattermost-bridge-plan.md §10)")
    if not cfg.mattermost_channel_id:
        raise RuntimeError("mattermost_channel_id is not set")
    if not cfg.mattermost_allowed_user_ids:
        raise RuntimeError("mattermost_allowed_user_ids is empty -- refusing to accept posts from anyone")
    if not cfg.mattermost_token_file:
        raise RuntimeError("mattermost_token_file is not set")

    client = HttpMattermostClient(cfg.mattermost_url, read_token(Path(cfg.mattermost_token_file)))
    me = client.whoami()
    log.info("authenticated to %s as %s (%s)", cfg.mattermost_url, me.get("username"), me.get("id"))

    # A dedicated bot account is a different user from you, so its posts can be
    # filtered by id. A personal access token is *your* account: the same id then
    # sits on the allowlist, and filtering by it would reject everything you type.
    # In that case post-id tracking in _post is the only correct mechanism.
    self_id = me.get("id", "")
    if self_id in cfg.mattermost_allowed_user_ids:
        log.info("token belongs to an allowlisted user; the bridge posts under your own name")
        self_id = ""

    root = Path(cfg.mailbox_root).expanduser()
    state = BridgeState.load(Path(cfg.mattermost_state_file))
    return Bridge(cfg, client, root, state, self_user_id=self_id)


def run_bridge(cfg: Config, once: bool = False) -> None:
    bridge = build_bridge(cfg)
    if once:
        bridge.tick()
        return
    bridge.run()
