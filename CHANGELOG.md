# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `cld build` subcommand that builds devcontainer and agent images in the right order.
- `cld --version` flag.
- `cld review` auto-detects trunk branch (main/master/trunk) when not given.
- `cld loop --approve`'s prompt now offers `[e]dit` to modify the next iteration's prompt.
- `cld loop` now reports cumulative cost across iterations from each agent's `result.json`.
- `cld loop` exposes a configurable `agent_timeout`.
- `ensure_image` builds parent images automatically and supports `force`/`--no-cache`.
- Agent system prompt is externalized to `imgs/claude-agent/agent-system-prompt.md` (override via `AGENT_SYSTEM_PROMPT_FILE`).
- Orchestrator's `list_agents` now also enumerates completed agent branches.
- Orchestrator's `check_status` distinguishes `failed` (missing `summary.json`) and surfaces `AGENT-FAILURE.md` contents.

### Changed
- `~/.config` is no longer mounted in full; only `anthropic/`, `claude/`, `nvim/` subdirs are mounted (avoid leaking gh/aws/gcloud creds).
- Session names use `secrets.token_hex(3)` instead of `random.randint` (collision resistance for parallel runs).
- LLM-generated commit message in agent entrypoint is opt-in via `AGENT_COMMIT_MSG_LLM=1`; default uses `git diff --stat`.
- `_to_host_path` is now public `to_host_path` and uses `CONTAINER_HOME` instead of `os.path.expanduser("~")` inside containers.
- `vcs_describe` MCP tool's argument order now matches the backend's `describe(revision, message)`.
- Review-then-fix flow standardized on `CODE_REVIEW.md` as the canonical output filename.
- `prompts/team-leader.md` renamed to `prompts/team-orchestrator.md` to match the documented `claude --agent team-orchestrator` invocation.
- `prompts/graphql-mcp.md` moved to `docs/graphql-mcp.md` (it was documentation, not a task prompt).

### Fixed
- `cld headless` now correctly passes through unknown flags to `claude -p`.
- Top-level CLI commands wrap errors so common failure modes (no VCS repo, missing docker, etc.) print one-line `[ERROR]` instead of a Python traceback.
- `git` backend: `diff(revision)` handles root commits via the empty-tree object instead of producing empty output.
- `git` backend: `new_change` no longer runs `git checkout` twice.
- `git` backend: `squash(from_rev, into_rev)` actually honors its arguments.

## [0.1.0] - 2026-04-27

Initial public release.

- Devcontainer with neovim, jj, git, poetry, claude (`cld devcontainer`).
- Autonomous agent runner committing results to a VCS branch (`cld agent`).
- Code-review agent with diff generation (`cld review`).
- Automated implement-review loop (`cld loop`).
- Headless wrapper around `claude -p --permission-mode acceptEdits` (`cld headless`).
- MCP orchestrator server for managing sibling agent containers from inside Claude Code.
- VCS abstraction supporting both jujutsu (preferred) and git (fallback).
