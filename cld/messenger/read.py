"""Print a full message by id."""

import argparse
import sys

from cld.messenger import mailbox
from cld.messenger.identity import resolve_self


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m cld.messenger.read")
    ap.add_argument("id")
    args = ap.parse_args()

    name, root = resolve_self()
    msg = mailbox.read_message(root, name, args.id)
    if msg is None:
        print(f"Message not found: {args.id}", file=sys.stderr)
        sys.exit(1)

    print(f"id:      {msg['id']}")
    print(f"from:    {msg['from']}")
    print(f"to:      {msg['to']}")
    print(f"ts:      {msg['ts']}")
    print(f"subject: {msg['subject']}")
    print()
    print(msg["body"])


if __name__ == "__main__":
    main()
