# Inter-container agent messaging — mental model (locked-in decisions)

Product/UX-level overview of the feature. No code, no schemas, no paths.

## Mental model

There are two flavors of the `cld` devcontainer:

- **Master** — the one you sit in. You attach, run Claude Code, code as
  usual. Unchanged from today, plus one new ability: it can send messages
  to and receive replies from agents.
- **Agent** — a headless companion, one per repo. It hosts a single Claude
  that lives across the whole session, remembers everything it's been asked
  to do, and works through incoming tasks one at a time. Nobody talks to it
  interactively; it exists only to consume messages.

You spin up an agent explicitly, per repo, when you want it: e.g. today you
want the `lide-api` agent up because you plan to hand it tasks. Tomorrow you
don't, so you don't start it. Agents don't auto-start with masters; they're
a deliberate act.

## Sending a message

From inside your master, you tell your Claude something like "*ask the
backend agent to sanity-check this migration plan*". Your Claude drops a
message into the backend agent's mailbox. The message is deliberately
minimal: who it's from, who it's to, a subject line, a body. That's it.

## Receiving a reply

When the agent is done, it replies. The reply lands in *your* master's
mailbox. You don't have to check anything — on your next turn, a small
notice appears at the top of Claude's context: "*1 unread from lide-api:
'migration review' — Two questions: …*". You either react to it in your
next message ("*for question a, do X; for b, do Y*"), or ignore it and it
will keep showing until you deal with it. Once you've acted on it, it moves
to an archive and stops showing.

Every task you *asked* about gets exactly one reply. Mark a message
`expects_reply` and you will hear back — no silent completions, no polling.
Leave it off and you deliberately won't: an unconditional reply makes each
acknowledgment oblige another one (see `docs/design-task-agents.md` §5, D30).

## Multi-turn feels like slow chat

Because each agent has one persistent Claude session with memory, follow-up
messages "just work": the agent remembers what it said last time, so when
you write "*for question a, RESTRICT*", it knows what "a" refers to. You
don't need to re-state context.

There's no explicit "thread" concept — you're just sending messages, and
both sides carry context in their own heads. It feels like slow, deliberate
chat with a colleague across the hallway.

## When the agent needs to ask you something

If the agent's Claude needs clarification mid-task, its reply *is* the
question. You see it as an unread notice, answer it with a normal reply,
and it picks up where it left off. Same mechanism, no special mode.

## The mailbox as a place

Each container — master or agent — has one mailbox. Unread items live in
the inbox; once they've been dealt with, they move to the archive. That's
the whole structure. No folders, no threads, no priority, no read receipts.
If you want to know what's happened recently, you look at your inbox for
pending items and your archive for the trail.

## Managing agents

Simple, symmetric with masters:

- Start one: `cld devcontainer --agent` in the repo.
- Check on it: a status command shows whether it's idle or currently
  processing, how many messages it's chewed through, and its running cost.
- Read what it's been doing: a log command tails its work log.
- Stop it: shutdown, same as a master.

You don't attach to it, you don't run interactive Claude in it, you don't
have to think about the workspace inside it — it's just there, listening,
doing.

## Safety

Two guardrails:

- Every task has a generous but finite turn cap, so a single runaway task
  can't burn unlimited budget.
- The agent's commits are anchored to the point where you started it, so
  it can't rewrite history above that line — same guarantee as the
  existing autonomous agent.

## Out of scope for the POC

- **One agent per repo, explicit start.** No auto-provisioning, no
  multiple agents per repo.
- **Same machine only.** Not designed to reach across hosts.
- **No broadcast, no group chat.** Every message has one sender and one
  recipient.
- **No threads / conversation IDs.** Context lives in the agent's memory,
  not in the transport.
- **No mid-task interrupts.** Once the agent starts processing a message,
  you can't cancel it from the sender side (kill the container if you
  really need to).
- **No fancy UI.** Everything is mailbox files on disk and a couple of
  status commands.

## The one-liner

*One agent per repo, send messages, replies come back in your next turn,
agent remembers everything.*

---

# Technical design

Implementation plan following the locked-in UX above. All decisions in this
section are final for the POC unless flagged **[open]**.

## 1. Scope

