# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

Tooling for running Claude Code in Docker containers with VCS workspace isolation. Supports **jujutsu (jj)** natively and falls back to **git** when jj is not installed.

- **Devcontainer** (`cld devcontainer`) -- Interactive session with neovim, jj/git, poetry. Drops into bash with `--dangerously-skip-permissions`.
- **Agent** (`cld agent`) -- Headless autonomous agent. Takes a task file and/or inline prompt, runs detached, commits results to a VCS branch.
- **Agent Review** (`cld review`) -- Generates a diff between branches and runs a code review via the agent pipeline.

## Architecture

```
cld/                             -- Python package (host-side CLI + shared logic)
  cli.py                         -- typer app, all subcommands
  docker.py                      -- container setup: arg building, image management, path translation
  agent.py                       -- agent/review launch logic
  agent_runtime.py               -- shared agent lifecycle helpers (wait, cost, formatting)
  loop.py                        -- automated implement-review loop
  chain.py                       -- chain orchestrator (mirrors loop.py for multi-step pipelines)
  vcs/                           -- VCS abstraction layer
    base.py                      -- abstract VcsBackend interface
    jj.py                        -- jujutsu backend implementation
    git.py                       -- git backend implementation (fallback)
    detect.py                    -- auto-detection: jj preferred, git fallback
  mcp/
    orchestrator.py              -- MCP server for orchestrating Docker agents
scripts/
  mcp/run-orchestrator.sh        -- Thin venv wrapper for MCP server
imgs/
  claude-base/                   -- Common base image (debian, git, jj, poetry, docker CLI, mysql client, claude). No editor, no entrypoint.
  claude-devcontainer/           -- Devcontainer image (FROM base, adds neovim + vim + entrypoint)
    container-init.sh            -- Shared container init (sourced by both entrypoints, baked into base)
    vcs-lib.sh                   -- Shell-level VCS abstraction (sourced by both entrypoints, baked into base)
  claude-agent/                  -- Agent image (FROM base, adds agent entrypoint + system prompt)
  claude-agent-review/           -- Review templates (review-template.md, fix-mr.md)
prompts/
  personas/                      -- Persona system prompts (architect, implementer, reviewer, …)
  (other task prompts)           -- Reusable task prompts for agents
chains/                          -- YAML chain definitions (e.g. architect-implement-review.yaml)
```

**Image hierarchy:** `claude-base` is the parent of both `claude-devcontainer` and `claude-agent` (siblings). Build base first.

**Shared logic lives in three places:**
- Host side: `cld/docker.py` -- imported by all commands. Provides `build_container_args`, `find_repo_root`, `ensure_image`, `build_session_name`, logging.
- Host side: `cld/vcs/` -- VCS abstraction layer. `get_backend()` returns a `JjBackend` or `GitBackend` depending on what's available.
- Container side: `imgs/claude-devcontainer/container-init.sh` + `vcs-lib.sh` -- sourced by both entrypoints. Sets up mysql wrapper and VCS-agnostic workspace functions.

**VCS detection order:**
1. If `.jj/` directory exists AND `jj` binary is available -> jujutsu backend
2. If `.git/` directory exists AND `git` binary is available -> git backend
3. Error

**Workspace isolation:** Containers mount the host repo at `/workspace/origin`, create a workspace (jj workspace / git worktree) at `/workspace/current` with a named branch. On exit, workspace is cleaned up but branch persists.

**Session naming:** All commands accept `-n/--name`. Names are prefixed per mode: `cld_`, `agent_`, `review_`. Passed into containers as `SESSION_NAME` env var. Entrypoints use it for branches, workspaces, and log directories.

## Key Commands

