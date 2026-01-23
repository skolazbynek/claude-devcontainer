# Claude Code - Safe Docker Container

Run Claude Code in an isolated Docker container with explicit volume mounts and security restrictions.

## Quick Start

```bash
# Build the image
docker build -t claude-code-safe .

# Run Claude Code
./run-claude.sh
```

First run will prompt for OAuth authentication via browser. Subsequent runs use cached credentials.

## Features

**Security:**
- Non-root user (UID 1000)
- Drops all capabilities (`--cap-drop=ALL`)
- No privilege escalation (`--security-opt=no-new-privileges`)
- Resource limits (2 CPUs, 4GB RAM)
- Explicit volume mounts only
- Network access limited to Claude API

**Functionality:**
- OAuth authentication (tokens persisted in `~/.config/claude`)
- Current directory auto-mounted to `/workspace/current`
- Git for local operations only (no SSH, no remote push/pull)
- Python 3 with pip included

## Usage

```bash
# Basic usage (mounts current directory)
./run-claude.sh

# Pass arguments to Claude Code
./run-claude.sh "your prompt here"
./run-claude.sh --help
```

## Multiple Repositories

Mount additional repositories via `CLAUDE_REPOS` environment variable:

```bash
# One-time
CLAUDE_REPOS=/path/to/repo1,/path/to/repo2 ./run-claude.sh

# Persistent (create .env file)
cp .env.example .env
# Edit .env and set: CLAUDE_REPOS=/path/to/repo1,/path/to/repo2
./run-claude.sh
```

Repositories are mounted as `/workspace/<repo-name>`.

## What Gets Mounted

| Host Path | Container Path | Mode |
|-----------|---------------|------|
| Current directory | `/workspace/current` | rw |
| `~/.config/claude` | `/home/claude/.config/claude` | rw |
| `~/.gitconfig` | `/home/claude/.gitconfig` | ro |
| Additional repos | `/workspace/<name>` | rw |

## Isolation

The container can **only** access:
- Explicitly mounted directories
- Network (for Claude API calls)

Everything else on your filesystem is inaccessible from inside the container.

**Note:** Git is available for local operations (init, commit, branch, log, diff) but cannot push/pull to remote repositories. While the SSH client is installed (git dependency), no SSH keys or authentication is configured, making remote git operations impossible.

## Customization

Edit `run-claude.sh` to adjust:
- Resource limits (lines 65-66)
- Security options (lines 58-60)
- Image name (line 5)

## How Claude Code is Installed

The Dockerfile uses the official Claude Code installer script from `https://claude.ai/install.sh`. The installer:
1. Downloads the latest Claude Code binary (version 2.1.15 as of build)
2. Installs it to `~/.local/bin` in the container
3. Copies the binary to `/usr/local/bin/claude` for system-wide access

The installer is preferred over copying from the host because:
- Gets the latest stable version automatically
- No dependency on host system having Claude installed
- Proper installation with all dependencies resolved
- Works consistently across different host operating systems

## Using the Container Interactively

To get a bash shell inside the container (where you can run `claude` manually):

```bash
./run-claude.sh
```

Then inside the container:
```bash
claude --help
claude "your prompt"
```

## Requirements

- Docker
- Bash
- OAuth access to Claude Console (you'll authenticate via browser on first run)
