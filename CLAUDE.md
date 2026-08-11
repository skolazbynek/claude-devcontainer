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
  cli.py                         -- typer app, all subcommands
  docker.py                      -- container setup: arg building, image management, path translation
  host_docker.py                 -- host-vs-broker seam: local docker on host, SSH broker actions inside master (no docker socket in containers)
  run.py                         -- one-shot run launch logic (`cld run`)
  agent_runtime.py               -- shared agent lifecycle helpers (wait, cost, formatting)
  chain.py                       -- declarative multi-step chain orchestrator
  vcs/                           -- VCS abstraction layer
    base.py                      -- abstract VcsBackend interface
    jj.py                        -- jujutsu backend implementation
    git.py                       -- git backend implementation (fallback)
    detect.py                    -- auto-detection: jj preferred, git fallback
  mcp/
    orchestrator.py              -- (deprecated) MCP server for orchestrating Docker agents. Not wired into any image or host claude; kept for reference.
    messenger.py                 -- MCP server for the mailbox transport (send/list_inbox/read_message/archive/list_agents)
  messenger/
    mailbox.py                   -- Filesystem mailbox transport (pure, unit-testable with tmpdirs)
    agent_loop.py                -- Repo agent supervisor daemon (`python -m cld.messenger.agent_loop`)
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
host-broker/                     -- Host-side SSH broker glue for runtests (ForceCommand script, sshd sample, keygen)
```

**Image hierarchy:** `claude-base` is the parent of both `claude-devcontainer` and `claude-run` (siblings). Build base first.

**Shared logic lives in three places:**
- Host side: `cld/docker.py` -- imported by all commands. Provides `build_container_args`, `find_repo_root`, `ensure_image`, `build_session_name`, logging.
- Host side: `cld/vcs/` -- VCS abstraction layer. `get_backend()` returns a `JjBackend` or `GitBackend` depending on what's available.
- Container side: `imgs/claude-devcontainer/container-init.sh` + `vcs-lib.sh` -- sourced by both entrypoints. Sets up mysql wrapper and VCS-agnostic workspace functions.

**VCS detection order:**
1. If `.jj/` directory exists AND `jj` binary is available -> jujutsu backend
2. If `.git/` directory exists AND `git` binary is available -> git backend
3. Error

**Workspace isolation:** Containers mount the host repo RW at `/workspace/origin`. The isolated agent workspace lives at `/workspace/current` inside the container's ephemeral filesystem (a real directory, not a symlink). The container entrypoint runs `jj workspace add /workspace/current -r <AGENT_ANCHOR_HASH>` on boot; jj writes into the origin's `.jj/repo/store` via the RW bind mount at `/workspace/origin`. No `.cld/workspaces/<session>` directory is ever created on the host. Watchman is enabled inside the workspace (`fsmonitor.backend=watchman`, `register-snapshot-trigger=true`) so background file changes get autonomously snapshotted into jj. A bookmark named `<SESSION_NAME>` tracks `@`; jj auto-advances it through rewrites. On `docker rm && docker run` (i.e. `cld master restart` / `cld agent restart`), the container entrypoint sees the existing bookmark, forgets the stale workspace registration, and re-adds a fresh workspace pointed at the bookmark's last tip -- so uncommitted-but-snapshotted edits survive across restarts even though the workspace directory does not. **Lifecycle contract:** bookmark `<SESSION_NAME>` in the origin store exists iff a live-or-restart-paused lifecycle owns the session. `cld <role> shutdown` (unlike `restart`) forgets that bookmark, so the next `cld <role>` launch is a fresh lifecycle that honors `-r/--revision` again. Committed work and watchman snapshots from the previous session remain in the store (`jj log -r 'heads(all())'` to find them), but the named pointer does not.

**Master sibling targets (`master_targets`):** Registered host paths master can launch peer containers against. Each path is materialized inside master as an *empty placeholder directory* (no bind mount, no repo content) so `cd <path>` succeeds; `cld agent` (or `cld run`, `cld master`, bare `cld`) inside master's shell then launches a peer with `-v <host_path>:/workspace/origin:rw` against the host filesystem. Master has no filesystem view of any sibling repo. Anchor resolution and scratch staging both run in the peer container's entrypoint (master passes `AGENT_REVISION_HINT` + `AGENT_SCRATCH`, peer does the jj work locally where its view is RW). Bookmark forget on shutdown also runs in the peer via SIGTERM handler. `cld master repos` lists mountable targets. See `docs/design-master-sibling-launch.md`. `cld chain run` is not yet supported from inside master.

**Session naming:** All commands accept `-n/--name`. Names are prefixed per mode: `cld_`, `agent_`, `review_`. Passed into containers as `SESSION_NAME` env var. Entrypoints use it for branches, workspaces, and log directories.

## Key Commands

```bash
# Build images (base first; cld build does this automatically)
docker build -f imgs/claude-base/Dockerfile.claude-base -t claude-base:latest .
docker build -f imgs/claude-devcontainer/Dockerfile.claude-devcontainer -t claude-devcontainer:latest .
docker build -f imgs/claude-run/Dockerfile.claude-run -t claude-run:latest imgs/claude-run