```bash
# Build images (base first; cld build does this automatically)
docker build -f imgs/claude-base/Dockerfile.claude-base -t claude-base:latest .
docker build -f imgs/claude-devcontainer/Dockerfile.claude-devcontainer -t claude-devcontainer:latest .
docker build -f imgs/claude-agent/Dockerfile.claude-agent -t claude-agent:latest imgs/claude-agent

# Interactive devcontainer
cld devcontainer [-n name]

# Master devcontainer lifecycle (persistent per-repo container)
cld devcontainer --master           # start-or-attach
cld devcontainer restart            # tear down + relaunch, preserving workspace; picks up cld image/code changes
cld devcontainer shutdown [--all]   # stop + remove + drop workspace

# Autonomous agent
cld agent [-n name] [-m model] [-r revision] [-p prompt] [task-file.md|@<name>]

# Code review agent
cld review [-n name] [-m model] <feature-branch> <trunk-branch>

```

## Configuration

All Python-side runtime tunables live in `cld/config.py:Config` (frozen dataclass). Each Typer command and MCP tool constructs `Config.from_env()` once at entry and passes it explicitly down the call chain (Variant A: explicit DI, no global).

**Resolution order (lowest → highest priority):** dataclass defaults < user TOML (`~/.config/cld/config.toml`) < project TOML (`<repo_root>/.cld.config`, walked up from cwd) < `.env` in cwd < `CLD_*` env vars.

TOML uses flat snake_case keys mirroring `Config` field names (`base_image`, `devcontainer_image`, `agent_image`, `mysql_config`, `ssl_certs_path`, `agent_timeout`, `poll_interval`, `debug`, `home_mounts_always`, `home_mounts_devcontainer`, `trunk_candidates`, `chain_max_parallel`, `chain_default_model`, `log_level`, `log_color`, `ignore_gitignore`). Array fields accept TOML arrays of strings. Unknown keys are warned about on stderr and ignored. `host_project_dir` / `host_home` are container-internal and not configurable via TOML.

`CLD_*` env vars (read by `Config.from_env`):

| Var | Default | Purpose |
|---|---|---|
| `CLD_BASE_IMAGE` | `claude-base:latest` | Common base Docker image |
| `CLD_DEVCONTAINER_IMAGE` | `claude-devcontainer:latest` | Devcontainer image |
| `CLD_AGENT_IMAGE` | `claude-agent:latest` | Agent image |
| `CLD_MYSQL_CONFIG` | `""` | Path to a `.cnf` file, mounted ro at `/run/secrets/mysql.cnf` |
| `CLD_SSL_CERTS_PATH` | `""` | SSL CA bundle path (dir or PEM file); empty = auto-detect |
| `CLD_HOST_PROJECT_DIR` | `""` | Set by host launcher into containers; lets in-container Python translate `/workspace/*` paths back to host paths for sibling `-v` mounts |
| `CLD_HOST_HOME` | `""` | Same idea for `$HOME` paths |
| `CLD_AGENT_TIMEOUT` | `1800` | Loop's per-agent wait timeout (seconds) |
| `CLD_POLL_INTERVAL` | `30` | Loop's docker-ps poll interval (seconds) |
| `CLD_CHAIN_MAX_PARALLEL` | `4` | Max parallel siblings launched concurrently in a chain group |
| `CLD_CHAIN_DEFAULT_MODEL` | `""` | Model override for chain agents; empty = agent default |
| `CLD_LOG_LEVEL` | `INFO` | Root level for the `cld` logger hierarchy. Accepts DEBUG/INFO/WARNING/ERROR (case-insensitive; WARN aliased to WARNING). |
| `CLD_LOG_COLOR` | `auto` | ANSI color in log output: `auto` (TTY-detect), `always`, or `never`. |
| `CLD_DEBUG` | `false` | Diagnostics flag. Back-compat alias: when truthy and `CLD_LOG_LEVEL` is unset, equivalent to `CLD_LOG_LEVEL=DEBUG`. |
| `CLD_IGNORE_GITIGNORE` | `""` | Colon-separated list of gitignored files to symlink from origin into workspace (e.g. `.env:.envrc`). Set in `.cld.config` as array: `ignore_gitignore = [".env"]`. |
| `CLD_SSH_AUTH_SOCK` | unset | SSH agent forwarding into `cld devcontainer`. Tri-state: **unset** = auto-detect from host `$SSH_AUTH_SOCK`; **empty** (`""`) = explicitly disable; **path** = use that socket. Forwarded to `/run/host-ssh-agent.sock` inside the container; devcontainer only (never headless `cld agent` / `cld review`). |

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