- **In:** a new headless container role `cld devcontainer --agent` (one per
  repo), a symmetric mailbox transport on the host filesystem, a `messenger`
  MCP server exposing `send / list_inbox / read_message / archive /
  list_agents`, and lifecycle commands (`--agent`, `--agent restart`,
  `--agent shutdown`, `--agent status`, `--agent logs`).
- **Out:** cross-host transport, threads/conversation IDs, hooks, priority,
  broadcast, mid-task interrupts, UI beyond CLI status/logs, and dashboard.

## 2. Architecture at a glance

```
┌── master(repo=A) ──┐    ┌── agent(repo=B) ────────────┐
│  claude (user)     │    │  supervisor (python daemon) │
│  MCP: messenger    │    │    ├─ inbox watcher (FIFO)  │
│                    │    │    ├─ claude -p per msg     │
│                    │    │    └─ outbox check + fallbk │
└─── /var/cld/mb ────┘    └─── /var/cld/mb ─────────────┘
              \                    /
               \─── ~/.cld/mailboxes/ (host, bind-mounted RW into all) ─
```

Everything hangs off a single shared directory tree on the host,
bind-mounted RW into every master and every agent at the same in-container
path. The only network traffic is Docker's own — the mailbox transport is
purely filesystem.

## 3. Container roles and naming

| Role | Container name | Label `org.cld.kind` | One-per-repo |
|---|---|---|---|
| master (existing) | `cld_master_<basename>_<sha8>` | `master` | yes |
| agent (new) | `cld_agent_<basename>` | `agent` | yes (basename-unique on host) |

**Design note.** Agent skips the sha8 disambiguator on purpose (Q4). At
most one agent per basename can be up host-wide; two repos with identical
basename cannot both run agents concurrently. On collision, `cld
devcontainer --agent` fails with a clear error naming the existing
container's `org.cld.repo-root` and asks the user to shut it down or rename
their repo dir. Existing master naming is left as-is.

Both roles carry the labels `org.cld.repo-root` (host repo path) and
`org.cld.session` (== container name), same as today. `docker_master_list()`
in `cld/docker.py` gains a sibling `docker_agent_list()`; both filter on
their respective `kind` label.

## 4. Mailbox layout

**Host root:** `~/.cld/mailboxes/` (per-user; created lazily by the first
container launcher).

**In-container mount:** `/var/cld/mailboxes/` (RW, single shared tree).

**Per-container subdir:** `<container_name>/`

```
~/.cld/mailboxes/
├── cld_master_repoA_abcd1234/
│   ├── tmp/              (write-here-first, then rename into inbox/)
│   ├── inbox/            (unread; FIFO by mtime)
│   └── archive/          (dealt with)
└── cld_agent_repoB/
    ├── tmp/
    ├── inbox/
    ├── archive/
    └── outbox.log        (append-only audit trail; supervisor uses this
                           to detect whether Claude called send() during
                           a turn — see §11)
```

**Atomicity:** every sender writes the message file into
`<recipient>/tmp/<id>.json`, then `rename()`s into
`<recipient>/inbox/<id>.json`. `rename` inside the same filesystem is
atomic, so no reader ever observes a partial payload.

**FIFO:** the agent supervisor selects the next message by minimum
`stat().st_mtime` of files under its own `inbox/`.

**Archival:** the recipient's Claude (via the `archive` MCP tool) or the
agent's supervisor (post-processing) moves the file from `inbox/<id>.json`
to `archive/<id>.json`. Same-filesystem `rename` — cheap and durable.

**Mailbox creation:** on container start, `container-init.sh` mkdir -p's
its own `tmp/inbox/archive` under the shared root. If the shared root
doesn't exist inside the mount (first-ever run), the host launcher
`mkdir -p`'s it before mounting so it doesn't get created as root.

## 5. Message payload (JSON)

```
{
  "id":      "<ulid-or-uuid4>",     // filename == "<id>.json"
  "from":    "<container_name>",    // sender container name (full, incl. sha8 for masters)
  "to":      "<container_name>",    // resolved recipient container name
  "subject": "<one-line string>",
  "body":    "<free markdown>",
  "ts":      "<RFC3339 UTC>"        // creation time
}
```

