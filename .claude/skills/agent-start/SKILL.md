---
name: agent-start
description: >
  Start a sibling `cld agent` container from within a master container.
  Resolves the target repo (master's own or one of the `master_targets`),
  launches an idempotent per-repo agent, and reports readiness. Invoke when
  the user wants to start, launch, spawn, or bring up an agent for another
  repo registered with this master.
user-invocable: true
---

# Agent: start a sibling `cld agent`

Launches a persistent per-repo agent as a sibling container of the current
master. `cld agent` is idempotent per repo -- if one already exists (running
or stopped), it reports that and returns without re-launching.

**Wrong skill for one bounded piece of work.** A repo agent is a standing
teammate that lives as long as its container; for a task with a definition of
done, a deliverable branch and a teardown, use `task-agent-start` instead. Many
task-agents can run per repo; only one repo agent can. Sibling agents
communicate via the mailbox/messenger transport once running (see the
`messenger-send` skill).

## Prerequisite: must run inside a master container

The skill assumes it is running inside a container started by `cld master`.
Confirm this before proceeding:

```bash
cld broker --help >/dev/null 2>&1 && [ -d /var/cld/mailboxes ] && echo master-ok
```

(`cld agent` from inside master reaches the host Docker daemon through the
`cld broker` client -- there is no docker socket in-container.)

If the check fails, stop and tell the user this skill only works from inside
a master container.

## Step 1: Resolve the target repo path

The target must be either master's own repo (`/workspace/origin` in-container,
which maps back to the host repo master was launched from) or one of the
`master_targets` paths registered in cld config. Sibling target paths appear
inside master as empty placeholder directories (no bind mount; master has no
filesystem view of the sibling content). List the reachable targets:

```bash
cld repos
# /host/side/cld     own
# /host/side/foo     target
# /host/side/bar     target
```

If the list is empty of `target` entries, the user has no siblings
configured. Add the host path to `master_targets` in cld config and restart
master (`cld master restart` picks up placeholder-dir changes at boot).

If the user did not name a target, present the list and ask them to pick
one. If exactly one target entry exists, you may propose it as the default
but still confirm.

## Step 2: Check whether an agent is already up for that repo

```bash
cd <target-path>
cld agent status
```

If status shows `running` or `stopped`, tell the user the agent already
exists; don't re-launch. Offer to attach via messenger, restart, or shutdown
instead.

## Step 3: Launch the sibling agent

```bash
cd <target-path>
cld agent
```

Optional first-launch flags (ignored on re-attach, so only meaningful when
step 2 said `absent`):

- `-r <revision>` -- anchor revision (default: current change).
- `-m <model>` -- Claude model.

There is no `-p` here: a repo agent's kickoff is its persona
(`agent_kickoff_persona`, default `@personas/agent`), and everything after that
arrives as a message. Send the first task with the `messenger-send` skill once
step 4 reports `idle`.

`cld agent` runs in the foreground on master's side only long enough to
stage the anchor and start the container; then it prints how to message the
agent and returns. The agent itself is detached.

## Step 4: Confirm readiness

```bash
cld agent status
```

Expect `Container: running` and a supervisor phase (`kickoff` on first
launch, then `idle` once the persona-driven kickoff finishes). Tail logs if
readiness lags:

```bash
cld agent logs -n 40
```

## Step 5: Report back to the user

Give the user:

- The target repo path.
- The container name (from `cld agent status`).
- One line on how to message it (`messenger-send` skill, `--to <repo-basename>`
  or `--to <container-name>`).

Do not stay in `<target-path>`; `cd -` back to where you started so
subsequent tool calls aren't rooted in the sibling repo.
