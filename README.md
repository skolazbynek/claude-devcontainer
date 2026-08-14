# cld

Run Claude Code in Docker containers with VCS workspace isolation. Supports **jujutsu (jj)** natively and **falls back to git** when jj is not installed. Each container gets its own isolated workspace (jj workspace or git worktree) and branch, so multiple agents can work on the same repo concurrently without conflicts.

## Prerequisites

- Docker
- A **jujutsu** or **git** repository (jj preferred; git used as fallback)
- Python 3.11+ with [Poetry](https://python-poetry.org/)
- Probably not Windows

## Setup

```bash
# Install with poetry
poetry install

# You can run from the poetry environment within CLD repo
poetry run cld --help

# To run from any directory, add `/.venv/bin/cld` to your PATH. I have `~/.local/bin/cld` symlink pointing there.

# Build images (one command builds both, devcontainer first)
cld build [--no-cache]
```

All commands must be run from within a VCS repository (jj or git).

## Usage

```bash
# Show information
cld --help

# Ephemeral interactive devcontainer (neovim, jj/git, poetry, claude with --dangerously-skip-permissions)
cld [-n name] [-m model] [-r revision] [-p prompt]   # -p only; prompt refs go to `cld run`

# Persistent per-repo interactive devcontainer (start-or-attach; idempotent per repo)
cld master                                # start or re-attach
cld master {restart | shutdown [--all] | status | logs}

# Persistent per-repo headless Claude agent (mailbox-driven)
cld agent                                 # start; never attaches
cld agent {restart | shutdown [--all] | status | logs}

# One-shot autonomous run (headless, --rm, commits to a branch)
cld run [refs...] [-n name] [-m model] [-r revision] [-p prompt]
cld run -p "Fix the auth bug in src/login.py"
cld run task.md -p "Focus on the database layer"

# Declarative multi-agent chain
cld chain run @review-implement task.md
cld chain run chains/parallel-review.yaml -p "Focus on auth code"
cld chain list
cld chain validate chains/my-chain.yaml
cld chain dry-run @review-implement
```

### Agent workflow

Agent containers run detached and auto-remove on exit. Results are committed to the agent's branch as `agent-output-<session>/` containing `agent.log`, `result.json`, and `summary.json`.

### Chain workflow

`cld chain` runs a declarative sequence of named agents defined in a YAML file. Each step is an autonomous agent that receives the prior step's output as context. Steps can run in parallel (a `parallel:` group); the synthesiser step that follows sees a combined summary. Built-in chains live in `chains/` in the repo and in the installed package; reference them with `@name` shorthand.

```bash
# Run a built-in chain against a task file
cld chain run @review-implement task.md

# Run with an inline prompt instead of a task file
cld chain run @review-implement -p "Fix the N+1 query in user_repo.py"

# Run a local chain file
cld chain run chains/parallel-review.yaml task.md

# Inspect without running
cld chain list
cld chain validate @parallel-review
cld chain dry-run @review-implement
```

**Built-in chains:**

`chains/review-implement.yaml` — reviewer flags issues, implementer fixes them:

```yaml
name: review-implement
description: Reviewer flags issues, implementer fixes them.

defaults:
  model: sonnet

steps:
  - name: review
    prompts: ["@personas/reviewer"]

  - name: implement
    prompts: ["@personas/implementer"]
```

`chains/parallel-review.yaml` — two reviewers in parallel; synthesiser ranks findings:

```yaml
name: parallel-review
description: Two reviewers in parallel; synthesiser picks the most actionable.

defaults:
  model: sonnet

steps:
  - parallel:
      - name: generic
        prompts: ["@personas/reviewer"]
      - name: security
        prompts: ["@personas/security-reviewer"]

  - name: synthesise
    prompts: ["@personas/reviewer"]
    prompt: |
      Two prior reviewers produced findings. Combine them, deduplicate,
      and rank by severity. Drop anything that contradicts the user's
      original task.
```

**YAML field reference:**

| Field | Level | Description |
|---|---|---|
| `name` | chain, step | Identifier; used as branch name suffix |
| `description` | chain | Human-readable summary shown by `cld chain list` |
| `defaults` | chain | Default values applied to every step (`model`, `timeout`) |
| `steps` | chain | Ordered list of step or `parallel` group items |
| `parallel` | step item | List of steps to run concurrently |
| `persona` | step | Claude persona / system-prompt name |
| `model` | step, defaults | Claude model override for this step |
| `timeout` | step, defaults | Per-agent timeout in seconds (0 = inherit `CLD_AGENT_TIMEOUT`) |
| `prompt` | step | Extra instructions appended to the step's system prompt |
| `output` | step | Explicit output file path committed by this step |
| `inputs` | step | List of prior step names whose output this step receives |

**Limitations (PoC scope):** no loops, no conditionals. For parallel groups, code changes committed by non-first siblings are not visible to the next sequential step — only text output is forwarded.

Chain env vars (see *Configuration* below): `CLD_CHAIN_MAX_PARALLEL` caps concurrent siblings; `CLD_CHAIN_DEFAULT_MODEL` overrides the model for all steps.

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

## MCP Orchestrator (deprecated)

The `orchestrator` MCP is no longer wired into cld images or host-side claude. `cld/mcp/orchestrator.py` and `scripts/mcp/run-orchestrator.sh` remain in the source tree for reference but are not registered anywhere. Use the `messenger` MCP (below) for inter-agent coordination.

## Messenger

Lets any devcontainer (master or repo agent) send a message to any other and get a reply on its next turn, backed by a shared mailbox directory on the host -- no threads, no polling required from the user. Full design and mental model: `docs/design-agent-messaging.md`.

```bash
# Register for host use (user-scoped, works from any directory)
claude mcp add -s user messenger -- /path/to/cld/scripts/mcp/run-messenger.sh

# Start a persistent, headless repo agent for the current repo
cld agent

# From any other container (master or another agent), message it by repo basename
# (inside Claude): mcp__messenger__send(to="my-repo", subject="...", body="...", expects_reply=True)
```

**Tools:** `send(to, subject, body)`, `list_inbox(unread_only)`, `read_message(id)`, `archive(id)`, `list_agents(kind)`.

**Lifecycle:**
```bash
cld agent                     # start (idempotent per repo)
cld agent restart             # rebuild + relaunch (fresh session)
cld agent shutdown [--all]    # stop + remove + cleanup
cld agent status              # supervisor phase / session / cost
cld agent logs [-n N]         # tail its log
```

The repo agent has one persistent Claude session that survives across messages -- it remembers prior conversations with a given sender, so follow-ups like "for question a, RESTRICT" resolve without re-stating context. A message sent with `expects_reply` gets exactly one reply; if the agent's turn doesn't call `send()`, the supervisor synthesizes a fallback so a question is never left hanging. Without that flag the agent stays quiet by design -- an unconditional reply makes each acknowledgment oblige another one.

## Architecture

```
cld/                               Python package (CLI + shared logic)
  cli.py                           host typer app (all docker-daemon verbs)
  cli_container.py                 container typer app, shipped as `cld` in the image
  task_agent.py                    task-agent helpers shared by both apps
  docker.py                        container arg building, image management, path translation
  run.py                           one-shot run launch logic (`cld run`)
  chain.py                         declarative multi-agent chain runner
  vcs/                             VCS abstraction layer
    base.py                        abstract VcsBackend interface
    jj.py                          jujutsu backend (preferred)
    git.py                         git backend (fallback)
    detect.py                      auto-detection logic
  mcp/orchestrator.py              MCP server for agent orchestration (deprecated, not wired)
  mcp/messenger.py                 MCP server for the mailbox transport
  messenger/mailbox.py             filesystem mailbox transport
  messenger/agent_loop.py          repo agent supervisor daemon

scripts/
  mcp/run-orchestrator.sh          venv wrapper (deprecated, kept for reference)
  mcp/run-messenger.sh             venv wrapper for the messenger MCP server

imgs/
  claude-base/                     Common base image (debian, git, jj, docker cli, poetry, claude). No editor, no entrypoint.
    Dockerfile.claude-base
  claude-devcontainer/             Devcontainer image (FROM base, adds neovim + classic vim)
    container-init.sh              Shared init (MCP config merge, mysql wrapper) -- baked into base
    vcs-lib.sh                     Shell VCS abstraction (sourced by both entrypoints) -- baked into base
    entrypoint-claude-devcontainer.sh
  claude-run/                      One-shot run image (FROM base, adds run entrypoint + system prompt)
    entrypoint-claude-run.sh

prompts/                           Reusable task prompts for agents
```

**Image hierarchy:** `claude-base` is the parent of both `claude-devcontainer` and `claude-run` (siblings). Always build base first; `cld build` handles all three in order.

### Managing sibling agents from `cld master`

To spin up / restart / shut down persistent agents for repos other than master's own, set `master_targets` in your config (list of host paths registered as launch targets for master; each becomes an empty placeholder directory inside master's shell so `cd <path>` works, without ever bind-mounting the repo into master):