Deliberately minimal. No `in_reply_to`, no `priority`, no `thread_id`, no
attachments (payload > body must be inlined). The `id` is the ULID (sortable
by time) so `ls inbox/` is naturally chronological even without stat calls
during simple inspection.

## 6. Addressing / resolution

`send(to=...)` accepts one of:

- A **shortname** (repo basename): `send(to="lide-api", …)`. Resolver
  scans `docker ps --filter label=org.cld.kind=agent`, picks the container
  whose `org.cld.repo-root` basename matches. Prefers `agent` over `master`
  when both exist for the same basename. Ambiguous match (two different
  paths, same basename) → error listing both full paths.
- A **full container name**: `send(to="cld_master_repoA_abcd1234", …)`.
  Used verbatim as the mailbox dir; also validated against
  `docker ps -a --filter label=org.cld.kind` (either kind) to catch typos.

Anyone can address anyone: masters ⇄ agents ⇄ agents ⇄ masters. Q5.

The sender always writes `from` as its own container name; the master
container looks this up via `docker inspect $HOSTNAME` (or reads from
`SESSION_NAME` env — already set by the launcher).

## 7. Agent lifecycle

New Typer wiring in `cli.py`:

```
cld devcontainer --agent                     # start (idempotent per repo)
cld devcontainer restart  --agent            # rebuild + relaunch (fresh session)
cld devcontainer shutdown --agent [--all]    # stop + remove + cleanup
cld devcontainer status   --agent            # print state.json summary
cld devcontainer logs     --agent [-n N]     # tail supervisor log
```

The `--agent` flag switches all existing subcommands into agent-mode. The
existing master-mode paths in `_run_master_devcontainer`,
`_shutdown_master_container`, and `devcontainer_restart` are refactored to
share a common `_run_persistent_devcontainer(role="master"|"agent", …)`
that parameterises on:

- container naming (`master_container_name` vs new `agent_container_name`),
- Docker label (`org.cld.kind`),
- entrypoint mode env var (`MASTER_MODE=1` vs `AGENT_MODE=1`),
- mailbox mount always present in `build_container_args`,
- the tail behaviour (master `docker exec -it`; agent detach and return).

New helpers in `cld/docker.py`:

- `agent_container_name(repo_root: Path) -> str` — returns
  `f"cld_agent_{repo_root.name}"`.
- `docker_agent_list() -> list[dict]` — mirrors `docker_master_list`,
  filter `label=org.cld.kind=agent`.
- `docker_agent_status(name) -> Literal["running","stopped","absent"]`
  — mirrors `docker_master_status`.

Anchor / workspace / VCS handling is **identical to master**: pin the
anchor at launch, create the editable_root child under
`<repo>/.cld/workspaces/<container_name>/`, bind-mount at
`/workspace/current`. The in-container `vcs_assert_descendant` guard
continues to protect the anchor.

## 8. Entrypoint changes

`imgs/claude-devcontainer/entrypoint-claude-devcontainer.sh`:

After the existing `MASTER_MODE` block, add:

```
if [ -n "${AGENT_MODE:-}" ]; then
    touch /run/cld-agent-ready              # host readiness sentinel
    exec python3 -m cld.messenger.agent_loop
fi
```

`build_claude_config` (in `container-init.sh`) grows a third rewrite branch
that points the `messenger` MCP server at
`/opt/cld/cld/mcp/messenger.py`, symmetric to how `orchestrator` and
`graphql-tester` are handled. This ensures **both** master and agent get
the `messenger` MCP available in their Claude config.

## 9. Agent supervisor (`cld/messenger/agent_loop.py`)

New Python module baked into the image via the existing
`COPY cld/ /opt/cld/cld/` in `Dockerfile.claude-base` — no image rebuild
plumbing to add.

### State machine (in-process)

```
                          ┌──────┐
                start ──▶ │KICKOFF│ ── ok ──▶ ┌──────┐
                          └──┬───┘            │ IDLE │◀────┐
                             │ fail            └──┬───┘     │
                             ▼                    │ msg     │
                          [exit]                  ▼         │
                                             ┌──────────┐   │
                                             │PROCESSING│───┘
                                             └──────────┘
```

