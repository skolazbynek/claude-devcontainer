# Two CLIs: one for the host, one shipped into containers

> Requested by `docs/next-steps-todo.md` § "Different CLD clis for host and for
> container". This document is the design; the implementation lands with it.

## Problem

1. **There is no `cld` executable in any image.** The package is `COPY`'d to
   `/opt/cld/cld/` with `PYTHONPATH=/opt/cld`; the only entry point is
   `python3 -m cld`. Every baked skill nevertheless instructs `cld task-agent
   start …`, `cld task-agent status`, `cld master repos`. Verified inside a live
   master: `which cld` finds nothing. The documented in-master workflow does not
   run.
2. **One app carries commands that cannot work in a container.** `build`, `run`,
   `master`, `chain` and the bare devcontainer launch all need a docker daemon,
   which no container has. They are refused at runtime by `_reject_in_master` and
   ~12 scattered `in_master_container()` branches — after appearing in `--help` as
   if they were available.
3. **The container's real capabilities are not in the CLI at all.** The messenger
   is `python3 -m cld.messenger.{send,inbox,read,archive,agents}`; the broker is a
   generated shell wrapper at `/tmp/bin/host-run`. One tool, three invocation
   styles, none of them `cld`.

## Decision

Two typer apps in one package:

- **`cld/cli.py` — the host CLI.** Today's surface, minus every in-master branch.
- **`cld/cli_container.py` — the container CLI.** Only verbs that work inside a
  container, wired directly to the seam each one uses (broker or mailbox).

The devcontainer image installs a shim so the container CLI *is* `cld`:

```dockerfile
RUN printf '#!/bin/sh\nexec python3 -P -m cld.cli_container "$@"\n' > ~/.local/bin/cld \
    && chmod +x ~/.local/bin/cld
```

Same command name on both sides, different surface, because the two never coexist:
the host's `cld` comes from `poetry install` and is not mounted into any container.
Skills, docs and muscle memory keep working; `cld --help` starts telling the truth.

`claude-run` (the one-shot agent image) gets no shim: a one-shot agent has no
mailbox, no fleet and no broker key, so it has nothing to run.

### Why not the alternatives

- **Two packages / distributions** — the container app is mostly a thin front-end
  over the same modules (`mailbox`, broker seam, `config`). A second distribution
  doubles the shipping story and buys nothing.
- **One app, conditional wiring on `MASTER_MODE`** — the condition is precisely what
  makes `--help` lie today. A separate module makes the container surface
  reviewable in one file, and keeps host code from knowing about containers.
- **`pip install /opt/cld` for a real console script** — needs a build inside the
  image (and `pyproject.toml` in the copied tree) to produce a two-line shim. The
  shim is the whole benefit.

## Container surface

| verb | seam | notes |
|---|---|---|
| `task-agent start/status/logs/shutdown` | host broker | same argv as the host |
| `task-agent transcript` | mailbox mount | no host channel needed |
| `agent [start]/restart/shutdown/status/logs` | host broker | |
| `repos` | `MASTER_TARGETS` env | was `master repos`; see `design-master-target-selection.md` |
| `msg send/inbox/read/archive/agents` | mailbox mount | replaces `python3 -m cld.messenger.*` |
| `broker <action> …` | ssh to the host broker | **lands with the broker-naming change**, not this one; until then the `host-run` wrapper stays as it is |
| `prompts` | prompts trees | lists the `@refs` a task or persona argument accepts |

Host-only verbs (`run`, `master`, `chain`, `build`, and a bare `cld` with no
subcommand) are registered as **hidden stubs**: they print one line — "host-only:
run this on the host" — and exit 2. A good error instead of typer's "No such
command", without cluttering `--help`.

One container app serves masters, repo agents and task-agents (they share the
image). A task-agent has a mailbox but no broker key, so its broker-backed verbs
fail with the existing "the host broker is not configured for this container"
message, which is the accurate diagnosis.

## Module layout

- `cld/cli.py` — host app. Loses `_reject_in_master`, `_dispatch_agent_to_broker`,
  `_dispatch_task_agent_to_broker`, `_task_agent_start_argv` and every
  `in_master_container()` branch. Gains one guard in the app callback: invoked with
  `MASTER_MODE`/`AGENT_MODE` set (i.e. `python3 -m cld` inside a container), it
  refuses and points at `cld`.
- `cld/cli_container.py` — container app: broker dispatch, mailbox reads, the
  `--repo`-free cwd-independent verbs, the hidden host-only stubs.
- `cld/task_agent.py` (new) — the pieces both apps need, moved out of `cli.py` as
  plain functions with no typer coupling: `_mailbox_root`, `_parse_peer_specs`,
  `_format_peers`, `_known_task_agent_names`, `_resolve_task_agent`,
  `_task_agent_rows`, roster/detail rendering, `_task_agent_record`,
  `_task_agent_parent`.

The container app is where the broker argv is rebuilt (`_task_agent_start_argv`),
because that translation exists only for the container→host hop.

## Consequences

- `cld task-agent start …` inside master works, as the skills already claim.
- `cld --help` in a container lists what that container can do, and nothing else.
- Host code paths lose their container branches; `in_master_container()` survives
  only in the one guard above and in `docker.py`'s launch-time plumbing.
- The messenger gets a first-class CLI (`cld msg …`); `python3 -m cld.messenger.*`
  stays as the module-level implementation the MCP server and the CLI both call.

## Tests

- `tests/test_cli_container.py` (new): every verb dispatches to the expected seam
  (broker call vs mailbox read), host-only stubs exit 2 with a host-only message,
  `repos` reads `MASTER_TARGETS`, broker verbs report the missing-broker case.
- `tests/test_cli.py`: the host app no longer branches on `MASTER_MODE`; the new
  callback guard refuses when it is set.
- `tests/test_task_agent.py` (new or folded into `test_cli.py`): the moved helpers
  keep their behaviour (slug resolution, roster rows, peer specs).

## Out of scope

- The chainable prompt interface (`docs/next-steps-todo.md` § "Chaining prompts")
  — it changes argument *shapes*, not which side owns which verb, and lands after
  this split.
- `--repo` target selection (`design-master-target-selection.md`), which is
  design-only for now: the container CLI keeps resolving the target from cwd.
- Any change to the broker's actions or wire format.