When running `cld devcontainer` or agents, the workspace is isolated in a jj/git working copy at `/workspace/current`, separate from the host repo mount at `/workspace/origin`. Gitignored files like `.env` don't automatically appear in the isolated workspace.

To symlink gitignored files (like `.env` or `.envrc`) from origin into the workspace, set `ignore_gitignore` in `.cld.config`:

```toml
ignore_gitignore = [".env", ".envrc"]
```

On container startup, `link_workspace_files()` symlinks each file from `/workspace/origin` into `/workspace/current`. If a file doesn't exist in origin, a warning is logged and linking continues. Symlinks are transparent to applications and reflect updates to the origin file.

## Anchor change contract

Every subcommand (`agent`, `devcontainer`, `review`, `loop`, `chain run`) shares one notion of an **anchor change**: an immutable revision from which all command-created changes descend. Default is the current change (`@` / `HEAD`); override with `-r`/`--revision`. The host pins the anchor to a commit hash, creates a single empty **editable root** child as a per-session workspace under `<repo>/.cld/workspaces/<session>/`, and starts the container with `AGENT_ANCHOR_HASH` set. The in-container `vcs_assert_descendant` guard (in `vcs-lib.sh`) refuses to commit or squash if `@` no longer descends from the anchor.

Host-side scratch files (composed task inputs, diff patches, persona stagings) live inside the per-session workspace at `.cld-run/<file>` — never in the caller's main working copy. `.cld-run/` is a reserved directory name inside agent workspaces; it is not gitignored, so each scratch file is structurally rooted in the anchor tree.

Three helpers in `cld/vcs/anchor.py` form the entire shared contract: `resolve_anchor`, `create_editable_root`, `assert_descendant`. `create_editable_root` also persists the anchor hash to `<repo>/.cld/anchors/<session>` so a workspace can be re-attached (e.g. by `cld devcontainer restart`) without recomputing the anchor from a possibly-moved `@`; `read_workspace_anchor(repo_root, session)` returns the recorded hash (or `None`).

## Agent Output

Agent containers are `--rm` (auto-removed on exit). Results are committed to the agent's branch as `agent-output-<session-name>/`: `agent.log`, `result.json`, `summary.json`. The orchestrator reads these via `VcsBackend.file_show()`.

Inspect with jj: `jj log -r <name>`, `jj diff -r <name>`. Merge: `jj squash --from <name>`.
Inspect with git: `git log <name>`, `git diff <name>~1..<name>`. Merge: `git merge <name>`.

## Notes

- All commands require a **VCS repository** (jj or git). They walk up from cwd to find `.jj/` or `.git/`.
- Containers run as host UID/GID with security hardening (cap-drop ALL, no-new-privileges, resource limits).
- The agent entrypoint merges global MCP server config from `~/.claude.json` into project scope.
- Install with `poetry install` to get the `cld` command.
- Logging is centralised in `cld/log.py`; each module obtains a logger via `get_logger(__name__)`.

## MCP Orchestrator

Python MCP server in `cld/mcp/orchestrator.py`. See README's "MCP Orchestrator" section for the user-facing description and tool list. CLAUDE.md focuses on developer-internal context only.

Internal notes:
- `launch_agent` calls `cld.agent.launch_agent()` directly (not via subprocess) so it shares image-management, env, and path-translation logic.
- Non-host-visible task files are staged into `repo_root/.agent-tasks/` so they can be bind-mounted.
- The orchestrator never squashes or merges into external branches; result aggregation is the caller's job.

## Chain Orchestrator

Module: `cld/chain.py`. Mirrors `cld/loop.py` but for declarative multi-step pipelines. Entry point is `cld chain <file.yaml>` via `cli.py`.

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

**Shared helpers** (`cld/agent_runtime.py`, extracted from `loop.py`):
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
