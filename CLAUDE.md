# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

Tooling for running Claude Code in Docker containers with VCS workspace isolation. Supports **jujutsu (jj)** natively and falls back to **git** when jj is not installed.

- **Devcontainer** (`cld devcontainer`) -- Interactive session with neovim, jj/git, poetry. Drops into bash with `--dangerously-skip-permissions`.
- **Agent** (`cld agent`) -- Headless autonomous agent. Takes a task file and/or inline prompt, runs detached, commits results to a VCS branch.
- **Agent Review** (`cld review`) -- Generates a diff between branches and runs a code review via the agent pipeline.
- **Headless** (`cld headless`) -- Thin wrapper: `claude -p --permission-mode acceptEdits`.

## Architecture

```
cld/                             -- Python package (host-side CLI + shared logic)
  cli.py                         -- typer app, all subcommands
  docker.py                      -- container setup: arg building, image management, path translation
  agent.py                       -- agent/review/headless launch logic
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
  claude-devcontainer/           -- Base image (debian, git, jj, neovim, poetry, mysql client, claude)
    container-init.sh            -- Shared container init (sourced by both entrypoints)
    vcs-lib.sh                   -- Shell-level VCS abstraction (sourced by both entrypoints)
  claude-agent/                  -- Agent image (FROM devcontainer, adds jq + agent entrypoint)
  claude-agent-review/           -- Review templates (review-template.md, fix-mr.md)
prompts/                         -- Reusable task prompts for agents
```

**Image hierarchy:** `claude-agent` builds FROM `claude-devcontainer`. Build devcontainer first.

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
# Build images (devcontainer first)
docker build -f imgs/claude-devcontainer/Dockerfile.claude-devcontainer -t claude-devcontainer:latest .
docker build -f imgs/claude-agent/Dockerfile.claude-agent -t claude-agent:latest imgs/claude-agent

# Interactive devcontainer
cld devcontainer [-n name]

# Autonomous agent
cld agent [-n name] [-m model] [-r revision] [-p prompt] [task-file.md]

# Code review agent
cld review [-n name] [-m model] <feature-branch> <trunk-branch>

# Headless
cld headless [args...]
```

## Env Vars

| Variable | Where set | Purpose |
|---|---|---|
| `SESSION_NAME` | `build_container_args` -> container | Branch/workspace name |
| `INSTRUCTION_FILE` | agent launch -> container | Task file path |
| `AGENT_REVISION` | agent launch -> container | Revision for workspace init (default: @ for jj, HEAD for git) |
| `MYSQL_CONFIG` | Host env / `.env` file | Path to `.cnf` file, mounted ro at `/run/secrets/mysql.cnf` |
| `MYSQL_DEFAULTS_FILE` | `build_container_args` -> container | Credentials path inside container |

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

Python MCP server (`cld/mcp/orchestrator.py`). Baked into the devcontainer at `/opt/cld/`. Also usable on host via `claude mcp add -s user orchestrator -- /path/to/cld/scripts/mcp/run-orchestrator.sh`.

**Tools provided:**
- `launch_agent` -- launch autonomous agent (task file, inline prompt, or builtin prompt -- non-host-visible files are auto-staged). Calls `cld.agent.launch_agent()` directly.
- `list_agents`, `check_status`, `stop_agent` -- container lifecycle (`check_status` checks docker while running, VCS branch + summary after completion; `include_result=True` for full claude output)
- `get_log` -- tail agent log from VCS branch
- `list_prompts`, `read_prompt`, `save_prompt` -- manage task prompts (builtin at `/opt/cld/prompts/` read-only + workspace at `<repo-root>/prompts/` read-write; saves always go to workspace)
- `vcs_log`, `vcs_branch_list`, `vcs_new`, `vcs_commit`, `vcs_describe`, `vcs_diff` -- VCS operations (backend-agnostic)
- `jj_log`, `jj_bookmark_list`, `jj_new`, `jj_commit`, `jj_describe`, `jj_diff` -- backward-compatible aliases (delegate to vcs_* tools)

**Note:** No automatic squash/merge into external branches. The orchestrator works within its own VCS changes only.
