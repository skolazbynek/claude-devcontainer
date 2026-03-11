# cld

Tooling for running Claude Code in Docker containers with jujutsu (jj) workspace isolation.

Each container gets its own jj workspace and bookmark, so multiple sessions and agents can work on the same repo concurrently without conflicts. Host files are mounted read-only where possible; a staging mechanism copies state that needs container-local writes.

## Modes

| Mode | Script | Purpose |
|---|---|---|
| **Devcontainer** | `run-claude-devcontainer.sh` | Interactive session with neovim, jj, poetry. Drops into bash with `--dangerously-skip-permissions`. |
| **Agent** | `run-claude-agent.sh` | Headless autonomous agent. Takes a task file, runs detached, commits results to a jj bookmark. |
| **Review** | `run-claude-agent-review.sh` | Generates a jj diff between branches and runs a code review via the agent pipeline. |
| **Headless** | `run-claude-headless.sh` | Thin wrapper: `claude -p --permission-mode acceptEdits`. |
| **Orchestrator** | `claude --agent team-orchestrator` | Team orchestrator persona (run inside devcontainer). Plans, delegates, and monitors parallel agents via MCP. |

## Quick Start

```bash
# Build images (devcontainer first, agent inherits from it)
docker build -f imgs/claude-devcontainer/Dockerfile.claude-devcontainer -t claude-devcontainer:latest .
docker build -f imgs/claude-agent/Dockerfile.claude-agent -t claude-agent:latest imgs/claude-agent

# Interactive session
scripts/run-claude-devcontainer.sh [-n name]

# Inside devcontainer: start orchestrator
claude --agent team-orchestrator

# Autonomous agent
scripts/run-claude-agent.sh [-n name] <task-file.md>

# Code review
scripts/run-claude-agent-review.sh [-n name] <feature-branch> <trunk-branch>
```

All scripts must be run from within a jj repository. They walk up from cwd to find `.jj/`.

## Structure

```
scripts/
  lib/docker-common.sh           Shared library (arg builders, path translation, logging)
  mcp/orchestrator.py            MCP server for agent orchestration (poetry managed)
  run-claude-devcontainer.sh     Interactive launcher
  run-claude-agent.sh            Agent launcher
  run-claude-agent-review.sh     Review agent (generates diff, delegates to agent launcher)
  run-claude-headless.sh         Headless wrapper

imgs/
  claude-devcontainer/           Base image (debian, git, jj, neovim, docker cli, poetry, claude)
    container-init.sh            Shared init (MCP config, staged file copy, mysql wrapper)
    entrypoint-claude-devcontainer.sh
  claude-agent/                  Agent image (FROM devcontainer, adds jq + agent entrypoint)
    entrypoint-claude-agent.sh
  claude-agent-review/           Review templates
    review-template.md
    fix-mr.md

prompts/                         Reusable task prompts for agents
```

**Image hierarchy:** `claude-agent` builds FROM `claude-devcontainer`. Always build devcontainer first.

## Features

### Workspace Isolation
Containers mount the host jj repo at `/workspace/origin`, create a jj workspace at `/workspace/current` with a named bookmark. On exit, the workspace is forgotten but the bookmark persists. Multiple containers can work on the same repo concurrently.

### Host File Protection
Host `~/.claude.json` is mounted read-only at `/tmp/host-claude.json`. The entrypoint builds a container-local copy with MCP servers merged for the container's project path. Host config is never modified.

Files that need container-local writes (nvim cache/state/data) are staged: mounted read-only under `/tmp/host-files/` and copied into `$HOME` at startup. Anything under `/tmp/host-files/<path>` is automatically copied to `~/<path>`.

### Mount Summary

**Read-only:** SSL certs, `~/.claude.json` (staged), `~/.config` (OAuth tokens), `.gitconfig`, `.config/nvim`, `.bashrc`, mysql credentials, nvim state (staged).

**Read-write:** jj repo (`/workspace/origin` -- agent output, workspace operations), `~/.claude` (session transcripts, agent memory, settings), docker socket (orchestrator).

### MCP Config Merge
On startup, the entrypoint reads the host's `~/.claude.json`, extracts global MCP servers and host-project MCP servers, and merges them into the container-local config under `projects["/workspace/current"]`. This makes all host-registered MCP servers available inside the container without modifying the host file.

### Docker Socket (Orchestrator Support)
The devcontainer mounts `/var/run/docker.sock` and adds the host docker group GID, enabling the orchestrator MCP server to launch and manage sibling agent containers. Path translation (`to_host_path`) converts container paths to host paths for volume mounts, since the docker daemon resolves paths on the host.

### Session Naming
All scripts accept `-n|--name`. Names are prefixed per mode: `cld_`, `agent_`, `review_`. Passed as `SESSION_NAME` env var. Used for jj bookmarks, workspaces, container names, and output directories.

### Agent Output
Results are written to `agent-output-<session-name>/` in the workspace: `agent.log`, `result.json`, `summary.json`. Inspect with `jj log -r <name>` and `jj diff -r <name>`. Merge with `jj squash --from <name>`.

### MCP Orchestrator
Python MCP server (`scripts/mcp/orchestrator.py`) that gives Claude the ability to manage Docker agents.

**Register:** `claude mcp add orchestrator -- poetry run python scripts/mcp/orchestrator.py`

**Tools:** `launch_agent`, `list_agents`, `check_status`, `stop_agent` (lifecycle), `get_results`, `get_log`, `get_diff` (inspection), `list_prompts`, `read_prompt`, `save_prompt` (prompt management), `jj_log`, `jj_bookmark_list`, `jj_new`, `jj_commit`, `jj_describe`, `jj_diff` (jujutsu operations).

The orchestrator does not auto-merge into external branches. Integration is always manual.

### Security
Containers run as host UID/GID with `--cap-drop=ALL`, `--security-opt=no-new-privileges`, resource limits (2 CPU, 4GB RAM). The docker socket mount is the exception -- it grants sibling container management.

## Environment Variables

| Variable | Set by | Purpose |
|---|---|---|
| `SESSION_NAME` | Launcher -> container | jj bookmark/workspace/container name |
| `INSTRUCTION_FILE` | Agent launcher -> container | Task file path inside container |
| `HOST_PROJECT_DIR` | `build_docker_socket_args` -> container | Host path of jj root (for path translation) |
| `HOST_HOME` | `build_docker_socket_args` -> container | Host home directory (for path translation) |
| `MYSQL_CONFIG` | Host env / `.env` | Path to `.cnf` file on host |
| `MYSQL_DEFAULTS_FILE` | `build_mysql_args` -> container | Credentials path inside container |

## Notes

- All scripts require a **jj repository** (not git).
- The devcontainer runs `poetry install` on startup if `pyproject.toml` exists in the workspace. This installs MCP orchestrator dependencies.
- The `claude` binary inside the devcontainer is wrapped to always pass `--dangerously-skip-permissions`.
- Agent containers run `claude -p` (headless) with a system prompt that instructs retry on failure.
- Review agents generate diffs in git format via `jj diff --git` and use `envsubst` to populate the review template.
