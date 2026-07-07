---
name: messenger-archive
description: >
  Move a cld messenger message from the caller's inbox to their archive.
  Invoke when the user wants to archive, dismiss, or mark a message as done.
user-invocable: true
---

# Messenger: archive message

The user must provide a message id. Run:

```bash
python -m cld.messenger.archive <id>
```

Show the output verbatim.
