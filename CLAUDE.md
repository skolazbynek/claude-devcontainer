# CLAUDE.md

Docker container orchestration for Claude Code with jujutsu/git workspace isolation.

## Roles

- **cld** — ephemeral interactive devcontainer
- **cld master** — persistent per-repo interactive
- **cld agent** — persistent per-repo headless (mailbox-driven)
- **cld task-agent** — bounded task-scoped with peer messaging
- **cld run** — one-shot autonomous headless agent

Prefers jujutsu; falls back to git.

## Core Contracts

**Anchor + descendant tree:** Containers can only edit changes descending from anchor (`-r`, default `@`). Works in isolated `/workspace/current`; origin at `/workspace/origin`.

**Persistence:** Sessions survive restart via jj bookmarks. Watchman snapshots autonomously.

**Config:** defaults → `~/.config/cld/config.toml` → repo `.cld/config.toml` → `.env` → `CLD_*` env vars. Key: `mailbox_root`, `agent_max_turns`, `master_targets`, `broker_*`.

## Anchor Modes

- **Isolated (default):** Edit one branch only. No touching siblings.
- **Shared (`--shared-anchor`):** Edit entire subtree. Needs approval.

No overlap: live anchor trees checked host-wide.

## Subsystems

**Workspace files:** `ignore_gitignore` in `.cld/config.toml` symlinks `.env` from origin into workspace.

**Broker:** SSH ForceCommand for tests + agent lifecycle. Secrets mounted RO into ephemeral `runtests`. See `broker/README.md`.

**Messenger:** Mailbox at `~/.cld/mailboxes`. `send()/list_inbox()/read_message()/archive()` via MCP. Hop budgets; explicit reply obligations. See `docs/design-agent-messaging.md`.

**Mattermost bridge:** `cld bridge start|stop` routes `@<name>` posts to mailboxes. See `docs/impl-mattermost-bridge-plan.md`.

**Chain:** `cld chain <file.yaml>` runs declarative multi-step pipeline. Persistent accumulator; transient per-step branches.
