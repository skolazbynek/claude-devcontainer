---
name: messenger-agents
description: >
  List running and stopped cld containers (masters and agents) that can be
  targeted with the messenger. Invoke when the user asks which agents,
  masters, or containers are around, or wants to know who to send a message to.
user-invocable: true
---

# Messenger: list containers

Run:

```bash
python -m cld.messenger.agents
```

Restrict to one kind with `--kind agent` or `--kind master`.

Show the output verbatim. Each row is `<status>  <kind>  <name>  <repo>`. The
name column is what the user passes to `messenger-send --to`. The repo basename
(last path component) also works as a shortname for `--to`.
