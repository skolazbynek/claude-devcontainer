---
name: task-agent-start
description: >
  Spawn a task-scoped agent (`cld task-agent start`) from a master container: pick a
  slug and a role persona, write the task, and draw its peer edges. Invoke when the
  user wants to hand a bounded piece of work to a fresh agent, fan work out across
  repos, or add an agent to a running fleet.
user-invocable: true
---

# Task-agent: spawn one

A task-agent is bounded to **one task**, driven by you, and torn down when its work
has landed. That is the difference from `cld agent` (one standing agent per repo,
immortal for as long as its container runs — see the `agent-start` skill).

## Prerequisite: inside a master, with a host channel

```bash
[ -x /tmp/bin/host-run ] && [ -d /var/cld/mailboxes ] && echo master-ok
```

Spawning and reaping need the host broker (there is no docker socket in-container).
*Reading* the fleet does not — `fleet_digest()`, `read_mailbox()` and
`cld task-agent transcript` all work off the mounted mailbox tree, so if the check
fails you can still observe and message agents, just not create them.

## Step 1: Resolve the target repo

```bash
cld repos          # <path> own | <path> target
```

`cd` to the target before the rest. An agent for your own repo and an agent for a
registered sibling are spawned exactly the same way; you have no filesystem view of a
sibling, which is what makes the *pushed branch* rule in `task-agent-wrapup` matter.

## Step 2: Choose the slug, the persona and the branch

- **Slug** (`-n`): short, kebab-case, describes the task — `add-oauth`, not `task-1`.
  It becomes the container name (`cld_agent_<repo>_<slug>`), the handle for every other
  command, and the default deliverable branch. Prefer a fresh slug over re-using one; a
  live collision gets a `-2` suffix and stops being self-explanatory.
- **Persona**: the role, resolved from `prompts/personas/` (`implementer`, `architect`,
  `reviewer`, …). Pass the bare name — a path is refused.
- **`--branch`**: only when the deliverable branch should differ from the slug.

## Step 3: Write the task

The task is the agent's whole brief; it will not see this conversation. State the goal,
the constraints, what "done" looks like, and any response-format requirement you want
back. Pass it with `-p`, or point at a `@<prompt-name>` from the target repo's
`prompts/`. Keep it to a few KB — it travels to the host inside the broker's argv.

## Step 4: Draw the peer edges

`--peer <full-container-name>[:<hops>]`, repeatable. An agent may message **only** you
and the peers you name here.

Two rules that follow from how edges work:

- **Only an already-spawned agent can be named**, so the *later*-spawned side declares
  the edge. Spawn A, then spawn B with `--peer <A>`; B can then message A, and A may
  reply (a reply is always allowed).
- **The declared budget governs the whole edge** — total messages, both directions, for
  its life. `:<hops>` sets it; omitted, it is the configured default. When it is spent
  the edge goes **silent** in both directions and the blocked agent escalates to you.
  So size it for the exchange you expect, and expect to be the one who breaks a tie.

Peers are addressed by full container name; a repo basename is ambiguous with several
task-agents per repo.

## Step 5: Spawn

```bash
cld task-agent start <persona> -n <slug> -p "<task>" \
    [--branch <name>] [-m <model>] [-r <revision>] [--peer <name>[:<hops>]]…
```

Two refusals to plan around rather than hit:

- **The cap** (`max_task_agents`, default 4 running per master). Reap something
  finished first — see `task-agent-wrapup`.
- **The anchor.** `-r` may point at a *finished* sibling's deliverable branch, but not
  inside a **live** agent's stack, because that agent can still rewrite it. A sequential
  handoff is therefore **reap-then-spawn**, not spawn-on-top.

The command stages the anchor, starts the container detached and waits for readiness —
up to 60 s, longer if it has to build the image first. That is normal, not a hang.

## Step 6: Report back

Give the user the container name, the task slug, the deliverable branch, the peers with
their budgets, and how to watch it (`cld task-agent status <slug>`, then the
`task-agent-fleet` skill for the ongoing loop). `cd -` back afterwards so later tool
calls are not rooted in a sibling repo.
