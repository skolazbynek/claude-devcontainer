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
  loop.py                        -- automated implement-review loop
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
prompts/                         -- Reusable task prompts for agents
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

# Autonomous agent
cld agent [-n name] [-m model] [-r revision] [-p prompt] [task-file.md]

# Code review agent
cld review [-n name] [-m model] <feature-branch> <trunk-branch>

```

## Configuration

All Python-side runtime tunables live in `cld/config.py:Config` (frozen dataclass). Each Typer command and MCP tool constructs `Config.from_env()` once at entry and passes it explicitly down the call chain (Variant A: explicit DI, no global).

**Resolution order (lowest → highest priority):** dataclass defaults < user TOML (`~/.config/cld/config.toml`) < project TOML (`<repo_root>/.cld.config`, walked up from cwd) < `.env` in cwd < `CLD_*` env vars.

TOML uses flat snake_case keys mirroring `Config` field names (`base_image`, `devcontainer_image`, `agent_image`, `mysql_config`, `ssl_certs_path`, `agent_timeout`, `poll_interval`, `debug`, `home_mounts_always`, `home_mounts_devcontainer`, `trunk_candidates`). Array fields accept TOML arrays of strings. Unknown keys are warned about on stderr and ignored. `host_project_dir` / `host_home` are container-internal and not configurable via TOML.

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
| `CLD_DEBUG` | `false` | Diagnostics flag |

Container-side env vars consumed by shell entrypoints (NOT read by Python `Config`; left unprefixed because shell scripts read them by name):

| Var | Where set | Purpose |
|---|---|---|
| `SESSION_NAME` | `build_container_args` -> container | Branch/workspace name |
| `INSTRUCTION_FILE` | agent launch -> container | Task file path |
| `AGENT_MODEL` / `AGENT_REVISION` | launcher -> container | Claude model / revision for workspace init |
| `AGENT_COMMIT_MSG_LLM` / `AGENT_SYSTEM_PROMPT_FILE` | user -> container | Optional agent overrides |
| `MYSQL_DEFAULTS_FILE` | `build_container_args` -> container | Credentials path inside container |
| `WORKSPACE_ORIGIN` | `container-init.sh` -> Python | `/workspace/origin` (read by `vcs/detect.py`) |

## Agent Output

Agent containers are `--rm` (auto-removed on exit). Results are committed to the agent's branch as `agent-output-<session-name>/`: `agent.log`, `result.json`, `summary.json`. The orchestrator reads these via `VcsBackend.file_show()`.

Inspect with jj: `jj log -r <name>`, `jj diff -r <name>`. Merge: `jj squash --from <name>`.
Inspect with git: `git log <name>`, `git diff <name>~1..<name>`. Merge: `git merge <name>`.

## Notes

- All commands require a **VCS repository** (jj or git). They walk up from cwd to find `.jj/` or `.git/`.
- Containers run as host UID/GID with security hardening (cap-drop ALL, no-new-privileges, resource limits).
- The agent entrypoint merges global MCP server config from `~/.claude.json` into project scope.
- Install with `poetry install` to get the `cld` command.

## MCP Orchestrator

Python MCP server in `cld/mcp/orchestrator.py`. See README's "MCP Orchestrator" section for the user-facing description and tool list. CLAUDE.md focuses on developer-internal context only.

Internal notes:
- `launch_agent` calls `cld.agent.launch_agent()` directly (not via subprocess) so it shares image-management, env, and path-translation logic.
- Non-host-visible task files are staged into `repo_root/.agent-tasks/` so they can be bind-mounted.
- The orchestrator never squashes or merges into external branches; result aggregation is the caller's job.
