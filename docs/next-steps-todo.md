# Next steps

This document outlines next steps and tasks for Claude Code agents to complete. A Master is expected to work dynamically with this file, read the issues, research and design solutions, make appropriate decisions, consider the big picture and the idea itself (and push back on user if not sure), then write tests for the expected design and plan implementation and implement a solution.

Any part of the workflow can be delegated to a spawned task-agent. Master is responsible for handling the concurrency between multiple running agents: before launching concurrent agents, make sure that the tasks do not depend on each other; and after multiple concurrent agents finish their tasks, it's the masters responsibility to reconcile their work into proper VCS branch.

## Empty mounted directories
The master container is started with a set of empty directories that are mocking other host-side repositories to be able to launch agents on them. Are these directories actually needed? Is there a way with current changes to task-agents, host-broker etc. to launch agents in different repos?
If it's completely redundant (we can launch agents from inside master on top of different repos in another, existing way), delete / refactor it. If it's not redundant, but there's a different, simpler design that would achieve that, then architect that design and output a design document for it: do not implement.

## BrokerCTL naming
The host-side brokerctl, script, ssh daemon and the whole design feature should have a single, unifying naming both on host and inside container. It's currently named brokerctl on host and host-run inside the container. Make sure it's semantics are unified.

**Done.** One root word, `broker`: host `broker/cld-broker.sh` (the sshd ForceCommand
dispatcher) + `broker/cld-brokerctl.sh` (operator control) + `/etc/cld/broker.conf`;
container `cld broker <action>` (`cld/broker.py`, Python -- the generated `host-run`
shell wrapper is gone); config keys `broker_key` / `broker_endpoint` /
`broker_known_hosts` (old spellings are a hard error, not a silent broker-off).
Naming table and operator migration: `docs/design-cld-broker.md` §0.

## Different CLD clis for host and for container
Within the CLD application, there are commands usable only from host or usable only from container. Design a split: a CLI application used on host and different one shipped into containers. The goal is to make the usage cleaner and more straighforward. It's possible to design a different way to ship the application into the master container if it helps usability.

**Done.** Design + implementation: `docs/design-cli-split.md`. `cld.cli` stays the host
app; `cld.cli_container` is the container app, shipped as `cld` by a shim in the
devcontainer image (there was no `cld` executable in any image before this, so every
skill that said `cld task-agent …` was broken). Shared task-agent helpers moved to
`cld/task_agent.py`; the messenger got first-class verbs (`cld msg …`).

## Chaining prompts
All commands accepting personas / task-files should change and unify their interface. They should accept an arbitrary (maybe limited) number of task-files/personas and a single positional argument (task description). The task-files are interchangeable with personas. The task-files can be specified either by a file path or by the @<taskfile> notation that works the same way as now: get's the respective file from the `cld/prompts` directory. The task-files are appended one after another in the order they were specified.
Example of my vision: `cld run @personas/architect @personas/agent ./tasks/task_description.md -p "When finished, reply to the master"`.
The goal is to provide a unified prompt interface for CLI commands that allows chaining multiple prepared prompts together.

**Done.** Design: `docs/design-prompt-chaining.md`. One resolver
(`cld.prompts.resolve_prompt_arg` -> `(path, kind)`, `@<path-under-prompts>` or a real
path, containment-checked), one composition (`compose_brief`: refs in argument order,
then `-p` verbatim, no invented headers), one channel into the container (the anchor
scratch envelope -> `.cld-run/brief.md`, replacing the `/config/{persona,task}.md`
mounts, `AGENT_INLINE_PROMPT`, `AGENT_SYSTEM_PROMPT_FILE` and `INSTRUCTION_FILE`).
Applies to bare `cld`, `cld run`, `cld master`, `cld task-agent start`, `cld chain run`
and chain steps (`persona:` -> `prompts: [...]`). The broker now requires every
positional to be an `@ref`; the container client folds its own local files into `-p`.
