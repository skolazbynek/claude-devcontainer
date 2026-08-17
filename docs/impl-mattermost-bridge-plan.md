# Mattermost bridge — design + implementation plan

Chat with the existing cld fleet from a private Mattermost channel. A host-side
daemon polls the channel, routes each post into an agent's mailbox, and posts
replies back into the thread. No tower agent, no new container, no new
privilege.

Status: planned, nothing built.

## 1. Scope

The bridge has **one responsibility**: deliver messages from the channel to an
agent, and its replies back. Its contract is one sentence:

> Every message you send either produces a reply in the thread, or a notice
> saying why it never will.

**In:** one private channel; address an agent by name; thread-scoped
conversations; immediate refusal when the target cannot answer; notice when an
agent dies or wedges after accepting; `!fleet` (you cannot address an agent you
cannot name) and `!help`.

**Out**, because none of it is delivery: spawning and reaping from chat (stays a
human act from a master); a tower/controller agent; progress reporting and
elapsed-time updates; queue-depth tracking; mirroring traffic you are not part
of; file uploads (chunking covers long output); WebSocket transport (polling is
sufficient — latency is dominated by the claude turn); group chat or multiple
channels; agent-to-agent relaying (peer edges already exist).

**Non-goal:** giving any container a Mattermost tool. The token never enters a
container and no agent gains a new capability. The bridge is an edge adapter
over the mailbox transport that already exists.

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | Mattermost is a **transport over the mailbox**, not a tool claude holds | `agent_loop.py` is already the driver; the reply guarantee, transcript, cost and state come free. A prompt instruction to "reply via MCP" is a request, not a guarantee — `replied_since` exists because of that |
| D2 | The bridge runs **on the host**, not in a container | The mailbox root is a host directory; the token stays off every container; privileged reads (`docker ps`) need no broker |
| D3 | The bridge has a **mailbox identity** (`mattermost`) | Makes `expects_reply` oblige a reply *to us*, so the supervisor's fallback lands in the channel instead of nowhere (`agent_loop.py:359-374`) |
| D4 | **No tower agent.** The bridge routes directly to named agents | One claude turn per question instead of two-plus; removes the async fan-out problem entirely; nothing new runs with skip-permissions |
| D5 | **Strict pre-flight.** A post for an agent that cannot answer is refused immediately, in-thread, with the reason | The alternative (optimistic delivery + timeout) means silence for minutes before you learn the container is gone |
| D6 | Threads are the unit of conversation; `@name` only at channel root | Thread-stickiness gives phone ergonomics without a channel-wide "current agent" you can misfire into |
| D7 | Privileged verbs are handled **by the bridge**, never by an agent | Keeps the broker's master-only invariant intact; the allowlist lives in code you own |
| D8 | Polling, not webhooks | Outbound-only; a laptop behind NAT/VPN cannot receive an inbound webhook |
| D9 | The bridge is **stateful and durable** | Cursor, seen-post ids and the thread map must survive restart or you re-run yesterday's tasks and orphan replies |
| D10 | **One responsibility** (§1). Every feature must serve the delivery contract or be cut | A message router that also reports progress, tracks queues and mirrors traffic is three things badly. `!fleet` is the one deliberate exception, and only because addressing requires naming |
| D11 | `!fleet` spans **every repo on the host**, unscoped, but lists **live agents and attended masters only** | Unscoped needs no config and matches the use case. Filtered to what can actually answer, because the roster exists to let you name a target that will -- crashed, stopped, reaped and genuinely unattended (never-booted) mailboxes accumulate and turn it into a graveyard you read past. Masters count as answerable even with no supervisor: a human answers when they attach, and delivery to one queues rather than refuses. Nothing is hidden: addressing a dead agent still reports why it cannot answer. Accepted cost: a channel compromise enumerates every live task and master across all repos |

## 3. Architecture

```
Mattermost channel
      |  poll GET /api/v4/channels/{id}/posts?since=<cursor>      (3s)
      v
cld bridge mattermost  (host process, poetry-installed cld)
      |  classify target -> refuse or deliver
      |  mailbox.gated_send(root, "mattermost", <agent>, expects_reply=True)
      v
~/.cld/mailboxes/
   ├── mattermost/inbox/        <- replies land here; the bridge drains it
   ├── cld_agent_<repo>/        <- standing repo agents
   └── cld_agent_<repo>_<slug>/ <- task-agents
             ^
             |  supervisor polls inbox every 1s (agent_loop.py:35,394-399)
```

**One loop, one tick:** drain `mattermost/inbox/` (local filesystem, cheap),
then poll the channel, then run the liveness check for outstanding messages.
Two threads would buy ~3 s of reply latency against a claude turn measured in
minutes. Not worth the lock.

## 4. Module layout

