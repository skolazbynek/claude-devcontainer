# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

Tooling for running Claude Code in Docker containers with VCS workspace isolation. Supports **jujutsu (jj)** natively and falls back to **git** when jj is not installed.

- **Ephemeral devcontainer** (`cld` bare) -- Interactive session with neovim, jj/git, poetry. Drops into bash with `--dangerously-skip-permissions`. Container is `--rm`.
- **Persistent master** (`cld master`) -- Persistent per-repo interactive devcontainer. Start-or-attach; idempotent per repo. Same interactive shell as the ephemeral one, but stays up so packages/history/state persist across attaches.
- **Persistent repo agent** (`cld agent`) -- Persistent per-repo headless Claude agent. Runs the `claude-devcontainer` image with `AGENT_MODE=1`; the entrypoint execs the supervisor daemon (`python -m cld.messenger.agent_loop`). Receives tasks via the mailbox/messenger transport (see "Messenger" below and `docs/design-agent-messaging.md`). One long-lived Claude session per repo.
- **One-shot run** (`cld run`) -- Headless *one-shot* autonomous agent. Takes a task file and/or inline prompt, runs detached in the `claude-run` image, commits results to a VCS branch, then its container exits (`--rm`).

## Architecture

```
cld/                             -- Python package (host-side CLI + shared logic)
  cli.py                         -- typer app, all subcommands
  docker.py                      -- container setup: arg building, image management, path translation
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
  (other task prompts)           -- Reusable task prompts for agents
chains/                          -- YAML chain definitions (e.g. architect-implement-review.yaml)
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

**Workspace isolation:** Containers mount the host repo RW at `/workspace/origin`. The container's own entrypoint (via `vcs_init_editable_root` in `vcs-lib.sh`) runs `jj workspace add` / `git worktree add` at `/workspace/origin/.cld/workspaces/<session>` on boot and symlinks `/workspace/current` to it. Workspace creation lives inside the container (not on the host) so `cld master` can launch sibling agents against RO-mounted repos without ever needing RW itself. Graceful shutdown of persistent containers does `docker exec /opt/cld/cleanup-workspace.sh` inside the container before `docker stop` -- this deregisters the workspace and removes it; the branch persists.

**Master extra mounts (`master_extra_mounts_ro`):** Host paths RO same-path bind-mounted into `cld master` containers. Lets the user `cd` into any RO-mounted repo inside master's shell and run `cld agent` -- master's cwd-walk finds the repo, resolves the anchor via RO reads, and calls `docker run` on a sibling agent container with `-v <host_path>:/workspace/origin` (RW inside the sibling only). Master's own filesystem view of the target repo stays RO throughout.

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

# One-shot autonomous run
cld run [-n name] [-m model] [-r revision] [-p prompt] [task-file.md|@<name>]

```

## Configuration

All Python-side runtime tunables live in `cld/config.py:Config` (frozen dataclass). Each Typer command and MCP tool constructs `Config.from_env()` once at entry and passes it explicitly down the call chain (Variant A: explicit DI, no global).

**Resolution order (lowest → highest priority):** dataclass defaults < user TOML (`~/.config/cld/config.toml`) < project TOML (`<repo_root>/.cld.config`, walked up from cwd) < `.env` in cwd < `CLD_*` env vars.

TOML uses flat snake_case keys mirroring `Config` field names (`base_image`, `devcontainer_image`, `run_image`, `mysql_config`, `ssl_certs_path`, `agent_timeout`, `poll_interval`, `debug`, `home_mounts_always`, `home_mounts_devcontainer`, `master_extra_mounts_ro`, `chain_max_parallel`, `chain_default_model`, `log_level`, `log_color`, `ignore_gitignore`, `mailbox_root`, `agent_max_turns`, `agent_kickoff_persona`). Array fields accept TOML arrays of strings. Unknown keys are warned about on stderr and ignored. `host_project_dir` / `host_home` are container-internal and not configurable via TOML.

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
| `CLD_IGNORE_GITIGNORE` | `""` | Colon-separated list of gitignored files to symlink from origin into workspace (e.g. `.env:.envrc`). Set in `.cld.config` as array: `ignore_gitignore = [".env"]`. |
| `CLD_SSH_AUTH_SOCK` | unset | SSH agent forwarding into `cld` (bare, ephemeral). Tri-state: **unset** = auto-detect from host `$SSH_AUTH_SOCK`; **empty** (`""`) = explicitly disable; **path** = use that socket. Forwarded to `/run/host-ssh-agent.sock` inside the container; devcontainer only (never headless `cld agent`). |
| `CLD_MAILBOX_ROOT` | `~/.cld/mailboxes` | Host root of the inter-container mailbox tree; bind-mounted RW into every master and agent container. |
| `CLD_AGENT_MAX_TURNS` | `30` | Per-message turn cap passed to the agent supervisor's `claude -p --max-turns`. |
| `CLD_AGENT_KICKOFF_PERSONA` | `agent` | Persona name (resolved like chain personas) used to kick off a new repo-agent Claude session. |

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
| `WORKSPACE_ORIGIN` | `container-init.sh` -> Python | `/workspace/origin` (read by `vcs/detect.py`) |

**Logging.** `cld/log.py` configures a stderr handler on the `cld.*` logger hierarchy. All modules use `log = get_logger(__name__)`. Logs go to stderr only — stdout is reserved for user-facing deliverables (final reports, list rows, prompts). Levels: DEBUG (verbose, every subprocess + VCS call), INFO (default; major lifecycle events), WARNING (recoverable issues), ERROR (failed operations).

## Workspace files (gitignored files in containers)