```toml
master_targets = ["~/repos/foo", "~/work/bar"]
```

Then inside master's shell:

```bash
cd /home/you/repos/repoB    # RO mount, safe to browse
cld agent                   # launches a sibling agent container for repoB
cld agent status
cld agent shutdown
```

Master itself has no filesystem view of the target repo -- only a placeholder directory so `cd` works. `cld agent` in master resolves that placeholder to the target's host path and hands it to the host broker, which runs host-side `cld agent` for RepoB (validated against master's `org.cld.targets` label). The peer container it launches gets RW at `/workspace/origin`, does its own anchor staging on boot, and forgets its bookmark on SIGTERM so master never writes to RepoB. `cld repos` inside master's shell lists what it can target.

### Workspace isolation

Containers mount the host repo RW at `/workspace/origin`. The container's own entrypoint runs `jj workspace add` / `git worktree add` at `/workspace/origin/.cld/workspaces/<session>` on boot and symlinks `/workspace/current` to it. Workspace creation lives in the container (not on the host), so `cld master` can launch sibling agents against RO-mounted repos without needing RW itself. The `-r` flag pins the anchor revision (default: `@` for jj, `HEAD` for git). On graceful shutdown, the container itself deregisters the workspace via `docker exec /opt/cld/cleanup-workspace.sh` before `docker stop`; the branch persists.