Pure/IO split mirroring `mailbox.py` (pure) vs `agents.py` (wrapper), so the
interesting logic is testable with `tmp_path` and no network.

```
cld/bridge/__init__.py
cld/bridge/mattermost.py   -- daemon: the tick loop, wiring, signal handling
cld/bridge/client.py       -- Mattermost REST over httpx (the only network code)
cld/bridge/routing.py      -- PURE: route_post, resolve_name, split_output, parse_command
cld/bridge/fleet.py        -- classify_target + fleet rows (mailbox fs join docker ps)
cld/bridge/state.py        -- BridgeState: load/save, atomic rename, ring-buffered seen ids
```

`client.py` sits behind a small protocol so the daemon runs against a fake in
tests. Three methods — no `update_post` (nothing is ever edited) and no
`upload_file` (chunking covers long output).

```python
# client.py
class MattermostClient(Protocol):
    def posts_since(self, channel_id: str, since_ms: int) -> list[dict]: ...
    def create_post(self, channel_id: str, message: str, root_id: str = "") -> dict: ...
    def whoami(self) -> dict: ...          # startup sanity check

# fleet.py
@dataclass(frozen=True)
class Target:
    name: str
    status: str          # ready | attended | reaped | unattended | stopped | crashed | unknown
    detail: str          # human sentence for the refusal / fleet row
    meta: dict | None
    state: dict | None

def classify_target(root: Path, name: str, running: set[str]) -> Target: ...
def fleet_rows(root: Path, running: set[str]) -> list[Target]: ...
def running_containers() -> set[str]: ...     # one batched docker ps, cached per tick

# routing.py
def resolve_name(token: str, known: list[str]) -> str | None: ...   # exact, then unique prefix/slug
def route_post(post: dict, state: BridgeState) -> Route: ...
def split_output(text: str, limit: int) -> list[str]: ...
```

## 5. Target classification (D5)

One function, evaluated **before** every delivery, first match wins. This is
the safety-critical part of the bridge.

| Order | Test | Status | Refusal message |
|---|---|---|---|
| 1 | `mailbox.mailbox_reaped(root, name)` | `reaped` | "reaped — transcript: `cld task-agent transcript <slug>`" |
| 2 | no live mailbox dir | `unknown` | "no such agent" + fleet list |
| 3a | `read_state()` is None, name is a master, not running | `crashed` | "container is gone. Work may be recoverable from the origin store" |
| 3b | `read_state()` is None, name is a master, running | `attended` (deliverable) | delivers; queues for a human to answer when they attach |
| 3c | `read_state()` is None, not a master | `unattended` | "supervisor never booted — check `cld agent logs`" |
| 4 | `state["phase"] == "stopped"` | `stopped` | "supervisor exited cleanly" |
| 5 | `name not in running` | `crashed` | "container is gone; mailbox last said `<phase>` at `<ts>`. Work may be recoverable from the origin store" |
| 6 | — | `ready` | deliver |

Notes:

- Step 3 is what separates masters from agents on disk: only
  `AgentSupervisor._write_state` writes `state.json`, and master's entrypoint
  just `sleep infinity` (`entrypoint-claude-devcontainer.sh:201-221`). A master
  never gets its own supervisor -- that was a deliberate choice, kept even after
  masters became addressable, because a headless loop running `claude -p`
  against the same `/workspace/current` a human attaches to interactively would
  race with whatever they are doing by hand. `attended` accepts that a master
  answers on human time instead: delivery still succeeds (`Target.ready` is
  true for `ready` *and* `attended`), the message queues in its mailbox, and a
  person replies via the `messenger-*` skills when they next attach --
  `mailbox.gated_send`/`write_message` don't care who or what writes the reply,
  so it posts back into the thread exactly like an agent's would.
- Step 1 matches what the transport would do anyway — `write_message` refuses a
  reaped recipient (`mailbox.py:138-140`) — but silently returns `None`. We
  want the reason in the channel, not a warning in a log.
- A `ready` agent is ready regardless of what is already in its inbox. The
  bridge does not count, report or gate on queue depth (D10); the supervisor is
  strictly FIFO and will get to it.
- `running_containers()` is **one batched call per tick**
  (`docker ps --filter label=org.cld.kind --format '{{.Names}}\t{{.State}}'`),
  cached for the tick. Never one call per message.

## 6. Routing

Applied in order:

1. **Bang-command** (`!fleet`, `!help`) → handled by the bridge, never reaches an
   agent, replies in the same thread if the post was in one. `!fleet` renders live
   agents only (D11); the name-resolution list behind rule 3 stays complete.
2. **Reply inside a known thread** → that thread's agent. No prefix needed.
3. **Starts with `@<token>`** → `resolve_name`: exact container name, else unique
   match on bare slug or prefix. Ambiguous → list the candidates and refuse.
