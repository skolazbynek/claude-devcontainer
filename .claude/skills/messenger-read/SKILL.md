---
name: messenger-read
description: >
  Show the full contents of one cld messenger message by id (headers plus body).
  Searches the caller's inbox first, then their archive. Invoke when the user
  wants to read, open, or view a specific message.
user-invocable: true
---

# Messenger: read message

The user must provide a message id (obtained from `messenger-inbox`). Run:

```bash
cld msg read <id>
```

Show the output verbatim. Does not archive the message.
