# cld

Run Claude Code in Docker containers with jujutsu (jj) workspace isolation. Each container gets its own jj workspace and bookmark, so multiple agents can work on the same repo concurrently without conflicts.

## Prerequisites

- Docker
- [jujutsu (jj)](https://github.com/jj-vcs/jj) repository (not git)
- Python 3.11+ with [Poetry](https://python-poetry.org/)

## Setup

```bash
# Install the CLI
poetry install

# Build images (devcontainer first -- agent inherits from it)
docker build -f imgs/claude-devcontainer/Dockerfile.claude-devcontainer -t claude-devcontainer:latest .
docker build -f imgs/claude-agent/Dockerfile.claude-agent -t claude-agent:latest imgs/claude-agent
```

All commands must be run from within a jj repository.

## Usage

```bash
# Interactive devcontainer (neovim, jj, poetry, claude with --dangerously-skip-permissions)
cld devcontainer [-n name]

# Autonomous agent (task file, inline prompt, or both)
cld agent [-n name] [-m model] [-r revision] task.md
cld agent -p "Fix the auth bug in src/login.py"
cld agent task.md -p "Focus on the database layer"

# Code review agent (generates diff, runs review from template)
cld review [-n name] [-m model] <feature-branch> <trunk-branch>

# Headless mode (passthrough to claude -p --permission-mode acceptEdits)
cld headless [args...]
```

### Agent workflow

```bash
# Launch an agent
cld agent -n fix-auth task.md

# Check progress
tail -f $(jj root)/agent-output-agent_fix-auth/agent.log

# After completion, inspect and merge
jj log -r agent_fix-auth
jj diff -r agent_fix-auth
jj squash --from agent_fix-auth
```

Agent containers run detached and auto-remove on exit. Results are committed to the agent's jj bookmark as `agent-output-<session>/` containing `agent.log`, `result.json`, and `summary.json`.

## MCP Orchestrator

The orchestrator gives Claude the ability to launch and manage Docker agents via MCP tools. It's baked into the devcontainer and also usable on the host:

```bash
# Register for host use (user-scoped, works from any directory)
claude mcp add -s user orchestrator -- /path/to/cld/scripts/mcp/run-orchestrator.sh

# Inside devcontainer, use the team orchestrator persona
claude --agent team-orchestrator
```

**Tools:** `launch_agent`, `list_agents`, `check_status`, `stop_agent`, `get_log`, `list_prompts`, `read_prompt`, `save_prompt`, `jj_log`, `jj_bookmark_list`, `jj_new`, `jj_commit`, `jj_describe`, `jj_diff`.

Builtin prompts are baked into the image at `/opt/cld/prompts/`. Workspace prompts live at `<jj-root>/prompts/`.

## Architecture

```
cld/                               Python package (CLI + shared logic)
  cli.py                           typer app with all subcommands
  docker.py                        container arg building, image management, path translation
  agent.py                         agent, review, and headless launch logic
  mcp/orchestrator.py              MCP server for agent orchestration

scripts/
  mcp/run-orchestrator.sh          venv wrapper for MCP server

imgs/
  claude-devcontainer/             Base image (debian, git, jj, neovim, docker cli, poetry, claude)
    container-init.sh              Shared init (MCP config merge, staged file copy, mysql wrapper)
    entrypoint-claude-devcontainer.sh
  claude-agent/                    Agent image (FROM devcontainer, adds jq + agent entrypoint)
    entrypoint-claude-agent.sh
  claude-agent-review/             Review templates

prompts/                           Reusable task prompts for agents
```

**Image hierarchy:** `claude-agent` builds FROM `claude-devcontainer`. Always build devcontainer first.

### Workspace isolation

Containers mount the host jj repo at `/workspace/origin` and create a workspace at `/workspace/current` with a named bookmark. The `-r` flag controls which revision the workspace branches from (default: `@`). On exit, the workspace is forgotten but the bookmark persists.

### Host file protection

Host `~/.claude.json` is mounted read-only. The entrypoint builds a container-local copy with MCP servers merged for the container's project path. Files needing container-local writes (nvim cache/state) are staged: mounted read-only under `/tmp/host-files/` and copied into `$HOME` at startup.

### Docker socket

The devcontainer mounts `/var/run/docker.sock` so the orchestrator can launch and manage sibling agent containers. Path translation converts container paths to host paths for volume mounts.

### Security

Containers run as host UID/GID with `--cap-drop=ALL`, `--security-opt=no-new-privileges`, resource limits (2 CPU, 4GB RAM). The docker socket mount is the exception.

## Environment Variables

| Variable | Purpose |
|---|---|
| `SESSION_NAME` | jj bookmark, workspace, and container name |
| `INSTRUCTION_FILE` | Task file path inside agent container |
| `AGENT_REVISION` | jj revset for workspace init (default: `@`) |
| `AGENT_MODEL` | Claude model override (default: `sonnet`) |
| `HOST_PROJECT_DIR` | Host jj root path (for nested docker path translation) |
| `HOST_HOME` | Host home directory (for path translation) |
| `MYSQL_CONFIG` | Host path to `.cnf` file (mounted into container if set) |