**KICKOFF (fires on every container start; Q6):**

1. Compose kickoff prompt from `prompts/personas/repo-agent.md`
   (parameterised with repo basename, absolute path, and turn cap).
2. Invoke `claude -p --output-format json --max-turns 30 <kickoff-body>`
   with **no** `--resume`. This creates a new Claude Code session.
3. Parse `session_id` from the returned JSON; hold it in memory as
   `self.session_id`.
4. Write `state.json` (see §12): `phase=idle`, `session_id`,
   `started_at`, `msg_count=0`, `cost_usd_total=<kickoff cost>`.

**IDLE loop:**

Poll `inbox/` every 1s (simple `os.scandir` sorted by mtime — inotify
optional later). When a new message appears → PROCESSING.

**PROCESSING (one message at a time — strict FIFO, Q9→ no parallelism):**

1. Read `inbox/<id>.json`.
2. Snapshot outbox: record max mtime + set of filenames in every
   `<other>/inbox/` where a matching `from == self` might land (cheap: scan
   the mailboxes root once).
3. Update `state.json`: `phase=processing`, `current_id`, `current_from`,
   `current_subject`, `current_started_at`.
4. Invoke `claude -p --resume <session_id> --output-format json --max-turns 30 <body>`.
5. On successful return:
   - Parse JSON; accumulate `cost_usd_total`.
   - Re-scan outbox: did any new file with `from == self` land in some
     `<other>/inbox/` since the snapshot? If yes → reply satisfied.
     If no → synthesize a fallback reply via direct filesystem write:
     `send_fallback(to=msg.from, subject=f"Re: {msg.subject}",
     body="(no reply produced; last text: <last_assistant_text>)")`.
6. On non-zero exit / timeout: synthesize failure reply
   `send_fallback(to=msg.from, subject=f"Re: {msg.subject}",
   body=f"failed: {reason}")`.
7. Move `inbox/<id>.json` → `archive/<id>.json`.
8. Increment `msg_count`; write `state.json`: `phase=idle`.

**Note on VCS.** The supervisor never runs `vcs_commit` (Q9). The agent's
Claude has full access to the existing `vcs_*` MCP tools from the
`orchestrator` server and commits at its own discretion inside the turn.

### Signal handling

`SIGTERM` → finish current message if in PROCESSING, then exit cleanly
after writing `state.json` with `phase=stopped`. `SIGKILL` from the host
during shutdown is a legitimate second option; the atomicity of
`inbox/`→`archive/` rename means an interrupted turn leaves the incoming
message in `inbox/` for a future run to see. Since restart discards the
session (Q6), the same message would be re-processed with fresh context.
This is acceptable for POC.

## 10. `messenger` MCP server (`cld/mcp/messenger.py`)

FastMCP module, same shape as `orchestrator.py`. All tools operate on
`/var/cld/mailboxes/` and use the **calling container's own name** (from
`SESSION_NAME` env) as its identity.

| Tool | Signature | Notes |
|---|---|---|
| `send` | `send(to: str, subject: str, body: str) -> {"id": str}` | Resolves `to` via §6; writes `<to>/tmp/<id>.json`, then rename to `<to>/inbox/<id>.json`; appends line to own `outbox.log` (used by agent supervisor for reply detection). |
| `list_inbox` | `list_inbox(unread_only: bool = True) -> [{id, from, subject, ts}]` | Lists own `inbox/` (unread) or `archive/` when `unread_only=False`. Sorted by `ts`. |
| `read_message` | `read_message(id: str) -> {id, from, to, subject, body, ts}` | Full read of one message. Searches `inbox/` then `archive/`. |
| `archive` | `archive(id: str) -> {"ok": true}` | Moves `inbox/<id>.json` → `archive/<id>.json`. No-op if already archived. |
| `list_agents` | `list_agents() -> [{name, kind, repo, status}]` | Runs `docker ps -a --filter label=org.cld.kind=agent`. Also enumerates masters if callers ask (`kind` filter param — omit for both). |

`send` and `archive` are the only mutations; both are single-file atomic
renames.

The `messenger` server is added to both master and agent Claude configs
via the same path-rewrite mechanism as `orchestrator` (§8).

