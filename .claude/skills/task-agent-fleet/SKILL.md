---
name: task-agent-fleet
description: >
  Reconcile a fleet of task-agents from a master: read the cheap digest, pull only the
  mailboxes that moved, route replies and report what changed. Invoke when the user asks
  what the agents are doing, whether anyone replied, what the fleet has cost, or at the
  start of a turn while a fleet is running.
user-invocable: true
---

# Task-agent: reconcile the fleet

You are the control tower. Agents poll their own inboxes about every second, but nothing
advances *in you* between turns — so a running fleet needs reconciling when you take a
turn, and that is this skill.

## Step 1: The digest first, always

```
fleet_digest()      # messenger MCP tool
```

One row per agent you spawned: `{name, task, phase, msg_count, cost_usd_total, unread,
last_activity}`. No message bodies, so it is cheap enough to call every turn.

**Do not sweep inboxes instead.** An agent archives each message within about a second
of processing it, so `inbox/` is empty almost always — a sweep shows nothing *and* fills
your context with fleet chatter on turns where the user asked about something else.

## Step 2: Read only what moved

Compare `msg_count` and `last_activity` against what you saw last turn. For each agent
that changed:

```
read_mailbox(name, since="<ts of the last entry you saw>")
```

`since` is exclusive, so this returns only what is new. It covers both directions —
what the agent received and what it sent — including peer-to-peer traffic on edges you
drew, so nothing on a sanctioned edge is invisible to you. A reaped agent's archived
mailbox stays readable.

Read the phases too:

- `processing` — mid-turn. It is not ignoring you; do not reap it (shutdown would refuse).
- `idle` with a fresh `last_activity` — it replied; there is something to route.
- `idle` and quiet for a long time — it is waiting on *you*. Check what its last message
  asked for.
- `kickoff` — still booting its first turn.
- `stopped` — its supervisor exited cleanly. Its mailbox is still there.

## Step 3: Route and answer

Every message an agent sends you expects exactly one reply from you; that is how it
learns anything. `send(to="<full container name>", …)` — your channel to an agent is
never hop-budgeted, so use it freely.

When two agents need to talk to each other repeatedly, prefer drawing a peer edge at
spawn (`task-agent-start`) over relaying: a peer hop lands in about a second, while a
relay through you costs two of your turns and a copy-paste.

If an agent reports a **spent hop budget**, it is telling you an exchange was cut off
mid-conversation — the transport now delivers nothing more on that edge, in either
direction, and its peer was told nothing. Decide: relay the remaining question yourself,
or reap and re-spawn with a bigger budget. Never tell the agent to retry; it cannot.

## Step 4: Report what changed

In your turn output, tell the user what moved: who replied, what they said in a line,
what it has cost so far, anything now blocked on a decision, and any agent you reaped
(with its deliverable branch). The user has no other window into the fleet between
turns, so an unreported reap is a finished piece of work that silently vanished from
view.

Keep it to what changed. A fleet of four idle agents needs one line, not four.

## Also available

- `cld task-agent status` — the roster with docker truth (running / stopped / **gone**).
  `gone` means a mailbox with no container: a crashed agent that needs clearing.
- `cld task-agent status <slug>` — one agent in detail (task, persona, branch, anchor,
  peers, phase, cost).
- `cld task-agent logs <slug>` — supervisor stderr, i.e. state and cost. **Not** the
  conversation; that is `read_mailbox` or `cld task-agent transcript <slug>`.
