# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

Tooling for running Claude Code in Docker containers with jujutsu (jj) workspace isolation.

- **Devcontainer** (`cld devcontainer`) -- Interactive session with neovim, jj, poetry. Drops into bash with `--dangerously-skip-permissions`.
- **Agent** (`cld agent`) -- Headless autonomous agent. Takes a task file and/or inline prompt, runs detached, commits results to a jj bookmark.
- **Agent Review** (`cld review`) -- Generates a jj diff between branches and runs a code review via the agent pipeline.
- **Headless** (`cld headless`) -- Thin wrapper: `claude -p --permission-mode acceptEdits`.

## Architecture

```
cld/                             -- Python package (host-side CLI + shared logic)
  cli.py                         -- typer app, all subcommands
  docker.py                      -- container setup: arg building, image management, path translation
  agent.py                       -- agent/review/headless launch logic
  mcp/
    orchestrator.py              -- MCP server for orchestrating Docker agents
scripts/
  mcp/run-orchestrator.sh        -- Thin venv wrapper for MCP server
imgs/
  claude-devcontainer/           -- Base image (debian, git, jj, neovim, poetry, mysql client, claude)
    container-init.sh            -- Shared container init (sourced by both entrypoints)
  claude-agent/                  -- Agent image (FROM devcontainer, adds jq + agent entrypoint)
  claude-agent-review/           -- Review templates (review-template.md, fix-mr.md)
prompts/                         -- Reusable task prompts for agents
```

**Image hierarchy:** `claude-agent` builds FROM `claude-devcontainer`. Build devcontainer first.

**Shared logic lives in two places:**
- Host side: `cld/docker.py` -- imported by all commands. Provides `build_container_args`, `find_jj_root`, `ensure_image`, `build_session_name`, logging.
- Container side: `imgs/claude-devcontainer/container-init.sh` -- sourced by both entrypoints. Sets up mysql wrapper if credentials are mounted.

**Workspace isolation:** Containers mount the host jj repo at `/workspace/origin`, create a jj workspace at `/workspace/current` with a named bookmark. On exit, workspace is forgotten but bookmark persists.

**Session naming:** All commands accept `-n/--name`. Names are prefixed per mode: `cld_`, `agent_`, `review_`. Passed into containers as `SESSION_NAME` env var. Entrypoints use it for jj bookmarks, workspaces, and log directories.

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
| `SESSION_NAME` | `build_container_args` -> container | jj bookmark/workspace name |
| `INSTRUCTION_FILE` | agent launch -> container | Task file path |
| `AGENT_REVISION` | agent launch -> container | jj revset for workspace init (default: @) |
| `MYSQL_CONFIG` | Host env / `.env` file | Path to `.cnf` file, mounted ro at `/run/secrets/mysql.cnf` |
| `MYSQL_DEFAULTS_FILE` | `build_container_args` -> container | Credentials path inside container |

## Agent Output

Agent containers are `--rm` (auto-removed on exit). Results are committed to the agent's jj bookmark as `agent-output-<session-name>/`: `agent.log`, `result.json`, `summary.json`. The orchestrator reads these via `jj file show -r <bookmark>`.

Inspect: `jj log -r <name>`, `jj diff -r <name>`. Merge: `jj squash --from <name>`.

## Notes

- All commands require a **jj repository** (not git). They walk up from cwd to find `.jj/`.
- Containers run as host UID/GID with security hardening (cap-drop ALL, no-new-privileges, resource limits).
- The agent entrypoint merges global MCP server config from `~/.claude.json` into project scope.
- Install with `poetry install` to get the `cld` command.

## MCP Orchestrator

Python MCP server (`cld/mcp/orchestrator.py`). Baked into the devcontainer at `/opt/cld/`. Also usable on host via `claude mcp add -s user orchestrator -- /path/to/cld/scripts/mcp/run-orchestrator.sh`.

**Tools provided:**
- `launch_agent` -- launch autonomous agent (task file, inline prompt, or builtin prompt -- non-host-visible files are auto-staged). Calls `cld.agent.launch_agent()` directly.
- `list_agents`, `check_status`, `stop_agent` -- container lifecycle (`check_status` checks docker while running, jj bookmark + summary after completion; `include_result=True` for full claude output)
- `get_log` -- tail agent log from jj bookmark
- `list_prompts`, `read_prompt`, `save_prompt` -- manage task prompts (builtin at `/opt/cld/prompts/` read-only + workspace at `<jj-root>/prompts/` read-write; saves always go to workspace)
- `jj_log`, `jj_bookmark_list`, `jj_new`, `jj_commit`, `jj_describe`, `jj_diff` -- jujutsu operations (use `jj_diff` with agent bookmark to review changes)

**Note:** No automatic squash/merge into external branches. The orchestrator works within its own jj changes only.