4. **Anything else** → refuse with `!fleet` output and a usage hint. No default
   target, no sticky channel-wide agent (D6).

On successful delivery the bridge posts one ack in-thread naming the
**resolved** target and the message id. One post, never edited, never followed
up unless the delivery fails (§8).

## 7. Inbound filters

Reject before routing, in this order:

- post id already in `seen_post_ids` — **this is what stops the bridge reading its own
  output back in**, because `_post` marks every post it creates as seen. Post-id
  tracking is the only mechanism that works for both auth shapes (below)
- `user_id` is the bridge's own, *and* that id is not on the allowlist. With a bot
  account the bridge is a separate user, so its id is a usable filter. With a
  **personal access token the bridge is you**: the same id is necessarily allowlisted,
  and filtering on it would reject every message you type. `build_bridge` blanks
  `self_user_id` in that case and relies on post-id tracking alone
- `user_id` not in `mattermost_allowed_user_ids` (ids, not usernames — usernames are mutable)
- `props.from_bot` or `props.from_webhook` present
- `type != ""` (system messages: joins, header changes)
- `update_at > create_at` (an edited post; otherwise editing re-runs the task)
- `channel_id != mattermost_channel_id`

Silently ignored, not refused — a refusal post for every system message would be
noise, and refusing a disallowed user tells them the bridge exists.

## 8. The three ways a delivery fails

Pre-flight (§5) covers the first. The other two happen after acceptance, and
each produces exactly **one** post in the thread. There is no progress
reporting: silence means the agent is working.

| # | Failure | Detection | Notice |
|---|---|---|---|
| 1 | Target cannot answer | `classify_target` before delivery | immediate refusal, with the reason and remedy |
| 2 | Container dies after accepting | it vanishes from `running` on a later tick | immediate crash warning with the recovery hint |
| 3 | Supervisor wedges while alive | no reply after `mattermost_reply_timeout` | one notice, then quiet |

Failure 2 is driven by docker liveness, not a timer, so a mid-turn crash is
reported within one tick rather than after a timeout expires. That matters
because `state.json` is written only on transitions — it is **not** a heartbeat,
and its mtime must never be treated as one.

One anomaly is worth detecting because it means a bug: our message id is gone
from the target's `inbox/`, `phase == idle`, and no reply arrived. The reply
guarantee (`agent_loop.py:359-374`) should make this impossible. Post it as
failure 3 with a note that it should be reported.

An outstanding entry clears when a reply with `answers == <our id>` arrives.

## 9. Durable state

`~/.cld/mattermost-bridge.json`, atomic rename (same pattern as
`mailbox._write_json_atomic`):

```json
{
  "version": 1,
  "cursor_ms": 1723712345678,
  "seen_post_ids": ["..."],
  "threads":     { "<root_post_id>": {"agent": "...", "opened_at": "..."} },
  "sent":        { "<msg_id>": {"root_post_id": "...", "agent": "...", "ts": "..."} },
  "outstanding": { "<msg_id>": {"agent": "...", "sent_at": "...", "notified": false} }
}
```

`seen_post_ids` is ring-buffered (cap ~2000) so the file cannot grow without
bound. `threads` and `sent` are the two directions of the same mapping and must
be written in one save. `notified` makes each failure notice fire once.

**Bootstrap:** the daemon calls `mailbox.ensure_mailbox(root, "mattermost")` at
startup. Without it the directory does not exist until the first inbound
message, and an agent trying to send to `mattermost` would fail
`resolve_recipient` — the filesystem short-circuit requires the dir to exist
(`mailbox.py:707`) and container enumeration will never find it.

## 10. Config

New `Config` fields (add to `_TOML_KEYS`, the dataclass, and `from_env`, per
`cld/config.py` conventions):

| Field | Env | Default | Purpose |
|---|---|---|---|
| `mattermost_url` | `CLD_MATTERMOST_URL` | `""` | Server base URL; empty disables the bridge |
| `mattermost_token_file` | `CLD_MATTERMOST_TOKEN_FILE` | `""` | Path to the PAT. **Path, never the value** |
| `mattermost_channel_id` | `CLD_MATTERMOST_CHANNEL_ID` | `""` | The one channel |
| `mattermost_allowed_user_ids` | — (TOML array) | `()` | Allowlist of Mattermost user ids |
| `mattermost_poll_interval` | `CLD_MATTERMOST_POLL_INTERVAL` | `3` | Channel poll, seconds |
| `mattermost_reply_timeout` | `CLD_MATTERMOST_REPLY_TIMEOUT` | `900` | Failure 3 (§8) |
| `mattermost_max_post_chars` | `CLD_MATTERMOST_MAX_POST_CHARS` | `15000` | Chunk threshold; verify against the server's `MaxPostSize` |
| `mattermost_state_file` | `CLD_MATTERMOST_STATE_FILE` | `~/.cld/mattermost-bridge.json` | Durable state |