### Host file protection

Host `~/.claude.json` is mounted read-only. The entrypoint builds a container-local copy with MCP servers merged for the container's project path.

All RO `$HOME` mounts (claude/anthropic/jj configs, `~/.claude.json`, plus devcontainer-only `~/.gitconfig`, `~/.bashrc`, and the nvim dirs `~/.config/nvim` / `~/.local/state/nvim` / `~/.cache/nvim`) are staged read-only under `/tmp/host-config/<rel>` and copied into `$HOME` on startup by `copy_host_configs`. Changes made inside the container do not persist to the host. The agent image has no editor and skips the devcontainer-only entries.

### No docker socket in containers

No container mounts `/var/run/docker.sock` (it was equivalent to host root; see the security notes below). The two things that needed in-container docker now go through the host broker over SSH:

- **Peer enumeration** (`list_agents`, `cld agent status`): master calls the broker's `list-containers` action, which runs `docker ps` on the host and streams structured records back. Agents don't enumerate at all -- message replies address the sender by the full name carried in the message, delivered by filesystem, so agents need no host channel.
- **Launching a sibling `cld agent` from inside master**: `cld agent` in master resolves the cwd's target repo and calls the broker's `agent` action, which runs host-side `cld agent` for that repo (validated against the master's host-set `org.cld.targets` label). Arg-building, anchor staging, and image builds all happen natively on the host.

See `cld/broker.py` (the host-vs-broker seam) and `broker/cld-broker.sh` (the actions). Path translation (`CLD_HOST_PROJECT_DIR`/`CLD_HOST_HOME`) still converts container paths to host paths for target resolution; it is now set unconditionally rather than riding along with the socket mount.

### Security model and known gaps

Containers run as host UID/GID with `--cap-drop=ALL`, `--security-opt=no-new-privileges`, and resource limits (2 CPU, 4GB RAM).

**Known gaps -- read carefully before shipping anything sensitive into a container:**

- **No outbound network firewall.** Once an agent is running, it can reach any host on the public internet and exfiltrate anything mounted in (`~/.claude` tokens, `~/.claude.json` MCP creds, `~/.config/*` creds, `CLD_MYSQL_CONFIG`). Anthropic's reference devcontainer ships an `init-firewall.sh` with default-deny outbound and a small allowlist; cld does not (yet) ship an equivalent.
- **No docker socket is mounted (was: host root).** Earlier versions mounted `/var/run/docker.sock` for peer enumeration, which let an agent run `docker run -v /:/host --privileged ...` and read or modify anything on the host -- bypassing every other control. The socket is gone; master's only host channel is now the broker key (a fixed, non-eval action allowlist with label-validated targets -- see the host broker section), and agents have no host channel at all. A leaked broker key grants that action set (run-tests + sibling `cld agent` lifecycle for allowlisted repos), which is strictly narrower than arbitrary host root but still privileged -- protect the key accordingly.
- **`~/.claude` is mounted rw.** A malicious agent can both read your OAuth tokens and overwrite session state.

## Configuration

Any command checks for `~/.config/cld/config.toml` and creates a default file if it doesn't exist. Adjust it as you need - especially what paths are mounted into the devcontainer. You can create a per-project overrides with `<repo_root>/.cld/config.toml`.

### Resolution order

Lowest → highest priority:

1. Dataclass defaults
2. User TOML — `~/.config/cld/config.toml`
3. Project TOML — `<repo_root>/.cld/config.toml` (walked up from cwd)
4. `.env` in cwd
5. `CLD_*` env vars

### TOML schema

Flat snake_case keys mirroring `Config` field names, valid in both `~/.config/cld/config.toml` (user-wide) and `<repo_root>/.cld/config.toml` (per-repo). Unknown keys are warned about on stderr and ignored. Array-typed keys take a TOML array of strings. `host_project_dir` / `host_home` are container-internal and not exposed via TOML.

```toml
base_image = "claude-base:latest"
devcontainer_image = "claude-devcontainer:latest"
run_image = "claude-run:latest"
mysql_config = "/path/to/mysql.cnf"
agent_timeout = 1800
poll_interval = 30
debug = false
```

Full set of keys:

| Key | Type | Default | Purpose |
|---|---|---|---|
| `base_image` | string | `"claude-base:latest"` | Common base Docker image |
| `devcontainer_image` | string | `"claude-devcontainer:latest"` | Devcontainer image (`cld`, `cld master`) |
| `run_image` | string | `"claude-run:latest"` | One-shot run image (`cld run`) |
| `mysql_config` | string | `""` | Path to a `.cnf` file, mounted RO at `/run/secrets/mysql.cnf` |
| `pyproject_dir` | string | `"."` | Directory (relative to repo root) holding `pyproject.toml` and `.env`. Not a `Config` field -- read directly out of `.cld/config.toml` by `cld-broker.sh` on the host, for the broker's `PROJECT_SUBDIR` and secrets path (see "Host-side test running" in `CLAUDE.md`) |
| `ssl_certs_path` | string | `""` | Opt-in override: host path (dir or PEM file) that **replaces** the baked CA bundle. Empty = use the baked bundle |
| `home_mounts_always` | array of strings | `[".claude.json", ".config/anthropic", ".config/claude", ".config/jj"]` | RO `$HOME` paths staged into every container |
| `home_mounts_devcontainer` | array of strings | `[".gitconfig", ".bashrc", ".config/nvim", ".local/state/nvim", ".cache/nvim"]` | Additional RO `$HOME` paths staged only for interactive devcontainer sessions |
| `master_targets` | array of strings | `[]` | Host repo paths registered as launchable sibling targets from inside `cld master` |
| `ignore_gitignore` | array of strings | `[]` | Gitignored files (e.g. `.env`) to symlink from `/workspace/origin` into the isolated workspace |
| `agent_timeout` | int (seconds) | `1800` | Chain orchestrator's per-agent wait timeout |
| `poll_interval` | int (seconds) | `30` | Chain orchestrator's docker-ps poll interval |
| `chain_max_parallel` | int | `4` | Max agents launched concurrently in a chain's parallel group |
| `chain_default_model` | string | `""` | Model override for chain agents; empty = each step's own default |
| `ssh_auth_sock` | string | unset (auto-detect) | SSH agent forwarding into `cld`/`cld master`. Unset = auto-detect host `$SSH_AUTH_SOCK`; `""` = explicitly disable; a path = use that socket |
| `mailbox_root` | string | `"~/.cld/mailboxes"` | Host root of the inter-container mailbox tree (bind-mounted RW into every master/agent) |
| `agent_max_turns` | int | `120` | Per-message turn cap passed to the repo agent's `claude -p --max-turns` |
| `agent_kickoff_persona` | string | `"agent"` | Persona used to kick off a new `cld agent` Claude session |
| `broker_key` | string | `""` | Host path to the restricted broker **private** key. Setting this enables `cld broker <action>` inside `cld master`. Master-only |
| `broker_endpoint` | string | `"host.docker.internal:2222"` | Broker SSH endpoint, `[user@]host:port` (default login user `zet`) |
| `broker_known_hosts` | string | `""` | Host path to the pinned `known_hosts` for the broker; required for the client's strict host-key check |
| `log_level` | string | `"INFO"` | Root level for the `cld` logger hierarchy: `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `log_color` | string | `"auto"` | ANSI color in log output: `auto` (TTY-detect) / `always` / `never` |
| `debug` | bool | `false` | Diagnostics flag; back-compat alias for `log_level = "DEBUG"` when `log_level` is otherwise unset |

Every key above also has a `CLD_*` env var equivalent that overrides it (see below) except the array-typed ones (`home_mounts_always`, `home_mounts_devcontainer`, `master_targets`, `ignore_gitignore`) and `pyproject_dir` (not a `Config` field), which are TOML-only.

### `CLD_*` env vars (defaults shown)

| Variable | Default | Purpose |
|---|---|---|
| `CLD_BASE_IMAGE` | `claude-base:latest` | Common base Docker image |
| `CLD_DEVCONTAINER_IMAGE` | `claude-devcontainer:latest` | Devcontainer image |
| `CLD_RUN_IMAGE` | `claude-run:latest` | One-shot run image |
| `CLD_HOST_PROJECT_DIR` | `""` | Host repo root path; set by host launcher into containers for nested docker path translation |
| `CLD_HOST_HOME` | `""` | Host home directory (for path translation) |
| `CLD_AGENT_TIMEOUT` | `1800` | Chain's per-agent wait timeout (seconds) |
| `CLD_POLL_INTERVAL` | `30` | Chain's docker-ps poll interval (seconds) |
| `CLD_CHAIN_MAX_PARALLEL` | `4` | Max agents running concurrently in a parallel chain group |
| `CLD_CHAIN_DEFAULT_MODEL` | `""` | Model override for all chain steps; empty = use chain YAML default |
| `CLD_LOG_LEVEL` | `INFO` | Root level for the `cld` logger hierarchy (DEBUG/INFO/WARNING/ERROR) |
| `CLD_LOG_COLOR` | `auto` | ANSI color in log output: `auto` / `always` / `never` |
| `CLD_DEBUG` | `false` | Diagnostics flag. Back-compat alias: truthy ⇒ `CLD_LOG_LEVEL=DEBUG` |

### Logging

`cld` writes diagnostic output via the stdlib `logging` module. All log records go to **stderr**; stdout is reserved for user-facing deliverable output (final reports, list rows, prompts).

| Env var | Default | Values |
|---|---|---|
| `CLD_LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR |
| `CLD_LOG_COLOR` | `auto` | auto / always / never |
| `CLD_DEBUG` | `false` | Back-compat alias: truthy ⇒ `CLD_LOG_LEVEL=DEBUG` |

The same keys can be set in TOML: `log_level`, `log_color`.

At DEBUG, every subprocess invocation (Docker, jj, git) and every VCS operation is logged with full command and exit code. At INFO (default), only major lifecycle events appear (agent starts/stops, image builds, chain steps).

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
