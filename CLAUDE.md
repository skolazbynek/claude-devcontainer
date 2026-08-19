# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

Tooling for running Claude Code in Docker containers with VCS workspace isolation. Supports **jujutsu (jj)** natively and falls back to **git** when jj is not installed.

> **Developer workflows / use-cases:** see `docs/workflows-brainstorm.md` for the end-to-end, multi-command developer journeys this tool is meant to support (hub, backlog burn-down, chain, cross-repo change, standing teammate).

- **Ephemeral devcontainer** (`cld` bare) -- Interactive session with neovim, jj/git, poetry. Drops into bash with `--dangerously-skip-permissions`. Container is `--rm`.
- **Persistent master** (`cld master`) -- Persistent per-repo interactive devcontainer. Start-or-attach; idempotent per repo. Same interactive shell as the ephemeral one, but stays up so packages/history/state persist across attaches.
- **Persistent repo agent** (`cld agent`) -- Persistent per-repo headless Claude agent. Runs the `claude-devcontainer` image with `AGENT_MODE=1`; the entrypoint execs the supervisor daemon (`python -m cld.messenger.agent_loop`). Receives tasks via the mailbox/messenger transport (see "Messenger" below and `docs/design-agent-messaging.md`). One long-lived Claude session per repo.
- **Task-scoped agents** (`cld task-agent`) -- Many per repo, one per task, bounded lifespan, master-owned lifecycle. Same `claude-devcontainer` image and supervisor as the repo agent, with `TASK_AGENT_MODE=1` layering a composed kickoff prompt (lifecycle preamble + role persona + the task), a durable **deliverable branch** distinct from the session bookmark, master-drawn **peer edges** with a hop budget, and three reap-readiness checks on teardown. Full design: `docs/design-task-agents.md`; implementation notes: `docs/impl-task-agents-plan.md`. From inside a master the lifecycle verbs are mediated by the host broker's `task-agent` action; reading the fleet is not (the mailbox is bind-mounted).
- **One-shot run** (`cld run`) -- Headless *one-shot* autonomous agent. Takes a task file and/or inline prompt, runs detached in the `claude-run` image, commits results to a VCS branch, then its container exits (`--rm`).

## Architecture

```
cld/                             -- Python package (host-side CLI + shared logic)
  cli.py                         -- host typer app (needs a docker daemon)
  cli_container.py               -- container typer app, installed as `cld` in the
                                    devcontainer image (docs/design-cli-split.md)
  cli_msg.py                     -- the `msg` sub-app + the shared error decorator,
                                    registered on both apps (host and container)
  task_agent.py                  -- task-agent helpers both apps use (name resolution,
                                    roster/detail rendering, peer specs, mailbox root)
  prompts.py                     -- prompt-ref interface: `@ref` resolution (escape-checked),
                                    frontmatter stripping, brief composition, listing
  docker.py                      -- container setup: arg building, image management, path translation
  broker.py                      -- host-vs-broker seam: local docker on host, SSH broker actions inside a container (no docker socket in containers)
  run.py                         -- one-shot run launch logic (`cld run`)
  agent_runtime.py               -- shared agent lifecycle helpers (wait, cost, formatting)
  chain.py                       -- declarative multi-step chain orchestrator
  chain_state.py                 -- chain run state serialisation for detached execution
  vcs/                           -- VCS abstraction layer
    base.py                      -- abstract VcsBackend interface
    jj.py                        -- jujutsu backend implementation
    git.py                       -- git backend implementation (fallback)
    detect.py                    -- auto-detection: jj preferred, git fallback
    anchor.py                    -- anchor resolution + descendant guard
    scratch.py                   -- `.cld-run/` scratch staging + envelope encode/decode
  mcp/
    orchestrator.py              -- (deprecated) MCP server for orchestrating Docker agents. Not wired into any image or host claude; kept for reference.
    messenger.py                 -- MCP server for the mailbox transport (send/list_inbox/read_message/archive/list_agents)
    graphql.py                   -- MCP server for GraphQL API testing (server lifecycle + queries)
  messenger/
    mailbox.py                   -- Filesystem mailbox transport (pure, unit-testable with tmpdirs)
    agent_loop.py                -- Repo agent supervisor daemon (`python -m cld.messenger.agent_loop`)
    identity.py                  -- who the caller is: own session in a container, cwd repo's master on the host
    send/inbox/read/archive/agents.py -- one thin module per `msg` verb, wrapping mailbox.py
  bridge/                        -- chat bridges: edge adapters over the mailbox transport
    mattermost.py                -- the daemon (`cld bridge mattermost`); one tick = drain
                                    own inbox, poll channel, check what we are still owed
    daemon.py                    -- detached start/stop/status/logs, PID file + log under
                                    ~/.cld/bridge (same shape as broker/cld-brokerctl.sh)
    client.py                    -- Mattermost REST over httpx (the only network code)
    routing.py                   -- pure: rejection filters, @name resolution, chunking
    fleet.py                     -- classify_target: who can answer and who cannot
    state.py                     -- durable cursor / seen posts / thread map
scripts/
  mcp/run-orchestrator.sh        -- (deprecated) thin venv wrapper; kept for reference
  mcp/run-messenger.sh           -- Thin venv wrapper for the messenger MCP server
imgs/
  claude-base/                   -- Common base image (debian, git, jj, poetry, docker CLI, mysql client, claude). No editor, no entrypoint.
  claude-devcontainer/           -- Devcontainer image (FROM base, adds neovim + vim + entrypoint)
    container-init.sh            -- Shared container init (sourced by both entrypoints, baked into base)
    vcs-lib.sh                   -- Shell-level VCS abstraction (sourced by both entrypoints, baked into base)
  claude-run/                    -- One-shot run image (FROM base, adds one-shot entrypoint + system prompt)
    Dockerfile.claude-run
    entrypoint-claude-run.sh
    run-system-prompt.md
prompts/
  personas/                      -- Persona system prompts (architect, implementer, reviewer, …)
                                    plus two lifecycle personas that frame a headless
                                    agent's contract: agent.md (standing repo agent) and
                                    task-agent.md (bounded task-agent preamble)
  (other task prompts)           -- Reusable task prompts for agents
chains/                          -- YAML chain definitions (e.g. architect-implement-review.yaml)
runtests/                        -- Standalone test-runner container (no cld dependency): pytest @ a jj revision
broker/                          -- Host-side SSH broker glue (ForceCommand dispatcher cld-broker.sh,
                                    operator control cld-brokerctl.sh, sshd sample, keygen)
```