# Ephemeral interactive devcontainer
cld [-n name] [-m model] [-r revision] [-p prompt] [task-file.md]

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
cld task-agent start @<persona> [task.md|@<name>] -n <slug> [-p prompt] \
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
cld run [-n name] [-m model] [-r revision] [-p prompt] [task-file.md|@<name>]

```

## Configuration

All Python-side runtime tunables live in `cld/config.py:Config` (frozen dataclass). Each Typer command and MCP tool constructs `Config.from_env()` once at entry and passes it explicitly down the call chain (Variant A: explicit DI, no global).

**Resolution order (lowest → highest priority):** dataclass defaults < user TOML (`~/.config/cld/config.toml`) < project TOML (`<repo_root>/.cld/config.toml`, walked up from cwd) < `.env` in cwd < `CLD_*` env vars.

TOML uses flat snake_case keys mirroring `Config` field names (`base_image`, `devcontainer_image`, `run_image`, `mysql_config`, `ssl_certs_path`, `agent_timeout`, `poll_interval`, `debug`, `home_mounts_always`, `home_mounts_devcontainer`, `master_targets`, `chain_max_parallel`, `chain_default_model`, `log_level`, `log_color`, `ignore_gitignore`, `mailbox_root`, `agent_max_turns`, `agent_kickoff_persona`, `host_broker_key`, `host_broker_endpoint`, `host_broker_known_hosts`). Array fields accept TOML arrays of strings. Unknown keys are warned about on stderr and ignored. `host_project_dir` / `host_home` are container-internal and not configurable via TOML. One exception to the field-mirroring rule: `pyproject_dir` is accepted (silently, not warned on) but has no `Config` field -- it's consumed only by `host-broker.sh`'s own TOML parser, not by Python, for the host test broker's `PROJECT_SUBDIR` (see "Host-side test running" below).

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
| `CLD_IGNORE_GITIGNORE` | `""` | Colon-separated list of gitignored files to symlink from origin into workspace (e.g. `.env:.envrc`). Set in `.cld/config.toml` as array: `ignore_gitignore = [".env"]`. |
| `CLD_SSH_AUTH_SOCK` | unset | SSH agent forwarding. Tri-state: **unset** = auto-detect from host `$SSH_AUTH_SOCK`; **empty** (`""`) = explicitly disable; **path** = use that socket. Forwarded to `/run/host-ssh-agent.sock` inside the container. Applies to every devcontainer-image launch the CLI makes -- bare `cld`, `cld master`, and the headless agents too (they need it to push a deliverable branch); `stage_ssh_agent` itself is role-agnostic. |
| `CLD_MAILBOX_ROOT` | `~/.cld/mailboxes` | Host root of the inter-container mailbox tree; bind-mounted RW into every master and agent container. |
| `CLD_AGENT_MAX_TURNS` | `30` | Per-message turn cap passed to the agent supervisor's `claude -p --max-turns`. |
| `CLD_AGENT_KICKOFF_PERSONA` | `agent` | Persona name (resolved like chain personas) used to kick off a new repo-agent Claude session. |
| `CLD_MAX_TASK_AGENTS` | `4` | Max **running** task-agents per master, enforced host-side at spawn (see `docs/design-task-agents.md`). |
| `CLD_PEER_ABSOLUTE_LIMIT` | `10` | Absolute hop budget for a peer edge whose `--peer <name>[:<hops>]` spec omits one. |
| `CLD_HOST_BROKER_KEY` | `""` | Host path to the restricted broker **private** key. When set, `cld master` mounts it RO and installs the `host-run` wrapper (host-side test running via the `runtests` container). Empty = off. Master-only. |
| `CLD_HOST_BROKER_ENDPOINT` | `host.docker.internal:2222` | Broker SSH endpoint `[user@]host:port` (default login user `zet`). |
| `CLD_HOST_BROKER_KNOWN_HOSTS` | `""` | Host path to the pinned `known_hosts` for the broker; mounted RO. Required for `host-run`'s strict host-key check. |

Container-side env vars consumed by shell entrypoints (NOT read by Python `Config`; left unprefixed because shell scripts read them by name):

| Var | Where set | Purpose |
|---|---|---|
| `SESSION_NAME` | `build_container_args` -> container | Branch/workspace name |
| `INSTRUCTION_FILE` | agent launch -> container | Task file path |
| `AGENT_MODEL` | launcher -> container | Claude model |
| `AGENT_ANCHOR_HASH` | launcher -> container | Anchor commit hash; in-container guard enforces all session changes descend from it |
| `WORKSPACE_PREINITIALIZED` | launcher -> container | Always `1`; host pre-creates the workspace as an empty editable_root child of the anchor |
| `WORKSPACE_FILES` | `build_container_args` -> container | Colon-separated list of gitignored files to symlink from origin into workspace (set from config `ignore_gitignore`) |
| `AGENT_COMMIT_MSG_LLM` / `AGENT_SYSTEM_PROMPT_FILE` | user -> container | Optional agent overrides |
| `MYSQL_DEFAULTS_FILE` | `build_container_args` -> container | Credentials path inside container |
| `TASK_AGENT_MODE` | `build_container_args(task_agent=…)` -> container | Marks a task-scoped agent. A *modifier* on `AGENT_MODE` (same mailbox precondition, readiness sentinel and supervisor exec), not a fourth mode: the entrypoint additionally creates the deliverable bookmark and seeds `known_hosts`, and skips the one-shot pre-run of the task prompt (the supervisor's kickoff owns it) |
| `AGENT_TASK_SLUG` / `AGENT_PARENT_MASTER` / `AGENT_DELIVERABLE_BRANCH` / `AGENT_PEERS` | `build_container_args(task_agent=…)` -> container | Task-agent spawn facts the supervisor turns into `meta.json`. `AGENT_PEERS` is `name:hops` pairs, **comma**-separated (`:` is the pair delimiter, so the usual colon-separated list convention doesn't fit) |
| `CLD_HOST_BROKER` | `stage_host_broker` -> master | Broker endpoint `[user@]host:port`; presence (with the mounted key) triggers `container-init.sh` to install the `host-run` wrapper |
| `WORKSPACE_ORIGIN` | `container-init.sh` -> Python | `/workspace/origin` (read by `vcs/detect.py`) |

**Logging.** `cld/log.py` configures a stderr handler on the `cld.*` logger hierarchy. All modules use `log = get_logger(__name__)`. Logs go to stderr only — stdout is reserved for user-facing deliverables (final reports, list rows, prompts). Levels: DEBUG (verbose, every subprocess + VCS call), INFO (default; major lifecycle events), WARNING (recoverable issues), ERROR (failed operations).

## Workspace files (gitignored files in containers)

When running `cld` (bare, ephemeral) or agents, the workspace is isolated in a jj/git working copy at `/workspace/current`, separate from the host repo mount at `/workspace/origin`. Gitignored files like `.env` don't automatically appear in the isolated workspace.

To symlink gitignored files (like `.env` or `.envrc`) from origin into the workspace, set `ignore_gitignore` in `.cld/config.toml`:

```toml
ignore_gitignore = [".env", ".envrc"]
```

On container startup, `link_workspace_files()` symlinks each file from `/workspace/origin` into `/workspace/current`. If a file doesn't exist in origin, a warning is logged and linking continues. Symlinks are transparent to applications and reflect updates to the origin file.

## Host-side test running (`runtests` + host broker)

Running the target repo's tests needs MySQL/Redis/etc. credentials from a gitignored `.env`, which must never reach the master container or claude's ambient environment. `master` triggers a fixed host-side command over SSH that runs the tests in a separate ephemeral container, keeping secrets **entirely off the container**. Full design + rationale: `docs/design-host-test-running.md`. Three decoupled pieces:

- **`runtests/`** — a standalone, self-contained container project (own Debian-slim Dockerfile + `entrypoint.sh`, no cld dependency; movable to its own repo). Single job: `jj workspace add -r <REVISION>` into an isolated workspace under `$HOME` (never touching the origin's `@`), source a mounted `.env`, `poetry install`, then `pytest "$@"`. Contract is env + argv only: `-v <repo>:/repo`, `REVISION` (default `@`), `-v <.env>:/secrets/.env:ro`, `PROJECT_SUBDIR`, arbitrary pytest argv. Ships **Poetry 2.x** (reads PEP 621 `[project]`). Output is bounded for context hygiene: `PYTEST_ADDOPTS` defaults to `--tb=short --disable-warnings -q --maxfail=30` (explicit argv wins) and `OUTPUT_MAX_BYTES` (default 64 KiB) caps the returned log to its tail. Build with `runtests/build.sh`.
- **`host-broker/`** — host-side glue: `host-broker.sh` is an sshd `ForceCommand` target invoked as `<action> <session> <base64-argv>`. It dispatches the action to a shell function `action_<name>` (adding an action = defining a function; unknown ⇒ denied), validates the session (`^cld_master_…$`), and decodes the argv from base64(NUL-joined) without `eval`. **Multi-repo, no whitelist:** it resolves the target repo from the calling master container's host-set `org.cld.repo-root` label (`docker inspect <session>`), then (for `run-tests`) resolves that session's current change from the jj store (store-reading only, never moving the working copy). Per-action context is resolved lazily inside each `action_*` (via `resolve_test_context` for run-tests), so read-only actions don't depend on the session bookmark. Secrets default to `<repo>/.env`, overridable by `pyproject_dir` in the repo's own `.cld/config.toml` (also gives `PROJECT_SUBDIR`). The default `run_tests` action does `docker run --rm runtests …` with that `.env` mounted (skipped if absent). Three more actions replace the (now-removed) in-container docker socket: **`list-containers`** (read-only cld-container enumeration for the messenger / `cld agent status`), **`agent`** (`<target> <op>` -- launch/manage a sibling `cld agent` host-side) and **`task-agent`** (`<target> <op>` -- the `cld task-agent` lifecycle verbs). Both launcher actions validate `<target>` against the master's host-set `org.cld.repo-root` + `org.cld.targets` labels via the shared `validate_target`. `task-agent` additionally polices its argv, because it is the one action that creates a container with a caller-chosen file mounted in it: **`--force` is denied** (overriding a reap-readiness refusal stays a human act), a caller-supplied **`--parent` is denied** and the validated `$session` appended instead (so an agent's recorded owner is host-set), and `start`'s persona positional must be a bare name -- a path would let a container read any host file the user can (`cld.prompts.persona_resolve` rejects it too). The container side reaches all of these through the `host-run` wrapper; `cld/host_docker.py` is the Python seam that routes enumeration/lifecycle to the broker when `in_master_container()`. Broker config is broker-wide only (`RUNTESTS_IMAGE`, `PATH` -- `PATH` must include `cld` for the `agent` action). Ships with a sample hardened `sshd_cld_broker.conf`, `keygen.sh`, and `brokerctl.sh` (operate the sshd: `start`/`restart`/`shutdown`/`status`/`logs`, detached, PID-tracked under `$CLD_BROKER_DIR`). Setup: `host-broker/README.md`.
- **cld plumbing** — `stage_host_broker(cfg)` (master-only, in `build_container_args`) mounts the restricted key (+ pinned known_hosts) RO, adds `host.docker.internal:host-gateway`, and sets `CLD_HOST_BROKER`; `container-init.sh` then installs a `host-run` wrapper. Config: `host_broker_key` (enables it), `host_broker_endpoint` (`[user@]host:port`), `host_broker_known_hosts`.

Usage from inside master: `host-run -k login -x tests/` -- **always via this wrapper, never a hand-built `ssh` call** (see the `host-run-tests` skill, `.claude/skills/host-run-tests/`, which any Claude session inside a master with the broker configured should already have loaded via `--add-dir /opt/cld`). Secret isolation depends on the docker socket being **absent** from master (so `host-run` is claude's only host channel); the broker is not pinned to a repo (§ multi-repo above), so a leaked key unlocks `run-tests` for any repo that has a running master, not just this one.

## Anchor change contract

Every subcommand (`cld`, `master`, `agent`, `run`, `chain run`) shares one notion of an **anchor change**: an immutable revision from which all command-created changes descend. Default is the current change (`@`); override with `-r`/`--revision`. The host only *resolves* the base revision `A` (via `resolve_anchor` in `cld/vcs/anchor.py`); the origin working copy is never touched -- crucial for the common jj case where the user's `@` **is** `A`. Everything else runs **inside the peer container's ephemeral workspace** at `/workspace/current`: after `jj workspace add --name <session> -r <A> /workspace/current`, `python -m cld.vcs.scratch` writes the scratch payload under `.cld-run/*` and runs `jj commit -m "cld anchor: <session>" .cld-run`, producing a dedicated child commit `B` of `A`. `B`'s hash is captured into `AGENT_ANCHOR_HASH`, and the workspace's `@` advances to a fresh empty child of `B` where the agent operates. The in-container `vcs_assert_descendant` guard (in `vcs-lib.sh`) refuses to commit or squash if `@` no longer descends from `B`.

`.cld-run/` is a reserved directory that only ever exists **inside the container's workspace** (never on the origin filesystem). Scratch content (task descriptions, personas, patches) is structurally rooted in the anchor tree and readable by the agent from `/workspace/current/.cld-run/*`. The host repo is not required to mark `.cld-run/` as tracked or non-gitignored.

Wire between host and peer:
- `AGENT_REVISION_HINT` -- the resolved commit hash of `A` (or an unresolved revset string when a `cld master` container delegates to a peer that has RW view of the target repo). The peer entrypoint runs `jj log -r "$AGENT_REVISION_HINT" -T commit_id` to pin it, then `jj workspace add -r <that hash>`.
- `AGENT_SCRATCH` -- base64 envelope of `{path-under-.cld-run/ : bytes}`, produced by `encode_scratch_envelope`.
- `AGENT_ANCHOR_HASH` is **not** sent by the host; it is computed peer-side after the workspace exists and exported by the entrypoint for downstream consumers (agent-loop, descendant guard, `summary.json`).

Shared helpers: `cld/vcs/anchor.py` (`resolve_anchor`, `assert_descendant`) and `cld/vcs/scratch.py` (`stage_in_workspace`, `stage_from_env`, envelope encode/decode). Anchor persistence across `cld master restart` / `cld agent restart` is provided by the jj bookmark named `<session>` in the origin's store: the container entrypoint detects it and reattaches by pointing a fresh workspace at the bookmark's last tip. On reattach, `AGENT_ANCHOR_HASH` is recovered by walking the bookmark's ancestors for the commit whose description is `"cld anchor: <session>"`. The peer-staging code path is jj-only; the git backend has no equivalent.

## Agent Output

Agent containers are `--rm` (auto-removed on exit). Results are committed to the agent's branch as `agent-output-<session-name>/`: `agent.log`, `result.json`, `summary.json`. Callers read these via `VcsBackend.file_show()`.

Inspect with jj: `jj log -r <name>`, `jj diff -r <name>`. Merge: `jj squash --from <name>`.
Inspect with git: `git log <name>`, `git diff <name>~1..<name>`. Merge: `git merge <name>`.

## Notes

- All commands require a **VCS repository** (jj or git). They walk up from cwd to find `.jj/` or `.git/`.
- Containers run as host UID/GID with security hardening (cap-drop ALL, no-new-privileges, resource limits).
- The agent entrypoint merges global MCP server config from `~/.claude.json` into project scope.
- Install with `poetry install` to get the `cld` command.
- Logging is centralised in `cld/log.py`; each module obtains a logger via `get_logger(__name__)`.

## MCP Orchestrator (deprecated)

`cld/mcp/orchestrator.py` and `scripts/mcp/run-orchestrator.sh` remain in the tree for reference but are not wired into any image, host claude config, or persona. Do not add new callers; use the `messenger` MCP for inter-agent coordination instead. Tests for the orchestrator module are skipped (`tests/test_orchestrator.py`, `tests/test_log.py::test_mcp_orchestrator_stdout_is_clean`).

## Messenger (inter-container agent messaging)

Full design: `docs/design-agent-messaging.md`. One-line summary: one repo agent per repo, `send()`/`list_inbox()`/`read_message()`/`archive()` via the `messenger` MCP server, replies come back on your next turn, the agent remembers everything across messages (one persistent `claude -p --resume` session).

- `cld/messenger/mailbox.py` -- pure filesystem transport (atomic `tmp/` write + `rename()` into `inbox/`; `archive/`; append-only `outbox.log`, whose lines carry the full subject+body so one mailbox is a complete transcript). No MCP/Docker coupling: `list_containers()` delegates to `cld/host_docker.py` (local docker on host, broker inside master) and `resolve_recipient()` short-circuits to filesystem delivery when the recipient names an existing mailbox dir (the reply path -- so agents message back with no host channel). Unit-testable with `tmp_path`. Also owns the task-agent registry surface (`meta.json` spawn facts via `ensure_meta`/`list_fleet`, `state.json` reads, `transcript()`, `_archive/<name>/` on teardown, and the `_edges/` hop counters) -- see `docs/design-task-agents.md`. Root entries starting with `_` are reserved, not mailboxes.
- `cld/messenger/agent_loop.py` -- the supervisor daemon (`python -m cld.messenger.agent_loop`, execed as PID 1 by the entrypoint's `AGENT_MODE` branch). State machine: `KICKOFF` (once, via `prompts/personas/<agent_kickoff_persona>.md`, default `agent.md`) -> `IDLE` (poll `inbox/` every 1 s) -> `PROCESSING` (one message, strict FIFO via oldest mtime) -> `IDLE`, until `SIGTERM`. Writes `state.json` into its own mailbox dir after every transition. The kickoff prompt goes to claude on **stdin**, not in argv -- a persona's leading `---` frontmatter was otherwise parsed as an unknown option; frontmatter is stripped either way (`cld.prompts.strip_frontmatter`). Under `TASK_AGENT_MODE` the same state machine runs in **task mode** (`TaskMode.from_env()`): kickoff is `compose_kickoff()`'s three layers -- lifecycle preamble (`prompts/personas/task-agent.md`), the master-chosen role persona (mounted at `/config/persona.md`), then the task verbatim -- and `meta.json` is written once at boot. No extra phase; see `docs/design-task-agents.md` §5, §11.
- `cld/mcp/messenger.py` -- FastMCP server wrapping `mailbox.py`. `send`/`list_inbox`/`read_message`/`archive` operate on the calling container's own mailbox, identified by `SESSION_NAME`. Two additive **fleet** tools are master surfaces instead, scoped to mailboxes whose `meta.json` records the caller as parent: `fleet_digest()` (one cheap row per task-agent -- task, phase, msg_count, cost, unread, last_activity; no bodies) and `read_mailbox(name, since="")` (the full exchange, `inbox/` + `archive/` received and `outbox.log` sent, `since` exclusive, archived mailboxes included). The digest is what makes the master's per-turn crank affordable; sweeping inboxes would find nothing, since an agent archives each message within ~1 s.
- **Hop gate (agent-to-agent only).** A message between two task-agents counts against that edge's absolute budget in `_edges/<sorted>.json`; past the limit the send is refused and **nothing more is ever delivered over that edge** -- not a retry, not a supervisor-synthesized reply, no cap notice (docs/design-task-agents.md D29: exempting the messages that announce the end is what loops). The blocked side escalates to its master, whose channel is a *different* edge and never budgeted. The gate lives in `mailbox.gated_send` and the closure rule one level down in `write_message`, because **two** instructed paths reach the transport -- the MCP `send()` tool and `python -m cld.messenger.send`, which the baked-in `messenger-send` skill documents. `edge_spent` is asked *before* a delivery and `bump_edge` counts *after* it, so the limit-th message is the last one that lands rather than the first one refused.
- **Reply guarantee (recipient-scoped):** the supervisor snapshots its own `outbox.log` line count before invoking Claude, then checks whether a line addressed to *this message's sender* appeared afterward (`mailbox.replied_since`, not the tool call itself) -- if not, it synthesizes a fallback reply so every incoming message gets exactly one reply. Scoping to the sender matters once an agent has peers: a send to a peer, or an escalation to the master, must not discharge the reply owed to whoever it is answering. The guarantee is **bounded by the edge budget**: a fallback is a real delivery on the edge, so it consumes a hop while the edge is open and is refused once it is spent (which is what keeps the guarantee from becoming a loop engine -- see the hop gate above).
- **Mailbox mount:** `/var/cld/mailboxes` in-container (`cld.docker.MAILBOX_MOUNT`), bind-mounted from `cfg.mailbox_root` on the host (`~/.cld/mailboxes` by default) whenever `build_container_args(..., master=True)` or `(..., agent=True)`. `cfg.mailbox_root` is a **host-side** path only -- in-container code always uses the fixed `MAILBOX_MOUNT` constant, never `Config.mailbox_root`.
- **Empirical note on `claude -p --output-format json`:** verified against a live Claude Code 2.1.198 build (see top of `agent_loop.py`) -- the cost field is `total_cost_usd`, not `cost_usd` as the existing stub fixtures under `tests/fixtures/stub-*` assume; `--max-turns` works despite being undocumented in `claude --help` output.
- **Naming to keep straight:** the persistent repo agent lives under `cld agent` (uses the `claude-devcontainer` image with `AGENT_MODE=1`); the one-shot autonomous agent lives under `cld run` (uses the `claude-run` image, `--rm`). They share no code path except `build_container_args`/`session_workspace_path`.
- **Deviation from the design doc, verified empirically:** readiness sentinels live at `/tmp/cld-{master,agent}-ready`, not `/run/...` as originally specced -- `/run` is root-owned `755` in the base image, so a non-root container (`--user <uid>:<gid>`) can never `touch` a file there (pre-existing bug shared with `--master`, fixed for both here).
- **Nested mailbox root is the user's problem, by design.** When `cld` itself runs nested inside another container (its own `CLD_HOST_PROJECT_DIR`/`CLD_HOST_HOME` set), `build_container_args` cannot create the mailbox root on the real host. (No docker socket is mounted into any container anymore, so there is no cross-socket shortcut to reach the host filesystem even if one were wanted -- container launches from inside master go through the host broker, which runs host-native `cld` where this nested branch does not apply.) `build_container_args` only `mkdir`s when it can verify its own filesystem view *is* the host view (`to_host_path` is a no-op); otherwise it logs a warning with the exact `mkdir -p && chown` command to run on the real host and proceeds to mount the (possibly not-yet-existing) path as-is. `container-init.sh`'s `ensure_own_mailbox` fails loudly in-container (and aborts `AGENT_MODE` startup) if that path turns out to be missing or wrong-owned, rather than silently degrading.

## Chain Orchestrator

Module: `cld/chain.py`. Declarative multi-step pipeline runner. Entry point is `cld chain <file.yaml>` via `cli.py`.

**Dataclasses** (all frozen):

| Class | Role |
|---|---|
| `ChainStep` | Single agent step: name, persona, model, prompt, output, inputs, timeout |
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

**Persona injection:** Each step declares a `persona:` name. `persona_resolve()` searches `<repo_root>/prompts/personas/` then `<cld_root>/prompts/personas/`. The resolved path is passed to `launch_agent(system_prompt_file=...)`, which mounts it as the agent's system prompt override.

**Branch model:**
- A persistent `chain_<name>` branch is created at the start and acts as the accumulator.
- Each step's agent runs off a transient `chain_<name>_<step>` branch (parallel: `chain_<name>_<idx>_<step>`).
- On step success, `advance_chain_branch()` moves the accumulator to the step's tip and deletes the transient branch.
- On failure the accumulator stays at its last good position; transient branches are left for inspection.

**Parallel concurrency:** `_run_parallel()` serializes `launch_agent()` calls (avoids Docker race conditions), then waits on all siblings sequentially. The effective parallelism is Docker-side: all containers run concurrently even though Python's wait loop is sequential. `CLD_CHAIN_MAX_PARALLEL` caps the number of siblings per group.

**Chain YAML files** live in `chains/`. Persona files live in `prompts/personas/`. Step outputs are written to `.cld/chain-outputs/<chain-name>/` inside the agent's workspace.