## 11. Reply detection

Q2 + Q8 + Q12 combine to give the following contract:

- The agent's Claude uses the same `send()` tool as everyone else. There
  is no separate `reply()` — a "reply" is just a `send()` targeting the
  original message's `from`.
- The supervisor guarantees the always-reply contract by watching
  **outbox activity per turn**, not the tool call itself:
  - `send()` appends one line to the sender's `outbox.log` (id, to, ts).
  - Before invoking `claude -p`, the supervisor snapshots the current
    number of `outbox.log` lines.
  - After `claude -p` returns, it re-reads the line count. If the count
    grew, some `send()` was made — reply requirement satisfied (even if
    the Claude sent to somewhere other than the original sender; that's
    an odd but valid choice).
  - If the count didn't grow, the supervisor synthesizes the fallback
    reply directly to `msg.from` (bypassing the MCP tool but using the
    same atomic-rename path).

This means the agent's Claude does not need to be prompted "you must call
`reply`". It just does its work and sends whatever messages it wants; if
it forgets to send anything, the supervisor fills in.

## 12. Agent status file

Written by the supervisor to `/var/cld/mailboxes/<self>/state.json`
(host-visible), consumed by `cld devcontainer status --agent`.

```
{
  "container_name":  "cld_agent_lide-api",
  "repo_root":       "/home/user/projects/lide-api",
  "phase":           "idle" | "processing" | "kickoff" | "stopped",
  "session_id":      "<claude session uuid>",
  "started_at":      "<RFC3339>",
  "msg_count":       12,
  "cost_usd_total":  1.83,
  "current": {
    "id":         "<msg id>",
    "from":       "<sender>",
    "subject":    "...",
    "started_at": "<RFC3339>"
  } | null
}
```

Atomic write via `write + rename` (tmp file in same dir).

## 13. Master's inbox UX

Master's Claude uses `list_inbox()` / `read_message(id)` / `archive(id)`
directly (Q7 — MCP only, no hook). The user's natural interaction is:

- "*any replies?*" → Claude calls `list_inbox()`.
- "*show me the migration one*" → Claude calls `read_message(<id>)`.
- User acts on it, then "*done with that*" → Claude calls `archive(<id>)`.

No automatic surfacing at prompt-submit time in the POC.

## 14. Kickoff prompt template

New file: `prompts/personas/repo-agent.md`.

Contents (parameterized at supervisor read time with `{repo_basename}`,
`{repo_abs_path}`, `{max_turns}`, `{container_name}`):

- Explains the agent's role (persistent Claude for this repo, receives
  messages from other containers).
- Points at available tools: `messenger` (send/list_inbox/read_message/
  archive), `orchestrator` (vcs_*, jj_*, etc.), and the standard
  Read/Write/Edit/Bash.
- Behaviour rules:
  - For each incoming message, do the work, then use `send()` to reply
    to the sender.
  - You are allowed and expected to commit code via `vcs_commit` when
    appropriate; supervisor does not commit for you.
  - Turn cap is `{max_turns}` per message.
- Anchor discipline mirrors the existing agent persona.

The supervisor reads this file once at KICKOFF, substitutes the
placeholders, and passes it as the first `claude -p` body (no
`--append-system-prompt` — kickoff via user turn is enough).

## 15. `build_container_args` changes

- Always mount `/var/cld/mailboxes` RW when either master or agent mode
  is active (behind a single `--label` gate: `org.cld.kind` is set).
- Host source path: `to_host_path(str(Path.home() / ".cld/mailboxes"), cfg)`.
- Ensure `~/.cld/mailboxes` exists on the host before mount (host launcher
  `mkdir -p` in the master/agent launch path).
- Pass `AGENT_MODE=1` when launching the agent, mirroring `MASTER_MODE=1`.

No other changes to `build_container_args` — the existing docker socket
mount, claude session mount, and label scheme carry over.

## 16. Configuration additions

`cld/config.py` gains:

| Field | Default | Purpose |
|---|---|---|
| `mailbox_root` | `~/.cld/mailboxes` | Host root; overridable via `CLD_MAILBOX_ROOT`. |
| `agent_max_turns` | `30` | Per-message turn cap for the agent supervisor's `claude -p`. |
| `agent_kickoff_persona` | `repo-agent` | Persona name resolved through the same lookup as chain personas. |