**Image hierarchy:** `claude-base` is the parent of both `claude-devcontainer` and `claude-run` (siblings). Build base first.

**Two CLIs, one package (`docs/design-cli-split.md`).** `cld` on the host is
`cld.cli:app` (poetry entry point) and carries every verb that needs a docker daemon.
`cld` **inside a container** is `cld.cli_container` -- a shim baked into the
devcontainer image (`~/.local/bin/cld` → `python3 -P -m cld.cli_container`) whose
surface is only what a container can reach: `task-agent` (broker; `transcript` off the
mailbox), `agent` (broker), `repos`, `msg send|inbox|read|archive|agents` (mailbox),
`prompts`. Host-only verbs are hidden stubs that say "host-only". `python3 -m cld`
inside a container refuses and points at `cld`. `msg` is the one surface both apps
share: it lives in `cld/cli_msg.py` and is registered on each, so `cld msg …` also
works on the host, acting as the cwd repo's master (`cld.messenger.identity.resolve_self`).

**Shared logic lives in three places:**
- Host side: `cld/docker.py` -- imported by all commands. Provides `build_container_args`, `find_repo_root`, `ensure_image`, `build_session_name`, logging.
- Host side: `cld/vcs/` -- VCS abstraction layer. `get_backend()` returns a `JjBackend` or `GitBackend` depending on what's available.
- Container side: `imgs/claude-devcontainer/container-init.sh` + `vcs-lib.sh` -- sourced by both entrypoints. Sets up mysql wrapper and VCS-agnostic workspace functions.

**VCS detection order:**
1. If `.jj/` directory exists AND `jj` binary is available -> jujutsu backend
2. If `.git/` directory exists AND `git` binary is available -> git backend
3. Error

**Workspace isolation:** Containers mount the host repo RW at `/workspace/origin`. The isolated agent workspace lives at `/workspace/current` inside the container's ephemeral filesystem (a real directory, not a symlink). The container entrypoint runs `jj workspace add /workspace/current -r <AGENT_ANCHOR_HASH>` on boot; jj writes into the origin's `.jj/repo/store` via the RW bind mount at `/workspace/origin`. No `.cld/workspaces/<session>` directory is ever created on the host. Watchman is enabled inside the workspace (`fsmonitor.backend=watchman`, `register-snapshot-trigger=true`) so background file changes get autonomously snapshotted into jj. A bookmark named `<SESSION_NAME>` tracks `@`; jj auto-advances it through rewrites. On `docker rm && docker run` (i.e. `cld master restart` / `cld agent restart`), the container entrypoint sees the existing bookmark, forgets the stale workspace registration, and re-adds a fresh workspace pointed at the bookmark's last tip -- so uncommitted-but-snapshotted edits survive across restarts even though the workspace directory does not. **Lifecycle contract:** bookmark `<SESSION_NAME>` in the origin store exists iff a live-or-restart-paused lifecycle owns the session. `cld <role> shutdown` (unlike `restart`) forgets that bookmark, so the next `cld <role>` launch is a fresh lifecycle that honors `-r/--revision` again. Committed work and watchman snapshots from the previous session remain in the store (`jj log -r 'heads(all())'` to find them), but the named pointer does not.

**Master sibling targets (`master_targets`):** Registered host paths master can launch peer containers against. Each path is materialized inside master as an *empty placeholder directory* (no bind mount, no repo content) so `cd <path>` succeeds; `cld agent` (or `cld run`, `cld master`, bare `cld`) inside master's shell then launches a peer with `-v <host_path>:/workspace/origin:rw` against the host filesystem. Master has no filesystem view of any sibling repo. Anchor resolution and scratch staging both run in the peer container's entrypoint (master passes `AGENT_REVISION_HINT` + `AGENT_SCRATCH`, peer does the jj work locally where its view is RW). Bookmark forget on shutdown also runs in the peer via SIGTERM handler. `cld repos` (container CLI) lists mountable targets. See `docs/design-master-sibling-launch.md`. `cld chain run` is not yet supported from inside master.

**Session naming:** All commands accept `-n/--name`. Names are prefixed per mode: `cld_`, `agent_`, `review_`. Passed into containers as `SESSION_NAME` env var. Entrypoints use it for branches, workspaces, and log directories.

## Key Commands

```bash
# Build images (base first; cld build does this automatically)
docker build -f imgs/claude-base/Dockerfile.claude-base -t claude-base:latest .
docker build -f imgs/claude-devcontainer/Dockerfile.claude-devcontainer -t claude-devcontainer:latest .
docker build -f imgs/claude-run/Dockerfile.claude-run -t claude-run:latest imgs/claude-run

# Ephemeral interactive devcontainer (-p only; prompt refs live on `run`/`task-agent
# start`/`chain run`, since click reads a group callback's first positional as a
# subcommand name)
cld [-n name] [-m model] [-r revision] [-p prompt]

# Persistent master devcontainer (per-repo, start-or-attach; idempotent)
cld master                             # start or re-attach
cld master restart                     # tear down + relaunch, preserving workspace
cld master shutdown [--all]            # stop + remove + drop workspace
cld master status
cld master logs [-n N]

# Persistent repo agent (per-repo, headless, mailbox-driven)
cld agent                              # start (idempotent per repo); never attaches
cld agent restart                      # tear down + relaunch, preserving workspace
cld agent shutdown [--all]             # stop + remove + drop workspace
cld agent status                       # docker + supervisor state.json summary
cld agent logs [-n N]                  # tail the container's log (= supervisor stderr)

# Task-scoped agents (many per repo, one per task; see docs/design-task-agents.md)
cld task-agent start [refs...] -n <slug> [-p prompt] \
    [--branch <name>] [-m model] [-r revision] [--peer <name>[:<hops>]]...
