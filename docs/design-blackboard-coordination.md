# The VCS blackboard — a second coordination edge for task-agents

> **Status: next step — out of scope for the first task-agent POC.**
> Date: 2026-08-11. Builds on `docs/design-task-agents.md`, which already ships
> **both** master↔agent and agent↔agent messaging over the mailbox (that doc,
> §7). This document adds *only* the blackboard edge on top of that base;
> nothing here is implemented until the base POC is proven.

## Where this sits

The base task-agent POC has **one** coordination channel: the mailbox. The
master draws a graph of *messaging* edges — master↔agent and brokered
agent↔agent (`design-task-agents.md` §7) — and every handoff is a message that
needs a live recipient.

This document adds a **second kind of edge**: a *dependency* edge carried on the
shared VCS store instead of the mailbox — a durable, pull-based signal ("X is
ready, build on this branch") that needs no live recipient and stays off the
manually-cranked master's hot path. It does **not** change any base topology
decision (master still draws the graph; no cold-messaging; agents never spawn) —
it adds a channel, not a new authority model.

## Two kinds of edge: messaging vs. blackboard

- **Messaging edge (base):** *dialogue* — a question, a clarification, a steer.
  Push, ephemeral, needs a live recipient. Already built.
- **Dependency edge (this doc):** *facts* — "ready", the deliverable pointer.
  Pull, durable, survives its author. The blackboard.

They are complementary. Anything needing back-and-forth stays on the messaging
edge; anything that is a settled fact moves to the blackboard.

## Substrate — already present

All task-agent workspaces write into the same origin `.jj/repo/store`, so every
agent can already `jj log` and see every peer's commits and bookmarks. That
shared store **is** the blackboard. No new transport, no delivery, no
dead-recipient failure mode. The work is conventions on top of it, plus one
mechanism (the watcher, below).

## The board schema — bookmarks as slots

A reserved, master-owned bookmark namespace. Each agent owns only its own task's
slots; nobody writes another agent's slot. Minimal set:

- `deliverable/<task>` — the durable output branch (already defined in the base
  POC; master-assigned).
- `ready/<task>` — the readiness signal. Its **existence** means "done"; it
  points at the deliverable tip. Signal and artifact are the *same object*, so
  they can never disagree.
- optional coarse state by name: `wip/<task>`, `blocked/<task>`.

Richer payload, when a boolean isn't enough, rides in the **description of the
commit the bookmark points at** (`status: ready; 3 endpoints; note: …`), read
with `jj log -r ready/<task> -T description`. Pushing much past that is a sign
the exchange should be a *message* on the base channel, not a board post.

(The exact separator — `ready/<task>` vs `ready__<task>` — depends on jj's
bookmark-name rules; confirm before locking. It doesn't change the design.)

**Two iron rules (persona-enforced, not transport-enforced):**
- **Reference by bookmark / change-id, never commit hash.** Watchman auto-commit
  plus the self-merge rewrite hashes constantly; only bookmarks and change-ids
  are stable handles.
- **One writer per slot.** Concurrent writes to the *same* bookmark don't corrupt
  — jj marks it *conflicted* (both targets visible, needs resolution) — but
  per-agent slot ownership avoids ever hitting that.

## How the JJ tree looks

Repo `@` = `A` (shared anchor base). Master spawns an API agent and an SDK
agent off `A`:

```
A  (repo @, shared anchor base)
├── B_api   "cld anchor: cld_agent_api_api-contract"
│    └── … api work (Watchman-snapshotted) …
│         └── ● deliverable/api-contract   ← durable output bookmark
│              ↑ ready/api-contract         ← board: readiness (== deliverable tip)
│
└── B_sdk   "cld anchor: cld_agent_sdk_sdk-consume"
     └── (idle; supervisor watching  ready/api-contract)
```

When the API agent self-merges and posts `ready/api-contract`, the SDK agent
rebases its line onto it — the dependency becomes **ancestry**:

```
● deliverable/api-contract  (== ready/api-contract)
   └── B_sdk'  (rebased onto the contract)
        └── … sdk work …
             └── ● deliverable/sdk-consume
```

The master reads the whole fleet's coordination state in **one query**
(`jj bookmark list` filtered to the convention). That single-query observability
is the blackboard's biggest win over doing the same handoff as messages.

## The one mechanism to build: the board watcher

The blackboard is *pull*, but an agent's Claude only wakes when a message hits
its inbox — the supervisor's IDLE loop polls the mailbox, not the tree. Without
help, an agent would never *notice* a board post.

Fix: **extend the supervisor IDLE loop to watch bookmarks as well as the
inbox.** At spawn the master hands the agent a set of watched bookmarks — these
are the *dependency edges* of the graph it drew, the counterpart to the *peer
names* it already hands out for messaging edges. The loop polls `jj bookmark
list` for that set; when a watched bookmark appears or moves, the supervisor
synthesizes a **board-event wake** — a synthetic message into its own PROCESSING
path ("`ready/api-contract` now at change `xyz`: <description>") — resuming the
agent's Claude exactly as an inbox message would. The only new code is "poll
these bookmarks, diff against last seen, enqueue a wake on change"; the entire
processing/reply path is reused.

So "the master draws the graph" (a base decision) gains a second edge type: an
edge is either a **peer name** (messaging, base) or a **watched bookmark**
(blackboard, this doc), both set at spawn.

## Workflow example — contract handoff, blackboard version

1. Master spawns the API agent (`deliverable/api-contract`) and the SDK agent
   (`deliverable/sdk-consume`), telling the SDK agent at kickoff: "your input is
   `ready/api-contract`; you are watching it; do not build until it exists."
2. API agent works; Watchman auto-commits. On finish it self-merges into
   `deliverable/api-contract` and posts `ready/api-contract` at that tip.
3. The SDK agent's supervisor sees `ready/api-contract` appear → wakes its Claude
   with the board event. The agent rebases onto the contract and proceeds. **No
   messages on the hot path.**
4. The master, whenever cranked, reads `jj bookmark list` and sees the whole
   state — both agents, both deliverables, both ready-flags — at a glance.

Contrast the base messaging version of the same handoff: API messages the SDK
peer directly (fast, agent-cadence) or relays via master (two crank-hops). The
blackboard removes the message *and* the live-recipient requirement entirely for
a pure readiness fact.

## Pros

- **Inherently observable** — one query shows fleet state; no mailbox archaeology.
- **No delivery races, no dead-recipient black holes, no ordering constraints** —
  durable pull. A late-spawned agent self-serves current state; a torn-down agent
  *leaves* its `ready/`/`deliverable/` posts (the signal survives its author —
  exactly what you want, unlike a dead mailbox). This directly neutralizes the
  base POC's "master tears down a depended-on peer" failure mode for readiness
  handoffs (`design-task-agents.md` §10).
- **Signal ≡ artifact** — `ready/<task>` *is* the branch to build on.
- **Bounded state space** — a fixed schema of slots is far easier to reason about
  (and to prompt) than open-ended chat.
- **Off the master's critical path** — propagates at the watcher's cadence.

## Cons / pain points

- **Needs the watcher** — the one real build cost; without it the board is inert.
- **Facts only, not dialogue** — no clarifying questions or negotiation; those
  stay on the base messaging edge.
- **Stale slots** — a crashed agent's `wip/<task>` looks in-progress forever;
  needs a GC/heartbeat convention or master reconciliation.
- **Namespace pollution + cleanup semantics** — what happens to `ready/`/`wip/`
  on teardown/completion is an open decision.
- **Discipline-dependent** — the two iron rules live in personas, not the
  transport; breaking them yields conflicted bookmarks or dangling hash refs.
- **Concurrent op-log churn** — many agents writing bookmarks to one store is
  fine when slots are disjoint; jj surfaces same-slot collisions as conflicts
  (no data loss) but they're noise the master may have to clear.

## Open decisions (to settle when this is picked up)

1. **Bookmarks-only vs. also a tracked `.cld-board/<task>.json`** per agent
   (richer, human-readable, Watchman-published — but rides in the file lineage /
   deliverable unless excluded). Lean bookmarks-only for a first cut.
2. **Schema breadth** — just `ready/` + `deliverable/`, or also `wip/`/`blocked/`?
   Lean minimal; more slots = more staleness to manage.
3. **Watcher now vs. master-triggered** — supervisor watches bookmarks
   (autonomous, the design above) or the master nudges "check the board" (a
   one-line message, not a full relay). The watcher is the payoff but the build
   cost.
4. **Cleanup on completion/teardown** — which board bookmarks survive, which are
   forgotten.

## Non-goals (unchanged from the base POC)

- No agent-initiated spawning.
- No cold-messaging; all edges (messaging and dependency) are master-drawn.
- No open mesh, no broadcast, no group chat — the messaging channel stays 1:1
  along brokered edges.