Token file is read once at startup; **refuse to start if it is group- or
world-readable**. Startup also calls `whoami()` and resolves the channel, so a
bad token fails loudly at launch rather than silently at the first poll.

Dependency: add `httpx = "^0.28"` as a direct dependency. It is already in
`poetry.lock` at 0.28.1 via `mcp`, so this is a declaration, not a new
resolution.

CLI: `cld/cli.py`, host-only (hidden `_host_only("cld bridge")` stub in
`cli_container.py` per `docs/design-cli-split.md`):

- `cld bridge mattermost [--once]` -- foreground; the one implementation, and what
  you use to debug.
- `cld bridge start|stop|restart|status|logs` -- detached lifecycle, deliberately the
  same shape as `broker/cld-brokerctl.sh`: a PID file and a log under `~/.cld/bridge`
  (`CLD_BRIDGE_DIR` overrides), no init system involved. `start` runs the config and
  auth checks **in your terminal** before detaching, so a bad token is not something
  you discover later by reading a log.

`cld/bridge/daemon.py` owns the process control. Two hazards it handles: a recycled
PID (the recorded pid must still be a `cld` process, read from `/proc/<pid>/cmdline`),
and a zombie -- signalling a terminated-but-unreaped process still succeeds, so the
`stop` wait loop tests liveness through `running_pid` rather than a bare
`kill(pid, 0)`, which would otherwise always burn the full timeout and SIGKILL.

## 11. Security controls

Checklist, all enforced in the bridge:

- Private channel; `user_id` allowlist; bot/webhook/system/edited posts dropped (§7).
- Token host-side only, mode-checked, never in TOML or a container.
- No container gains a capability. No broker access. No spawn/reap from chat (D7).
- Outbound is a choke point: size cap and chunking, and the one place to redact
  before anything reaches corporate chat.
- **Any container can send to `mattermost`** — `resolve_recipient` will resolve
  it for anyone with the mailbox mounted. That is a feature (an agent can flag a
  blocker directly) but the bridge must label `msg["from"]` prominently on every
  post so an unexpected sender is obvious.
- Consider launching chat-reachable agents without ssh-agent forwarding
  (`CLD_SSH_AUTH_SOCK=""`) so "may not push" is structural rather than prompted.

## 12. Phases

**P0 — outbound only.** `ensure_mailbox`, drain `mattermost/inbox/`, post to the
channel, archive. Chunking. State file. No inbound path at all, so nothing can
be driven and the risk is zero.
*Acceptance:* `cld msg send --to mattermost` from any container appears in the
channel within one tick and is archived.

**P1 — inbound with strict pre-flight.** Filters, routing, `classify_target`,
delivery, ack, thread mapping.
*Acceptance:* `@<agent> hello` reaches the agent and its reply lands in-thread;
`@<crashed-agent>` is refused **immediately** naming the reason; `@<master>`
delivers and queues, acking that it will be answered when someone attaches;
`@<reaped>` names the transcript command; an unknown or ambiguous name returns
candidates.

**P2 — close the contract.** `!fleet`, `!help`, failures 2 and 3 (§8), the detached
lifecycle verbs, VPN/backoff handling with a visible "disconnected" state.
*Acceptance:* killing an agent's container mid-turn produces an in-thread crash
warning within one tick; `!fleet` correctly separates ready / attended /
crashed / unattended / reaped across every repo; the daemon survives a VPN drop
and reconnects without replaying posts.

Then stop. Anything further is a second responsibility (D10).

## 13. Tests

Pure modules with `tmp_path` and a fake client; no network, no docker.

- `classify_target`: one case per row of §5, including the master case (no
  `state.json`, running -> `attended`, deliverable), the master-crashed case
  (no `state.json`, absent from `running` -> `crashed`), and the ordinary
  crashed case (state present, name absent from `running`).
- `resolve_name`: exact, bare slug, unique prefix, ambiguous, unknown.
- `route_post`: thread reply, `@name` at root, bang-command, bare post.
- Filters: each rejection in §7, especially the edited-post case.
- `BridgeState`: round-trip, ring-buffer cap, atomic replace, restart without
  duplicate delivery.
- `split_output`: boundaries, code-fence preservation.
- Failure detection: each row of §8 plus the anomaly, driven by synthetic
  `state.json` + `running` sets; assert each notice fires exactly once.
- **Pin that the bridge's edges are unbudgeted**: `read_meta(root, "mattermost")`
  is `None`, so `peer_edge` is False in `write_message` (`mailbox.py:148-149`)
  and no hop or ask budget applies. If someone later gives the bridge a
  `meta.json`, every fleet conversation silently becomes budgeted and starts
  getting refused mid-exchange. This test is the tripwire.
- Integration (marked `docker`): live agent round trip through a fake client.