cld task-agent status [<name>]         # roster (host-wide), or one agent in detail
cld task-agent logs <name> [-n N]      # supervisor stderr -- NOT the conversation
cld task-agent transcript <name>       # the mailbox conversation (works after a reap)
cld task-agent shutdown <name>         # stop + rm, forget session bookmark, archive mailbox
cld task-agent shutdown --all [--force]
#   -n/--name is the task slug; the container is cld_agent_<repo>_<slug>. Every
#   `start` makes a new container (no start-or-attach). <name> takes a bare slug,
#   resolved against the cwd's repo. `shutdown` is gated by three reap-readiness
#   checks (not mid-turn, not a live peer, own fleet only); `--force` is host-only.
#   Inside master: start/status/logs/shutdown go through the broker (which stamps
#   --parent, so the roster and `--all` are scoped to that master's fleet, and
#   refuses --force); `transcript` reads the mounted mailbox directly.

# One-shot autonomous run
cld run [refs...] [-n name] [-m model] [-r revision] [-p prompt]

```

## Configuration

All Python-side runtime tunables live in `cld/config.py:Config` (frozen dataclass). Each Typer command and MCP tool constructs `Config.from_env()` once at entry and passes it explicitly down the call chain (Variant A: explicit DI, no global).

**Resolution order (lowest → highest priority):** dataclass defaults < user TOML (`~/.config/cld/config.toml`) < project TOML (`<repo_root>/.cld/config.toml`, walked up from cwd) < `.env` in cwd < `CLD_*` env vars.

TOML uses flat snake_case keys mirroring `Config` field names (`base_image`, `devcontainer_image`, `run_image`, `mysql_config`, `ssl_certs_path`, `agent_timeout`, `poll_interval`, `debug`, `home_mounts_always`, `home_mounts_devcontainer`, `master_targets`, `chain_max_parallel`, `chain_default_model`, `log_level`, `log_color`, `ignore_gitignore`, `ssh_auth_sock`, `mailbox_root`, `agent_max_turns`, `agent_kickoff_persona`, `max_task_agents`, `peer_absolute_limit`, `root_ask_limit`, `broker_key`, `broker_endpoint`, `broker_known_hosts`). Array fields accept TOML arrays of strings. Unknown keys are warned about on stderr and ignored. `host_project_dir` / `host_home` are container-internal and not configurable via TOML. One exception to the field-mirroring rule: `pyproject_dir` is accepted (silently, not warned on) but has no `Config` field -- it's consumed only by `cld-broker.sh`'s own TOML parser, not by Python, for the host test broker's `PROJECT_SUBDIR` (see "Host-side test running" below).

`CLD_*` env vars (read by `Config.from_env`):

| Var | Default | Purpose |
|---|---|---|
| `CLD_BASE_IMAGE` | `claude-base:latest` | Common base Docker image |
| `CLD_DEVCONTAINER_IMAGE` | `claude-devcontainer:latest` | Devcontainer image |
| `CLD_RUN_IMAGE` | `claude-run:latest` | Agent image |
| `CLD_MYSQL_CONFIG` | `""` | Path to a `.cnf` file, mounted ro at `/run/secrets/mysql.cnf` |
| `CLD_SSL_CERTS_PATH` | `""` | Opt-in override: host path (dir or PEM file) that **replaces** the baked CA bundle. Empty = use baked bundle (internal Seznam CAs + Debian defaults). No auto-detect. |
| `CLD_HOST_PROJECT_DIR` | `""` | Set by host launcher into containers; lets in-container Python translate `/workspace/*` paths back to host paths for sibling `-v` mounts |
| `CLD_HOST_HOME` | `""` | Same idea for `$HOME` paths |
| `CLD_AGENT_TIMEOUT` | `1800` | Chain's per-agent wait timeout (seconds) |
| `CLD_POLL_INTERVAL` | `30` | Chain's docker-ps poll interval (seconds) |
| `CLD_CHAIN_MAX_PARALLEL` | `4` | Max parallel siblings launched concurrently in a chain group |
| `CLD_CHAIN_DEFAULT_MODEL` | `""` | Model override for chain agents; empty = agent default |
| `CLD_LOG_LEVEL` | `INFO` | Root level for the `cld` logger hierarchy. Accepts DEBUG/INFO/WARNING/ERROR (case-insensitive; WARN aliased to WARNING). |
| `CLD_LOG_COLOR` | `auto` | ANSI color in log output: `auto` (TTY-detect), `always`, or `never`. |
| `CLD_DEBUG` | `false` | Diagnostics flag. Back-compat alias: when truthy and `CLD_LOG_LEVEL` is unset, equivalent to `CLD_LOG_LEVEL=DEBUG`. |
| (no env var) | `()` | `ignore_gitignore` is TOML-only -- gitignored files to symlink from origin into workspace. Set in `.cld/config.toml` as array: `ignore_gitignore = [".env"]`. |
| `CLD_SSH_AUTH_SOCK` | unset | SSH agent forwarding. Tri-state: **unset** = auto-detect from host `$SSH_AUTH_SOCK`; **empty** (`""`) = explicitly disable; **path** = use that socket. Forwarded to `/run/host-ssh-agent.sock` inside the container. Applies to every devcontainer-image launch the CLI makes -- bare `cld`, `cld master`, and the headless agents too (they need it to push a deliverable branch); `stage_ssh_agent` itself is role-agnostic. |
| `CLD_MAILBOX_ROOT` | `~/.cld/mailboxes` | Host root of the inter-container mailbox tree; bind-mounted RW into every master and agent container. |
| `CLD_AGENT_MAX_TURNS` | `120` | Per-message turn cap passed to the agent supervisor's `claude -p --max-turns`. Passed into agent/task-agent containers by `build_container_args` (in-container `Config.from_env()` sees no host TOML). Hitting it is **not** fatal: the session is kept and the next message gets a fresh budget. |
| `CLD_AGENT_KICKOFF_PERSONA` | `agent` | Persona name (resolved like chain personas) used to kick off a new repo-agent Claude session. |
| `CLD_MAX_TASK_AGENTS` | `4` | Max **running** task-agents per master, enforced host-side at spawn (see `docs/design-task-agents.md`). |
| `CLD_PEER_ABSOLUTE_LIMIT` | `10` | Absolute hop budget for a peer edge whose `--peer <name>[:<hops>]` spec omits one. |
| `CLD_ROOT_ASK_LIMIT` | `3` | Questions that may be open at once on one peer edge before a further `expects_reply` send is refused. Bounds the clarification regress; clears when the exchange's root question is answered. |
| `CLD_BROKER_KEY` | `""` | Host path to the restricted broker **private** key. When set, `master`, `agent`, and `task-agent` containers mount it RO and make `cld broker <action>` work (host-side test running via the `runtests` container). Empty = off. Agents/task-agents are prompt-instructed to use it only with master's explicit per-run authorization (not enforced by the broker itself). |
| `CLD_BROKER_ENDPOINT` | `host.docker.internal:2222` | Broker SSH endpoint `[user@]host:port` (default login user `zet`). |
| `CLD_BROKER_KNOWN_HOSTS` | `""` | Host path to the pinned `known_hosts` for the broker; mounted RO. Required for the client's strict host-key check. |
| `CLD_MATTERMOST_URL` | `""` | Mattermost server base URL. Empty = the bridge is not configured. Host-only. |
| `CLD_MATTERMOST_TOKEN_FILE` | `""` | Path to the bot's personal access token. A **path**, never a value; the bridge refuses to start if the file is group- or world-readable. |
| `CLD_MATTERMOST_CHANNEL_ID` | `""` | The one channel the bridge reads and writes. |
| (no env var) | `()` | `mattermost_allowed_user_ids` is TOML-only -- the allowlist of Mattermost **user ids** (not usernames, which are mutable). Empty = the bridge refuses to start. |
| `CLD_MATTERMOST_POLL_INTERVAL` | `3` | Seconds between channel polls (one tick). |
| `CLD_MATTERMOST_REPLY_TIMEOUT` | `900` | Seconds before the bridge reports an agent that accepted a message but never answered. |
| `CLD_MATTERMOST_MAX_POST_CHARS` | `15000` | Chunk threshold; verify against the server's `MaxPostSize`. |
| `CLD_MATTERMOST_STATE_FILE` | `~/.cld/mattermost-bridge.json` | Durable cursor, seen post ids and thread map. |
| `CLD_BRIDGE_DIR` | `~/.cld/bridge` | Where `cld bridge start` keeps the daemon's PID file and log. Read directly by `cld/bridge/daemon.py`, not a `Config` field. |

Container-side env vars consumed by shell entrypoints (NOT read by Python `Config`; left unprefixed because shell scripts read them by name):

| Var | Where set | Purpose |
|---|---|---|
| `SESSION_NAME` | `build_container_args` -> container | Branch/workspace name |
| (the brief) | launcher -> `.cld-run/brief.md` | The composed prompt: N refs in order, then `-p`, written into the anchor scratch rather than mounted (`docs/design-prompt-chaining.md`) |
| `AGENT_MODEL` | launcher -> container | Claude model |
| `AGENT_ANCHOR_HASH` | launcher -> container | Anchor commit hash; in-container guard enforces all session changes descend from it |
| `WORKSPACE_PREINITIALIZED` | launcher -> container | Always `1`; host pre-creates the workspace as an empty editable_root child of the anchor |
| `WORKSPACE_FILES` | `build_container_args` -> container | Colon-separated list of gitignored files to symlink from origin into workspace (set from config `ignore_gitignore`) |
| `AGENT_COMMIT_MSG_LLM` | user -> container | Optional agent override |
| `MYSQL_DEFAULTS_FILE` | `build_container_args` -> container | Credentials path inside container |
| `TASK_AGENT_MODE` | `build_container_args(task_agent=…)` -> container | Marks a task-scoped agent. A *modifier* on `AGENT_MODE` (same mailbox precondition, readiness sentinel and supervisor exec), not a fourth mode: the entrypoint additionally creates the deliverable bookmark and seeds `known_hosts`, and skips the one-shot pre-run of the task prompt (the supervisor's kickoff owns it) |
| `AGENT_TASK_SLUG` / `AGENT_PARENT_MASTER` / `AGENT_DELIVERABLE_BRANCH` / `AGENT_PEERS` | `build_container_args(task_agent=…)` -> container | Task-agent spawn facts the supervisor turns into `meta.json`. `AGENT_PEERS` is `name:hops` pairs, **comma**-separated (`:` is the pair delimiter, so the usual colon-separated list convention doesn't fit) |
| `CLD_BROKER_ENDPOINT` | `stage_broker` -> master/agent/task-agent | Broker endpoint `[user@]host:port`; presence (with the mounted key) is what makes `cld broker` available in-container |
| `WORKSPACE_ORIGIN` | `container-init.sh` -> Python | `/workspace/origin` (read by `vcs/detect.py`) |

**Logging.** `cld/log.py` configures a stderr handler on the `cld.*` logger hierarchy. All modules use `log = get_logger(__name__)`. Logs go to stderr only — stdout is reserved for user-facing deliverables (final reports, list rows, prompts). Levels: DEBUG (verbose, every subprocess + VCS call), INFO (default; major lifecycle events), WARNING (recoverable issues), ERROR (failed operations).

## Workspace files (gitignored files in containers)

When running `cld` (bare, ephemeral) or agents, the workspace is isolated in a jj/git working copy at `/workspace/current`, separate from the host repo mount at `/workspace/origin`. Gitignored files like `.env` don't automatically appear in the isolated workspace.

To symlink gitignored files (like `.env` or `.envrc`) from origin into the workspace, set `ignore_gitignore` in `.cld/config.toml`:

```toml
ignore_gitignore = [".env", ".envrc"]
```

On container startup, `link_workspace_files()` symlinks each file from `/workspace/origin` into `/workspace/current`. If a file doesn't exist in origin, a warning is logged and linking continues. Symlinks are transparent to applications and reflect updates to the origin file.

## The cld broker (`runtests` + host-side actions)

Running the target repo's tests needs MySQL/Redis/etc. credentials from a gitignored `.env`, which must never reach the container or claude's ambient environment. `master`, `agent`, and `task-agent` containers each trigger a fixed host-side command over SSH that runs the tests in a separate ephemeral container, keeping secrets **entirely off the container**. Full design + rationale: `docs/design-cld-broker.md`. Three decoupled pieces:

- **`runtests/`** — a standalone, self-contained container project (own Debian-slim Dockerfile + `entrypoint.sh`, no cld dependency; movable to its own repo). Single job: `jj workspace add -r <REVISION>` into an isolated workspace under `$HOME` (never touching the origin's `@`), source a mounted `.env`, `poetry install`, then `pytest "$@"`. Contract is env + argv only: `-v <repo>:/repo`, `REVISION` (default `@`), `-v <.env>:/secrets/.env:ro`, `PROJECT_SUBDIR`, arbitrary pytest argv. Ships **Poetry 2.x** (reads PEP 621 `[project]`). Output is bounded for context hygiene: `PYTEST_ADDOPTS` defaults to `--tb=short --disable-warnings -q --maxfail=30` (explicit argv wins) and `OUTPUT_MAX_BYTES` (default 64 KiB) caps the returned log to its tail. Build with `runtests/build.sh`.
- **`broker/`** — host-side glue: `cld-broker.sh` is an sshd `ForceCommand` target invoked as `<action> <session> <base64-argv>`. It dispatches the action to a shell function `action_<name>` (adding an action = defining a function; unknown ⇒ denied), validates the session (`^cld_(master|agent)_…$` -- the latter covers both the standing repo agent and task-agents, discriminated by the `org.cld.kind` label, not the name), and decodes the argv from base64(NUL-joined) without `eval`. **Multi-repo, no whitelist:** it resolves the target repo from the calling container's host-set `org.cld.repo-root` label (`docker inspect <session>`), then (for `run-tests`) resolves that session's current change from the jj store (store-reading only, never moving the working copy). Per-action context is resolved lazily inside each `action_*` (via `resolve_test_context` for run-tests), so read-only actions don't depend on the session bookmark. Secrets default to `<repo>/.env`, overridable by `pyproject_dir` in the repo's own `.cld/config.toml` (also gives `PROJECT_SUBDIR`). The default `run_tests` action does `docker run --rm runtests …` with that `.env` mounted (skipped if absent). Three more actions replace the (now-removed) in-container docker socket: **`list-containers`** (read-only cld-container enumeration for the messenger / `cld agent status`), **`agent`** (`<target> <op>` -- launch/manage a sibling `cld agent` host-side) and **`task-agent`** (`<target> <op>` -- the `cld task-agent` lifecycle verbs). Both launcher actions validate `<target>` against the calling session's host-set `org.cld.repo-root` + `org.cld.targets` labels via the shared `validate_target` -- **this keeps `agent`/`task-agent` effectively master-only** even though the session regex now also admits agent/task-agent callers: `org.cld.targets` (from `master_targets`) is only ever set on a `cld master` container, so an agent or task-agent session always fails `validate_target` for lack of any registered target; only `run-tests` and `list-containers` are actually reachable from those roles. `task-agent` additionally polices its argv, because it is the one action that creates a container with a caller-chosen file mounted in it: **`--force` is denied** (overriding a reap-readiness refusal stays a human act), a caller-supplied **`--parent` is denied** and the validated `$session` appended instead (so an agent's recorded owner is host-set), and every positional of `start` must be an `@ref` -- a bare path would let a container read any host file the user can, since refs are resolved host-side and composed into the new container's brief (`cld.prompts` refuses a ref that escapes the prompts tree; the container client folds its own local files into `-p`). The container side reaches all of these through `cld broker <action>`; `cld/broker.py` is the Python seam that routes enumeration/lifecycle to the broker when `in_master_container()`. Broker config is broker-wide only (`RUNTESTS_IMAGE`, `PATH` -- `PATH` must include `cld` for the `agent` action -- and `SSH_AUTH_SOCK`, which both launcher actions export via `stage_agent_socket` so a broker-launched agent can push its deliverable branch: sshd hands the forced command no agent socket of its own, and connection agent-forwarding is not a substitute because that socket dies with the ssh session while the container outlives it). Ships with a sample hardened `sshd_cld_broker.conf`, `keygen.sh`, and `cld-brokerctl.sh` (operate the sshd: `start`/`restart`/`shutdown`/`status`/`logs`, detached, PID-tracked under `$CLD_BROKER_DIR`). Setup: `broker/README.md`.
- **cld plumbing** — `stage_broker(cfg)` (in `build_container_args`, called for `master`, `agent`, and `task_agent` containers) mounts the restricted key (+ pinned known_hosts) RO, adds `host.docker.internal:host-gateway`, and sets `CLD_BROKER_ENDPOINT`, which is what makes `cld broker <action>` (`cld/broker.py`) work in-container. Config: `broker_key` (enables it), `broker_endpoint` (`[user@]host:port`), `broker_known_hosts`.

Usage: `cld broker run-tests -k login -x tests/` -- **always via this client, never a hand-built `ssh` call** (see the `broker-run-tests` skill, `.claude/skills/broker-run-tests/`, which any Claude session with the broker configured should already have loaded via `--add-dir /opt/cld`). Available from `master`, `agent`, and `task-agent` containers, but **agents and task-agents are instructed by their personas** (`prompts/personas/agent.md`, `prompts/personas/task-agent.md`) **to only invoke it with their master's explicit authorization for that specific run** -- this is a prompt-level policy, not a technical gate; the broker itself does not distinguish an authorized call from an unauthorized one once a session is admitted. Secret isolation depends on the docker socket being **absent** from every cld container (so the broker is claude's only host channel); the broker is not pinned to a repo (§ multi-repo above), so a leaked key unlocks the broker's actions for any repo that has a running master, agent, or task-agent, not just this one.

## Anchor change contract

Every subcommand (`cld`, `master`, `agent`, `run`, `chain run`) shares one notion of an **anchor change**: an immutable revision from which all command-created changes descend, and -- per the anchor descendant-tree contract -- the only boundary on what a container may touch: any change descending from the anchor is fair game, including changes that already existed before the container was spawned, not just ones the container itself creates. Default is the current change (`@`); override with `-r`/`--revision`. The host only *resolves* the base revision `A` (via `resolve_anchor` in `cld/vcs/anchor.py`); the origin working copy is never touched -- crucial for the common jj case where the user's `@` **is** `A`. Everything else runs **inside the peer container's ephemeral workspace** at `/workspace/current`: after `jj workspace add --name <session> -r <A> /workspace/current`, `python -m cld.vcs.scratch` writes the scratch payload under `.cld-run/*` and runs `jj commit -m "cld anchor: <session>" .cld-run`, producing a scratch commit `B` (child of `A`, carrying only `.cld-run/*`) and advancing the workspace's `@` to a fresh empty child of `B` where the agent operates. `AGENT_ANCHOR_HASH` is set to `A` itself, **not** `B` -- so a pre-existing descendant of `A` that sits outside `B`'s own lineage (e.g. a sibling commit made before the container spawned) is still inside the container's editable tree. The in-container `vcs_assert_descendant` guard (in `vcs-lib.sh`) refuses to commit or squash if `@` no longer descends from `A`.

`.cld-run/` is a reserved directory that only ever exists **inside the container's workspace** (never on the origin filesystem). Scratch content (task descriptions, personas, patches) is structurally rooted in the anchor tree and readable by the agent from `/workspace/current/.cld-run/*`. The host repo is not required to mark `.cld-run/` as tracked or non-gitignored.

Wire between host and peer:
- `AGENT_REVISION_HINT` -- the resolved commit hash of `A` (or an unresolved revset string when a `cld master` container delegates to a peer that has RW view of the target repo). The peer entrypoint runs `jj log -r "$AGENT_REVISION_HINT" -T commit_id` to pin it, then `jj workspace add -r <that hash>`.
- `AGENT_SCRATCH` -- base64 envelope of `{path-under-.cld-run/ : bytes}`, produced by `encode_scratch_envelope`.
- `AGENT_ANCHOR_HASH` is **not** sent by the host; it is computed peer-side after the workspace exists and exported by the entrypoint for downstream consumers (agent-loop, descendant guard, `summary.json`).

Shared helpers: `cld/vcs/anchor.py` (`resolve_anchor`, `assert_descendant`) and `cld/vcs/scratch.py` (`stage_in_workspace`, `stage_from_env`, envelope encode/decode). Anchor persistence across `cld master restart` / `cld agent restart` is provided by the jj bookmark named `<session>` in the origin's store: the container entrypoint detects it and reattaches by pointing a fresh workspace at the bookmark's last tip. On reattach, `AGENT_ANCHOR_HASH` is recovered by walking the bookmark's ancestors for the commit whose description is `"cld anchor: <session>"` (that finds scratch commit `B`) and taking its parent (`A`). The peer-staging code path is jj-only; the git backend has no equivalent.

## Agent Output

Agent containers are `--rm` (auto-removed on exit). Results are committed to the agent's branch as `agent-output-<session-name>/`: `agent.log`, `result.json`, `summary.json`. Callers read these via `VcsBackend.file_show()`.

Inspect with jj: `jj log -r <name>`, `jj diff -r <name>`. Merge: `jj squash --from <name>`.
Inspect with git: `git log <name>`, `git diff <name>~1..<name>`. Merge: `git merge <name>`.

## Notes

- All commands require a **VCS repository** (jj or git). They walk up from cwd to find `.jj/` or `.git/`.
- Containers run as host UID/GID with security hardening (cap-drop ALL, no-new-privileges, resource limits).
- The agent entrypoint merges global MCP server config from `~/.claude.json` into project scope.
- **The baked-in skills reach claude through a PATH wrapper, not through claude's own config.** The entrypoint writes `/tmp/bin/claude` (adding `--dangerously-skip-permissions --add-dir /opt/cld`, which is what auto-loads `/opt/cld/.claude/skills/`; `permissions.additionalDirectories` grants file access only and does *not* load skills). `container-init.sh` exports `/tmp/bin` onto the PATH of the entrypoint's own process tree -- the headless supervisor -- while `/etc/profile.d/cld-path.sh` (baked into `claude-base`) does the same for the `docker exec -it … bash -l` shell a `cld master` attach lands in. Without that second hook an interactive session runs the raw binary and sees neither the skills nor the credentialed `mysql` wrapper.
- Install with `poetry install` to get the `cld` command.
- Logging is centralised in `cld/log.py`; each module obtains a logger via `get_logger(__name__)`.

## MCP Orchestrator (deprecated)

`cld/mcp/orchestrator.py` and `scripts/mcp/run-orchestrator.sh` remain in the tree for reference but are not wired into any image, host claude config, or persona. Do not add new callers; use the `messenger` MCP for inter-agent coordination instead. Tests for the orchestrator module are skipped (`tests/test_orchestrator.py`, `tests/test_log.py::test_mcp_orchestrator_stdout_is_clean`).

## Messenger (inter-container agent messaging)

Full design: `docs/design-agent-messaging.md`. One-line summary: one repo agent per repo, `send()`/`list_inbox()`/`read_message()`/`archive()` via the `messenger` MCP server, replies come back on your next turn, the agent remembers everything across messages (one persistent `claude -p --resume` session).

- `cld/messenger/mailbox.py` -- pure filesystem transport (atomic `tmp/` write + `rename()` into `inbox/`; `archive/`; append-only `outbox.log`, whose lines carry the full subject+body so one mailbox is a complete transcript). No MCP/Docker coupling: `list_containers()` lazily delegates to `cld/broker.py`'s `list_cld_containers()` (local docker on host, `list-containers` broker action inside master) and `resolve_recipient()` short-circuits to filesystem delivery when the recipient names an existing mailbox dir (the reply path -- so agents message back with no host channel). Unit-testable with `tmp_path`. Also owns the task-agent registry surface (`meta.json` spawn facts via `ensure_meta`/`list_fleet`, `state.json` reads, `transcript()`, `_archive/<name>/` on teardown, and the `_edges/` hop counters plus obligation ledger) -- see `docs/design-task-agents.md`. Root entries starting with `_` are reserved, not mailboxes.
- `cld/messenger/agent_loop.py` -- the supervisor daemon (`python -m cld.messenger.agent_loop`, execed as PID 1 by the entrypoint's `AGENT_MODE` branch). State machine: `KICKOFF` (once, via `prompts/personas/<agent_kickoff_persona>.md`, default `agent.md`) -> `IDLE` (poll `inbox/` every 1 s) -> `PROCESSING` (one message, strict FIFO via oldest mtime) -> `IDLE`, until `SIGTERM`. Writes `state.json` into its own mailbox dir after every transition. The kickoff prompt goes to claude on **stdin**, not in argv -- a persona's leading `---` frontmatter was otherwise parsed as an unknown option; frontmatter is stripped either way (`cld.prompts.strip_frontmatter`). Under `TASK_AGENT_MODE` the same state machine runs in **task mode** (`TaskMode.from_env()`): kickoff is `compose_kickoff()`'s two layers -- the lifecycle preamble (`prompts/personas/task-agent.md`, placeholders substituted), then the brief the launcher composed (`.cld-run/brief.md`, verbatim, role persona already inside it) -- and `meta.json` is written once at boot. No extra phase; see `docs/design-task-agents.md` §5, §11.
- `cld/mcp/messenger.py` -- FastMCP server wrapping `mailbox.py`. `send`/`list_inbox`/`read_message`/`archive` operate on the calling container's own mailbox, identified by `SESSION_NAME`. Two additive **fleet** tools are master surfaces instead, scoped to mailboxes whose `meta.json` records the caller as parent: `fleet_digest()` (one cheap row per task-agent -- task, phase, msg_count, cost, unread, last_activity, plus `open_asks`/`open_with`/`oldest_open`; no bodies) and `read_mailbox(name, since="")` (the full exchange, `inbox/` + `archive/` received and `outbox.log` sent, `since` exclusive, archived mailboxes included). The digest is what makes the master's per-turn crank affordable; sweeping inboxes would find nothing, since an agent archives each message within ~1 s.
- **Hop gate (agent-to-agent only).** A message between two task-agents counts against that edge's absolute budget in `_edges/<sorted>.json`; past the limit the send is refused and **nothing more is ever delivered over that edge** -- not a retry, not a supervisor-synthesized reply, no cap notice (docs/design-task-agents.md D29: exempting the messages that announce the end is what loops). The blocked side escalates to its master, whose channel is a *different* edge and never budgeted. The gate lives in `mailbox.gated_send` and the closure rule one level down in `write_message`, because **two** instructed paths reach the transport -- the MCP `send()` tool and `cld msg send`, which the baked-in `messenger-send` skill documents. `edge_spent` is asked *before* a delivery and `bump_edge` counts *after* it, so the limit-th message is the last one that lands rather than the first one refused.
- **Reply obligation is declared, not implied.** Every message carries `expects_reply` (does this open an obligation on the recipient) and `answers` (which message id does it discharge), both set by the sender and defaulted to "no". Arrival alone obliges nothing: an unconditional "every message gets a reply" makes each acknowledgment oblige another one, which in testing had two agents trading courtesies until the hop budget ran out. There is no special case for the master channel -- the master sets `expects_reply` like anyone else. A reply *may* also ask (`answers` + `expects_reply` together), which is what makes a clarification sub-dialogue work: the root obligation survives it and is discharged separately.
- **Reply guarantee (recipient-scoped, obligation-gated):** the supervisor snapshots its own `outbox.log` line count before invoking Claude, then -- **only if the incoming message set `expects_reply`** -- checks whether a line addressed to *this message's sender* appeared afterward (`mailbox.replied_since`, not the tool call itself); if not, it synthesizes a fallback so a question always gets an answer. Scoping to the sender matters once an agent has peers: a send to a peer, or an escalation to the master, must not discharge the reply owed to whoever it is answering. The guarantee is **bounded by the edge budget**: a fallback is a real delivery on the edge, so it consumes a hop while the edge is open and is refused once it is spent (which is what keeps the guarantee from becoming a loop engine -- see the hop gate above). A claude *failure* is reported to the sender unconditionally: it is information no other channel gives them, not an acknowledgment.
- **Ask gate (agent-to-agent only).** A second budget on the same edge file, bounding the **clarification regress** -- two agents that keep asking each other for clarification and never answer the question they started from. `open` holds the ids of unanswered questions on the edge, `asks` counts obligation-opening sends while any of them is open, and `asks` clears only when `open` empties (so discharge-and-reopen cannot reset it, which is why a depth cap would not catch this shape). Past `cfg.root_ask_limit`, `mailbox.ask_spent` refuses **the ask, not the edge**: answers and plain informs still deliver, so a graceful landing is always available -- unlike the hop budget, where it has to fit inside the ceiling. The refusal names the two ways out: answer with a stated assumption, or escalate to the master. `fleet_digest` surfaces `open_asks` / `open_with` / `oldest_open` so the master sees a stall forming before the gate fires; a regress usually means the master under-specified one of the two tasks, and the master is the only party who can resolve that.
- **Mailbox mount:** `/var/cld/mailboxes` in-container (`cld.docker.MAILBOX_MOUNT`), bind-mounted from `cfg.mailbox_root` on the host (`~/.cld/mailboxes` by default) whenever `build_container_args(..., master=True)` or `(..., agent=True)`. `cfg.mailbox_root` is a **host-side** path only -- in-container code always uses the fixed `MAILBOX_MOUNT` constant, never `Config.mailbox_root`.
- **Empirical note on `claude -p --output-format json`:** verified against a live Claude Code 2.1.198 build (see top of `agent_loop.py`) -- the cost field is `total_cost_usd`, not `cost_usd` as the existing stub fixtures under `tests/fixtures/stub-*` assume; `--max-turns` works despite being undocumented in `claude --help` output.
- **Naming to keep straight:** the persistent repo agent lives under `cld agent` (uses the `claude-devcontainer` image with `AGENT_MODE=1`); the one-shot autonomous agent lives under `cld run` (uses the `claude-run` image, `--rm`). They share no code path except `build_container_args`/`session_workspace_path`.
- **Deviation from the design doc, verified empirically:** readiness sentinels live at `/tmp/cld-{master,agent}-ready`, not `/run/...` as originally specced -- `/run` is root-owned `755` in the base image, so a non-root container (`--user <uid>:<gid>`) can never `touch` a file there (pre-existing bug shared with `--master`, fixed for both here).
- **Nested mailbox root is the user's problem, by design.** When `cld` itself runs nested inside another container (its own `CLD_HOST_PROJECT_DIR`/`CLD_HOST_HOME` set), `build_container_args` cannot create the mailbox root on the real host. (No docker socket is mounted into any container anymore, so there is no cross-socket shortcut to reach the host filesystem even if one were wanted -- container launches from inside master go through the host broker, which runs host-native `cld` where this nested branch does not apply.) `build_container_args` only `mkdir`s when it can verify its own filesystem view *is* the host view (`to_host_path` is a no-op); otherwise it logs a warning with the exact `mkdir -p && chown` command to run on the real host and proceeds to mount the (possibly not-yet-existing) path as-is. `container-init.sh`'s `ensure_own_mailbox` fails loudly in-container (and aborts `AGENT_MODE` startup) if that path turns out to be missing or wrong-owned, rather than silently degrading.

## Mattermost bridge (chat with the fleet)

Full design + plan: `docs/impl-mattermost-bridge-plan.md`. Run it on the host:
`cld bridge start|stop|restart|status|logs` for the detached daemon (PID file + log
under `~/.cld/bridge`, no init system -- same shape as `cld-brokerctl`), or
`cld bridge mattermost [--once]` in the foreground to debug. One responsibility:
**deliver messages from a private
Mattermost channel to an agent's mailbox and its replies back.** Its whole contract
is that every message you send either produces a reply in the thread, or a notice
saying why it never will.

- **Not a tool claude holds.** The bridge is a host process with a mailbox identity
  of its own (`mattermost`, a normal mailbox dir under `cfg.mailbox_root`). No token
  enters a container, no MCP server is added, and no agent gains a capability. It
  sends with `expects_reply=True`, so the supervisor's reply guarantee and synthesized
  fallback (`agent_loop.py`) land in the channel rather than nowhere.
- **No controller agent.** Posts route straight to a named agent: `@<name> …` at
  channel root opens a thread, replies in that thread continue it with no prefix, and
  `!fleet` lists the **live agents and attended masters** on every repo so you can
  always name one. `!fleet` is the one deliberate exception to the single
  responsibility -- you cannot address an agent you cannot name. The filter is a
  *display* filter: `fleet_rows` stays complete because it is also the name-resolution
  list, so a crashed or reaped agent remains addressable and answers with the reason
  it cannot reply.
- **Strict pre-flight** (`bridge/fleet.py:classify_target`). A mailbox directory is not
  an agent: a crashed container leaves its mailbox behind, and a reaped agent lives
  under `_archive/`. The bridge classifies before sending and refuses in-channel with
  the reason. Masters write no `state.json` (only `AgentSupervisor` does -- a master
  deliberately gets no supervisor of its own, since a headless loop running `claude -p`
  in the same workspace a human attaches to interactively would race with them) but are
  classified `attended` rather than refused: delivery still queues in their mailbox,
  and a person answers via the `messenger-*` skills the next time they attach --
  `write_message` doesn't care who replies, so it posts back into the thread exactly
  like an agent's reply would. `running_containers()` returns `None` when docker is
  unreachable, and a `None` liveness view never concludes "crashed" -- otherwise a
  daemon restart floods the channel with refusals.
- **Three ways a delivery fails**, one post each, no progress reporting in between:
  refused pre-flight; the container dies after accepting (detected from `docker ps` on
  the next tick, not a timer -- `state.json` is written only on transitions and is *not*
  a heartbeat); or the supervisor wedges while alive (`mattermost_reply_timeout`).
- **The bridge's edges are unbudgeted** because it writes no `meta.json`, so `peer_edge`
  is False in `write_message`. Giving it one would silently make every fleet
  conversation hop-budgeted; `tests/test_bridge.py::test_bridge_edges_are_unbudgeted`
  is the tripwire.
- **Any container can post to the channel** by sending to `mattermost` --
  `resolve_recipient` short-circuits to filesystem delivery for anyone with the mailbox
  mounted. That is deliberate (an agent can flag a blocker directly), which is why every
  post names `msg["from"]`.

## Chain Orchestrator

Module: `cld/chain.py`. Declarative multi-step pipeline runner. Entry point is `cld chain <file.yaml>` via `cli.py`.

**Dataclasses** (all frozen):

| Class | Role |
|---|---|
| `ChainStep` | Single agent step: name, prompts (ordered refs), model, prompt, output, inputs, timeout |
| `ParallelGroup` | A group of `ChainStep` siblings to run in parallel |
| `ChainDefaults` | Chain-level defaults for model and timeout |
| `Chain` | Top-level: name, description, defaults, ordered `steps` tuple |
| `StepResult` | Outcome of one step: status, output_text, failure_md, cost, duration |
| `ChainResult` | Outcome of the whole chain: aggregated results, branch name, success flag |

**Public functions:**

| Function | Purpose |
|---|---|
| `load_chain(path)` | Parse YAML into `Chain`; structural only, no file-system checks |
| `validate_chain(chain, repo_root, cld_root)` | Semantic validation: name regex, unique step names, persona resolution, no forward `inputs` refs |
| `run_chain(cfg, chain_file, ...)` | Orchestrate all steps; returns `ChainResult` |
| `print_chain_report(result, vcs)` | Print summary table with cost, duration, VCS inspect commands |

**Shared helpers** (`cld/agent_runtime.py`):
- `wait_for_agent(session_name, vcs, cfg)` -- polls `docker ps` until container exits, reads `summary.json`.
- `read_agent_cost(session, vcs)` -- reads `result.json` for `cost_usd`.
- `format_duration(seconds)` -- formats as `Xm00s`.

**Prompt refs per step:** Each step declares `prompts:` -- an ordered list of refs (`@personas/architect`, `@some-task`, or a path), personas and task files interchangeable -- plus an optional inline `prompt:`. `compose_task()` builds one brief per step: the refs first (they carry the role the system-prompt mount used to), then the step prompt, the initial task, prior step outputs, and the output-path footer. There is no system-prompt override and no host-side scratch staging; the brief travels in the anchor scratch envelope (`docs/design-prompt-chaining.md`).

**Branch model:**
- A persistent `chain_<name>` branch is created at the start and acts as the accumulator.
- Each step's agent runs off a transient `chain_<name>_<step>` branch (parallel: `chain_<name>_<idx>_<step>`).
- On step success, `advance_chain_branch()` moves the accumulator to the step's tip and deletes the transient branch.
- On failure the accumulator stays at its last good position; transient branches are left for inspection.

**Parallel concurrency:** `_run_parallel()` serializes `launch_agent()` calls (avoids Docker race conditions), then waits on all siblings sequentially. The effective parallelism is Docker-side: all containers run concurrently even though Python's wait loop is sequential. `CLD_CHAIN_MAX_PARALLEL` caps the number of siblings per group.

**Chain YAML files** live in `chains/`. Persona files live in `prompts/personas/`. Step outputs are written to `.cld/chain-outputs/<chain-name>/` inside the agent's workspace.