All three are optional; defaults match the locked-in POC choices.

## 17. Files to add / change

### New

| File | Purpose |
|---|---|
| `cld/messenger/__init__.py` | Package marker. |
| `cld/messenger/agent_loop.py` | Supervisor daemon (`python -m cld.messenger.agent_loop`). |
| `cld/messenger/mailbox.py` | Shared filesystem helpers (path building, atomic send, snapshot, list, archive). Used by both MCP tools and supervisor. |
| `cld/mcp/messenger.py` | FastMCP server exposing `send / list_inbox / read_message / archive / list_agents`. |
| `prompts/personas/repo-agent.md` | Kickoff prompt template. |

### Changed

| File | Change |
|---|---|
| `cld/cli.py` | Add `--agent` flag to `devcontainer`, `shutdown`, `restart`; add `status --agent`, `logs --agent` subcommands; refactor `_run_master_devcontainer` → `_run_persistent_devcontainer(role=…)`. |
| `cld/docker.py` | Add `agent_container_name`, `docker_agent_list`, `docker_agent_status`; extend `build_container_args` with mailbox mount (both roles) and `AGENT_MODE` env. |
| `cld/config.py` | Add three fields from §16 + `from_env` wiring. |
| `imgs/claude-devcontainer/entrypoint-claude-devcontainer.sh` | Add `AGENT_MODE` branch invoking `python -m cld.messenger.agent_loop`; touch `/run/cld-agent-ready`. |
| `imgs/claude-devcontainer/container-init.sh` | Add `messenger` MCP rewrite in `build_claude_config`; `mkdir -p /var/cld/mailboxes/$SESSION_NAME/{tmp,inbox,archive}`. |

No changes to `imgs/claude-base/Dockerfile.claude-base` — `cld/` is already
copied to `/opt/cld/`. No changes to `imgs/claude-agent/` (that image is
for the separate one-shot `cld agent` command and is unrelated).

## 18. Implementation task list (for the implementer agent)

Ordered; each item is independently testable.

1. **Mailbox library** (`cld/messenger/mailbox.py`). Path helpers, atomic
   `write_message`, `list_inbox`, `read_message`, `archive`, `outbox_snapshot` /
   `outbox_changed_since`, `list_containers` (Docker label queries). Pure
   Python, unit-testable with tmpdirs.
2. **Messenger MCP server** (`cld/mcp/messenger.py`). Wraps §1 in FastMCP
   tools. Reads own identity from `SESSION_NAME` env. Manual-test via
   `stdio` from a shell.
3. **Config wiring** (`cld/config.py`). Add three fields + env vars +
   TOML support.
4. **`build_container_args` mailbox mount + `AGENT_MODE`** (`cld/docker.py`).
   Add helpers `agent_container_name`, `docker_agent_list`,
   `docker_agent_status`.
5. **Entrypoint + container-init changes**. Add `AGENT_MODE` branch,
   mailbox `mkdir -p`, `messenger` MCP path rewrite.
6. **Kickoff persona**. Write `prompts/personas/repo-agent.md` with the
   four placeholders.
7. **Agent supervisor** (`cld/messenger/agent_loop.py`). State machine
   from §9; JSON parsing of `claude -p --output-format json`;
   `state.json` writer. Manual-test by launching bare-metal (skip
   Docker) against a temp mailbox root.
8. **`cld devcontainer --agent` launcher + refactor**. Extract shared
   persistent-container function; add `--agent` on the four subcommands;
   wire status/logs.
9. **End-to-end smoke.** Start a master and an agent in two different
   repos; from the master, `send(to="<agent-basename>", …)`; verify
   reply lands in master's inbox; check `state.json` and agent
   `outbox.log`.

Each step should keep the codebase green (typer app boots, existing tests
pass) before proceeding to the next.

## 19. Open questions [none]

All 12 design questions closed. If additional questions arise during
implementation (e.g., exact `--output-format json` shape from the current
Claude Code build), the implementer should verify empirically and
document deviations at the top of the affected file.

