"""Print a full message by id."""

import argparse
import sys

from cld.messenger import mailbox
from cld.messenger.identity import resolve_self


def show(msg_id: str) -> None:
    name, root = resolve_self()
    msg = mailbox.read_message(root, name, msg_id)
    if msg is None:
        print(f"Message not found: {msg_id}", file=sys.stderr)
        sys.exit(1)

    print(f"id:      {msg['id']}")
    print(f"from:    {msg['from']}")
    print(f"to:      {msg['to']}")
    print(f"ts:      {msg['ts']}")
    print(f"subject: {msg['subject']}")
    print()
    print(msg["body"])


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m cld.messenger.read")
    ap.add_argument("id")
    show(ap.parse_args().id)


if __name__ == "__main__":
    main()
