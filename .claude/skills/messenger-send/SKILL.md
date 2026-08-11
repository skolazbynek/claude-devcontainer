---
name: messenger-send
description: >
  Compose and send a cld messenger message to another container's mailbox.
  Drafts subject and body from the user's intent, then delivers atomically.
  Invoke when the user wants to send, message, ping, or write to another agent
  or master.
user-invocable: true
---

# Messenger: send message

Compose a message from the user's intent and deliver it.

## Step 1: Resolve the recipient

If the user did not name a recipient, list available targets:

```bash
python -m cld.messenger.agents
```

The recipient is the container `name` (or its repo basename as a shortname).
An agent is preferred over a master when both exist for the same shortname.

**Task-agents and peers must be addressed by full container name** -- several
task-agents can share one repo, so a basename identifies nothing and the send is
refused as ambiguous.

## Step 2: Draft subject and body

From the user's intent, draft:

- **Subject** — one line, imperative, specific. No trailing period.
- **Body** — markdown. State the ask (or the update), give the minimum context
  the recipient needs to act, and end with what response you expect (a reply, an
  action, no reply). Skip preamble and sign-offs; the transport already records
  who sent it.

Confirm the draft with the user before sending unless they asked to send
immediately.

## Step 3: Deliver

Write the body to a tmp file (multi-line safe) and invoke the sender:

```bash
BODY_FILE="$(mktemp -t messenger-send-body.XXXXXX.md)"
cat > "$BODY_FILE" <<'EOF'
<the drafted body verbatim>
EOF
python -m cld.messenger.send --to <recipient> --subject "<subject>" --body-file "$BODY_FILE"
rm -f "$BODY_FILE"
```

Report the returned id to the user.

## Hop budget (agent-to-agent only)

A message between two task-agents counts against that edge's absolute hop budget, and
the output reports your position: `sent: <id>  a -> b  (hop 3/10)`. Converge as it
nears the limit -- land the exchange rather than letting it drift.

Once the budget is spent the send is **refused** (exit 1) and nothing is delivered, in
either direction, ever again on that edge. Do not retry it and do not work around it:
tell your master, whose channel is never budgeted, and let it decide what happens next.
Messages to a master are never counted.