When running `cld` (bare, ephemeral) or agents, the workspace is isolated in a jj/git working copy at `/workspace/current`, separate from the host repo mount at `/workspace/origin`. Gitignored files like `.env` don't automatically appear in the isolated workspace.

To symlink gitignored files (like `.env` or `.envrc`) from origin into the workspace, set `ignore_gitignore` in `.cld.config`:

```toml
ignore_gitignore = [".env", ".envrc"]
```

On container startup, `link_workspace_files()` symlinks each file from `/workspace/origin` into `/workspace/current`. If a file doesn't exist in origin, a warning is logged and linking continues. Symlinks are transparent to applications and reflect updates to the origin file.

## Anchor change contract

Every subcommand (`agent`, `devcontainer`, `review`, `loop`, `chain run`) shares one notion of an **anchor change**: an immutable revision from which all command-created changes descend. Default is the current change (`@` / `HEAD`); override with `-r`/`--revision`. The host pins the anchor to a commit hash, creates a single empty **editable root** child as a per-session workspace under `<repo>/.cld/workspaces/<session>/`, and starts the container with `AGENT_ANCHOR_HASH` set. The in-container `vcs_assert_descendant` guard (in `vcs-lib.sh`) refuses to commit or squash if `@` no longer descends from the anchor.

Host-side scratch files (composed task inputs, diff patches, persona stagings) live inside the per-session workspace at `.cld-run/<file>` — never in the caller's main working copy. `.cld-run/` is a reserved directory name inside agent workspaces; it is not gitignored, so each scratch file is structurally rooted in the anchor tree.

Three helpers in `cld/vcs/anchor.py` form the entire shared contract: `resolve_anchor`, `create_editable_root`, `assert_descendant`. `create_editable_root` also persists the anchor hash to `<repo>/.cld/anchors/<session>` so a workspace can be re-attached (e.g. by `cld master restart` / `cld agent restart`) without recomputing the anchor from a possibly-moved `@`; `read_workspace_anchor(repo_root, session)` returns the recorded hash (or `None`).

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

- `cld/messenger/mailbox.py` -- pure filesystem transport (atomic `tmp/` write + `rename()` into `inbox/`; `archive/`; append-only `outbox.log`). No MCP/Docker-daemon coupling beyond `list_containers()`'s `docker ps` calls, so it's unit-testable with `tmp_path`.
- `cld/messenger/agent_loop.py` -- the repo agent's supervisor daemon (`python -m cld.messenger.agent_loop`, execed as PID 1 by the entrypoint's `AGENT_MODE` branch). State machine: `KICKOFF` (once, via `prompts/personas/repo-agent.md`) -> `IDLE` (poll `inbox/` every 1 s) -> `PROCESSING` (one message, strict FIFO via oldest mtime) -> `IDLE`, until `SIGTERM`. Writes `state.json` into its own mailbox dir after every transition.
- `cld/mcp/messenger.py` -- FastMCP server wrapping `mailbox.py`; every tool operates on the calling container's own mailbox, identified by `SESSION_NAME`.
- **Reply guarantee:** the supervisor snapshots its own `outbox.log` line count before invoking Claude and checks it grew afterward (not the tool call itself) -- if not, it synthesizes a fallback reply so every incoming message gets exactly one reply, even if Claude's turn forgets to call `send()`.
- **Mailbox mount:** `/var/cld/mailboxes` in-container (`cld.docker.MAILBOX_MOUNT`), bind-mounted from `cfg.mailbox_root` on the host (`~/.cld/mailboxes` by default) whenever `build_container_args(..., master=True)` or `(..., agent=True)`. `cfg.mailbox_root` is a **host-side** path only -- in-container code always uses the fixed `MAILBOX_MOUNT` constant, never `Config.mailbox_root`.
- **Empirical note on `claude -p --output-format json`:** verified against a live Claude Code 2.1.198 build (see top of `agent_loop.py`) -- the cost field is `total_cost_usd`, not `cost_usd` as the existing stub fixtures under `tests/fixtures/stub-*` assume; `--max-turns` works despite being undocumented in `claude --help` output.
- **Naming to keep straight:** the persistent repo agent lives under `cld agent` (uses the `claude-devcontainer` image with `AGENT_MODE=1`); the one-shot autonomous agent lives under `cld run` (uses the `claude-run` image, `--rm`). They share no code path except `build_container_args`/`session_workspace_path`.
- **Deviation from the design doc, verified empirically:** readiness sentinels live at `/tmp/cld-{master,agent}-ready`, not `/run/...` as originally specced -- `/run` is root-owned `755` in the base image, so a non-root container (`--user <uid>:<gid>`) can never `touch` a file there (pre-existing bug shared with `--master`, fixed for both here).
- **Nested mailbox root is the user's problem, by design.** When `cld` itself runs nested inside another container (its own `CLD_HOST_PROJECT_DIR`/`CLD_HOST_HOME` set), `build_container_args` cannot create the mailbox root on the real host -- doing so via a throwaway `docker run` over the shared docker socket was considered and **rejected**: reaching across that socket to touch the host filesystem breaks the container-isolation guarantee even though the socket is shared for launching sibling containers. Instead, `build_container_args` only `mkdir`s when it can verify its own filesystem view *is* the host view (`to_host_path` is a no-op); otherwise it logs a warning with the exact `mkdir -p && chown` command to run on the real host and proceeds to mount the (possibly not-yet-existing) path as-is. `container-init.sh`'s `ensure_own_mailbox` fails loudly in-container (and aborts `AGENT_MODE` startup) if that path turns out to be missing or wrong-owned, rather than silently degrading.

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
