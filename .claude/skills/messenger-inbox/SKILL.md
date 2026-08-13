---
name: messenger-inbox
description: >
  List unread messages in the caller's cld messenger mailbox. In a cld container
  the caller is that container; on the host, it's the master container for the
  current cwd repo. Invoke when the user asks to check inbox, list messages,
  or see mail.
user-invocable: true
---

# Messenger: list inbox

Run:

```bash
cld msg inbox
```

Add `--all` to include the archive as well:

```bash
cld msg inbox --all
```

Show the output verbatim. Each row is `<id>  <ts>  <from>  <subject>`. The ids
are what the user passes to `messenger-read` or `messenger-archive`.
