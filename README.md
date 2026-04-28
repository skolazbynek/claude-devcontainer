# cld

Run Claude Code in Docker containers with VCS workspace isolation. Supports **jujutsu (jj)** natively and **falls back to git** when jj is not installed. Each container gets its own isolated workspace (jj workspace or git worktree) and branch, so multiple agents can work on the same repo concurrently without conflicts.

## Prerequisites

- **Linux host** (required: `/etc/ssl/certs` is mandatory; macOS/Windows Docker Desktop layouts may not satisfy this)
- Docker
- A **jujutsu** or **git** repository (jj preferred; git used as fallback)
- Python 3.11+ with [Poetry](https://python-poetry.org/)
- Optional: `MYSQL_CONFIG` env pointing to a `.cnf` file (vendor-specific; safe to ignore)

## Setup

```bash
# Install the CLI
poetry install

# Build images (one command builds both, devcontainer first)
cld build [--no-cache]
```

All commands must be run from within a VCS repository (jj or git).

## Usage

```bash
# Interactive devcontainer (neovim, jj/git, poetry, claude with --dangerously-skip-permissions)
cld devcontainer [-n name]

# Autonomous agent (task file, inline prompt, or both)
cld agent [-n name] [-m model] [-r revision] task.md
cld agent -p "Fix the auth bug in src/login.py"
cld agent task.md -p "Focus on the database layer"

# Code review agent (generates diff, runs review from template)
cld review [-n name] [-m model] <feature-branch> <trunk-branch>

# Implement-review loop (automated iterate until clean review)
cld loop [-n name] [-m model] [--max-iterations 3] task.md
```

### Agent workflow

With jujutsu:

```bash
# Launch an agent
cld agent -n fix-auth task.md

# Check progress (wait a few seconds first for the file to appear)
until [ -f "$(jj root)/agent-output-agent_fix-auth/agent.log" ]; do sleep 1; done
tail -f $(jj root)/agent-output-agent_fix-auth/agent.log

# After completion, inspect and merge
jj log -r agent_fix-auth
jj diff -r agent_fix-auth
jj squash --from agent_fix-auth
```

With git:

```bash
# Launch an agent
cld agent -n fix-auth task.md

# Check progress
tail -f $(git rev-parse --show-toplevel)/agent-output-agent_fix-auth/agent.log

# After completion, inspect and merge
git log agent_fix-auth
git diff agent_fix-auth~1..agent_fix-auth
git merge agent_fix-auth
```

Agent containers run detached and auto-remove on exit. Results are committed to the agent's branch as `agent-output-<session>/` containing `agent.log`, `result.json`, and `summary.json`.

## VCS Backend

The tool auto-detects the VCS backend:

1. If `.jj/` exists and `jj` is installed -- **jujutsu backend** (preferred)
2. If `.git/` exists and `git` is installed -- **git backend** (fallback)

This detection runs both on the host (CLI commands) and inside containers (entrypoints). The abstraction layer lives in `cld/vcs/` (Python) and `imgs/claude-devcontainer/vcs-lib.sh` (shell).

| Concept | jujutsu | git |
|---|---|---|
| Repository marker | `.jj/` | `.git/` |
| Workspace isolation | `jj workspace add` | `git worktree add` |
| Named ref | bookmark | branch |
| Current change | `@` | `HEAD` |
| Commit | `jj commit` (auto-tracks) | `git add -A && git commit` |
| Read file from revision | `jj file show -r <rev> <path>` | `git show <rev>:<path>` |
| Common ancestor | `fork_point(A \| B)` | `git merge-base A B` |

## MCP Orchestrator

The orchestrator gives Claude the ability to launch and manage Docker agents via MCP tools. It's baked into the devcontainer and also usable on the host:

```bash
# Register for host use (user-scoped, works from any directory)
claude mcp add -s user orchestrator -- /path/to/cld/scripts/mcp/run-orchestrator.sh

# Inside devcontainer, use the team orchestrator persona
claude --agent team-orchestrator
```

**Tools:**
- Agent lifecycle: `launch_agent`, `list_agents`, `check_status`, `stop_agent`, `get_log`
- Prompt management: `list_prompts`, `read_prompt`, `save_prompt`
- VCS operations: `vcs_log`, `vcs_branch_list`, `vcs_new`, `vcs_commit`, `vcs_describe`, `vcs_diff`
- Backward-compatible aliases: `jj_log`, `jj_bookmark_list`, `jj_new`, `jj_commit`, `jj_describe`, `jj_diff`

Builtin prompts are baked into the image at `/opt/cld/prompts/`. Workspace prompts live at `<repo-root>/prompts/`.

### End-to-end orchestrator flow

```
+-- host -----------------+
|  cld devcontainer       |
|        |                |
|        v                |
|  +------------------+   |
|  |  devcontainer    |   |
|  |  claude --agent  |   |
|  |  team-orchestr.  |---+ launches sibling agent containers via /var/run/docker.sock
|  +------------------+   |        |
|                         |        v
|                         |  +------------+  +------------+
|                         |  | agent_a... |  | review_b...| -> commits to its own VCS branch
|                         |  +------------+  +------------+
+-------------------------+        |                |
                                   v                v
                              jj squash --from agent_a   (host user merges results)
```

1. User starts a devcontainer with `cld devcontainer`.
2. Inside it, runs `claude --agent team-orchestrator`.
3. The orchestrator calls `launch_agent` to spawn sibling Docker agents.
4. Each agent commits results to its own VCS branch; the host user merges them with `jj squash --from <branch>` (or `git merge`).

## Architecture

```
cld/                               Python package (CLI + shared logic)
  cli.py                           typer app with all subcommands
  docker.py                        container arg building, image management, path translation
  agent.py                         agent and review launch logic
  loop.py                          automated implement-review loop
  vcs/                             VCS abstraction layer
    base.py                        abstract VcsBackend interface
    jj.py                          jujutsu backend (preferred)
    git.py                         git backend (fallback)
    detect.py                      auto-detection logic
  mcp/orchestrator.py              MCP server for agent orchestration

scripts/
  mcp/run-orchestrator.sh          venv wrapper for MCP server

imgs/
  claude-base/                     Common base image (debian, git, jj, docker cli, poetry, claude). No editor, no entrypoint.
    Dockerfile.claude-base
  claude-devcontainer/             Devcontainer image (FROM base, adds neovim + classic vim)
    container-init.sh              Shared init (MCP config merge, mysql wrapper) -- baked into base
    vcs-lib.sh                     Shell VCS abstraction (sourced by both entrypoints) -- baked into base
    entrypoint-claude-devcontainer.sh
  claude-agent/                    Agent image (FROM base, adds agent entrypoint + system prompt)
    entrypoint-claude-agent.sh
  claude-agent-review/             Review templates

prompts/                           Reusable task prompts for agents
```

**Image hierarchy:** `claude-base` is the parent of both `claude-devcontainer` and `claude-agent` (siblings). Always build base first; `cld build` handles all three in order.

### Workspace isolation

Containers mount the host repo at `/workspace/origin` and create an isolated workspace at `/workspace/current` with a named branch. For jj this uses `jj workspace add`; for git it uses `git worktree add`. The `-r` flag controls which revision the workspace branches from (default: `@` for jj, `HEAD` for git). On exit, the workspace is cleaned up but the branch persists.

### Host file protection

Host `~/.claude.json` is mounted read-only. The entrypoint builds a container-local copy with MCP servers merged for the container's project path.

The devcontainer mounts host nvim config/data (`~/.config/nvim`, `~/.local/share/nvim`, `~/.local/state/nvim`, `~/.cache/nvim`) read-only under `/tmp/nvim-host/` and copies them into `$HOME` on startup. Changes made inside the container do not persist to the host. The agent image has no editor and does not mount these.

### Docker socket

The devcontainer mounts `/var/run/docker.sock` so the orchestrator can launch and manage sibling agent containers. Path translation converts container paths to host paths for volume mounts.

### Security model and known gaps

Containers run as host UID/GID with `--cap-drop=ALL`, `--security-opt=no-new-privileges`, and resource limits (2 CPU, 4GB RAM).

**Known gaps -- read carefully before shipping anything sensitive into a container:**

- **No outbound network firewall.** Once an agent is running, it can reach any host on the public internet and exfiltrate anything mounted in (`~/.claude` tokens, `~/.claude.json` MCP creds, `~/.config/*` creds, `MYSQL_CONFIG`). Anthropic's reference devcontainer ships an `init-firewall.sh` with default-deny outbound and a small allowlist; cld does not (yet) ship an equivalent.
- **`/var/run/docker.sock` mount = host root.** When the docker socket is mounted (it is, for the orchestrator), an agent inside can run `docker run -v /:/host --privileged ...` and read or modify anything on the host. This effectively bypasses every other security control. If you don't need the orchestrator, comment out the docker.sock block in `cld/docker.py`.
- **`~/.claude` is mounted rw.** A malicious agent can both read your OAuth tokens and overwrite session state.

Treat the container as **trusted with your full host environment** until the firewall and a docker-socket proxy land. Use `--dangerously-skip-permissions` accordingly.

## Environment Variables

| Variable | Purpose |
|---|---|
| `SESSION_NAME` | Branch, workspace, and container name |
| `INSTRUCTION_FILE` | Task file path inside agent container |
| `AGENT_REVISION` | Revision for workspace init (default: `@` / `HEAD`) |
| `AGENT_MODEL` | Claude model override (default: `sonnet`) |
| `HOST_PROJECT_DIR` | Host repo root path (for nested docker path translation) |
| `HOST_HOME` | Host home directory (for path translation) |
| `MYSQL_CONFIG` | Host path to `.cnf` file (mounted into container if set) |

## Development

```bash
poetry install

# Unit tests (no docker, no network)
poetry run pytest -m "not integration and not docker and not e2e"

# Integration tests
poetry run pytest -m integration

# Tests that need Docker
poetry run pytest -m docker

# End-to-end tests (slow, real containers)
poetry run pytest -m e2e
```

Test markers are declared in `pyproject.toml`. The `tests/conftest.py` detects when running inside the devcontainer via `HOST_PROJECT_DIR` to translate paths.
