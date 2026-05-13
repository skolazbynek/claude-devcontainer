# cld

Run Claude Code in Docker containers with VCS workspace isolation. Supports **jujutsu (jj)** natively and **falls back to git** when jj is not installed. Each container gets its own isolated workspace (jj workspace or git worktree) and branch, so multiple agents can work on the same repo concurrently without conflicts.

## Prerequisites

- Docker
- A **jujutsu** or **git** repository (jj preferred; git used as fallback)
- Python 3.11+ with [Poetry](https://python-poetry.org/)
- Probably not Windows

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
# Show information
cld --help

# Interactive devcontainer (neovim, jj/git, poetry, claude with --dangerously-skip-permissions)
cld devcontainer [-n name]

# Autonomous agent (task file, inline prompt, or both)
cld agent [-n name] [-m model] [-r revision] task.md
cld agent -p "Fix the auth bug in src/login.py"
cld agent task.md -p "Focus on the database layer"

# Code review agent (generates diff, runs review from template)
cld review [-n name] [-m model] <feature-branch> <trunk-branch>

# Implement-review loop (automated iterate until clean review)
cld loop task.md -p "Optional additional information to the task file"
```

### Agent workflow

Agent containers run detached and auto-remove on exit. Results are committed to the agent's branch as `agent-output-<session>/` containing `agent.log`, `result.json`, and `summary.json`.

### Loop workflow

`cld loop` runs implement → review iterations on a single branch until the review is clean (no Critical and no Major findings) or `--max-iterations` is reached. Each iteration spawns two agent containers in sequence (implementer, then reviewer). Review findings are fed into the next implementer's prompt.

```bash
# Run with a task file (combine with -p, same semantics as `cld agent`)
cld loop -n add-cache task.md
cld loop -n add-cache -p "Use the redis client already in src/cache.py" task.md

# Inline-only
cld loop -n add-cache -p "Add a cache layer to the user repository"

# Pick a reviewer model independent of the implementer
cld loop -n add-cache -m opus --review-model sonnet task.md

# Human-in-the-loop: pause after each review (continue/stop/view/edit findings)
# Untested
cld loop --approve task.md
```

Exit conditions:

- **Clean review** (`critical == 0` and `major == 0`) -- loop stops early, exit reason `clean review`.
- **`--max-iterations` reached** (default 3) -- loop stops with the last iteration's state.
- **Implementer or reviewer failure** -- loop stops; the failing iteration's branch state is preserved for inspection.
- **`--approve` stop** -- user terminates after a paused review.

The loop creates a single branch `loop_<name>` accumulating all iterations. Commit messages on the loop branch are tagged `[loop impl N]` and `[loop review N]` and include severity counts. A total cost in USD is reported at the end. Per-iteration review files (`CODE_REVIEW_iter<N>.md`) are committed to the branch.

Loop env vars (see *Configuration* below): `CLD_AGENT_TIMEOUT` caps per-agent wait time; `CLD_POLL_INTERVAL` controls docker-ps polling.

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

All RO `$HOME` mounts (claude/anthropic/jj configs, `~/.claude.json`, plus devcontainer-only `~/.gitconfig`, `~/.bashrc`, and the nvim dirs `~/.config/nvim` / `~/.local/state/nvim` / `~/.cache/nvim`) are staged read-only under `/tmp/host-config/<rel>` and copied into `$HOME` on startup by `copy_host_configs`. Changes made inside the container do not persist to the host. The agent image has no editor and skips the devcontainer-only entries.

### Docker socket

The devcontainer mounts `/var/run/docker.sock` so the orchestrator can launch and manage sibling agent containers. Path translation converts container paths to host paths for volume mounts.

### Security model and known gaps

Containers run as host UID/GID with `--cap-drop=ALL`, `--security-opt=no-new-privileges`, and resource limits (2 CPU, 4GB RAM).

**Known gaps -- read carefully before shipping anything sensitive into a container:**

- **No outbound network firewall.** Once an agent is running, it can reach any host on the public internet and exfiltrate anything mounted in (`~/.claude` tokens, `~/.claude.json` MCP creds, `~/.config/*` creds, `CLD_MYSQL_CONFIG`). Anthropic's reference devcontainer ships an `init-firewall.sh` with default-deny outbound and a small allowlist; cld does not (yet) ship an equivalent.
- **`/var/run/docker.sock` mount = host root.** When the docker socket is mounted (it is, for the orchestrator), an agent inside can run `docker run -v /:/host --privileged ...` and read or modify anything on the host. This effectively bypasses every other security control. If you don't need the orchestrator, comment out the docker.sock block in `cld/docker.py`.
- **`~/.claude` is mounted rw.** A malicious agent can both read your OAuth tokens and overwrite session state.

## Configuration

All Python-side runtime tunables live in `cld/config.py:Config` (frozen dataclass). Each command/MCP tool builds a `Config.from_env()` once at entry and passes it explicitly to launch helpers (Variant A: explicit DI).

### Resolution order

Lowest → highest priority:

1. Dataclass defaults
2. User TOML — `~/.config/cld/config.toml`
3. Project TOML — `<repo_root>/.cld.config` (walked up from cwd)
4. `.env` in cwd
5. `CLD_*` env vars

### TOML schema

Flat snake_case keys mirroring `Config` field names. Unknown keys are warned about on stderr and ignored. `host_project_dir` / `host_home` are container-internal and not exposed.

```toml
base_image = "claude-base:latest"
devcontainer_image = "claude-devcontainer:latest"
agent_image = "claude-agent:latest"
mysql_config = "/path/to/mysql.cnf"
agent_timeout = 1800
poll_interval = 30
debug = false
```

### `CLD_*` env vars (defaults shown)

| Variable | Default | Purpose |
|---|---|---|
| `CLD_BASE_IMAGE` | `claude-base:latest` | Common base Docker image |
| `CLD_DEVCONTAINER_IMAGE` | `claude-devcontainer:latest` | Devcontainer image |
| `CLD_AGENT_IMAGE` | `claude-agent:latest` | Agent image |
| `CLD_MYSQL_CONFIG` | `""` | Host path to a `.cnf` file (mounted into container if set) |
| `CLD_HOST_PROJECT_DIR` | `""` | Host repo root path; set by host launcher into containers for nested docker path translation |
| `CLD_HOST_HOME` | `""` | Host home directory (for path translation) |
| `CLD_AGENT_TIMEOUT` | `1800` | Loop's per-agent wait timeout (seconds) |
| `CLD_POLL_INTERVAL` | `30` | Loop's docker-ps poll interval (seconds) |
| `CLD_DEBUG` | `false` | Diagnostics flag |

Container-side env vars consumed by shell entrypoints (kept unprefixed):

| Variable | Purpose |
|---|---|
| `SESSION_NAME` | Branch, workspace, and container name |
| `INSTRUCTION_FILE` | Task file path inside agent container |
| `AGENT_REVISION` | Revision for workspace init (default: `@` / `HEAD`) |
| `AGENT_MODEL` | Claude model override (default: `sonnet`) |
| `WORKSPACE_ORIGIN` | Path to bind-mounted host repo inside container (set by `container-init.sh`) |

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

Test markers are declared in `pyproject.toml`. The `tests/conftest.py` detects when running inside the devcontainer via `CLD_HOST_PROJECT_DIR` to translate paths.
