"""Move a message from the caller's inbox to their archive."""

import argparse
import sys

from cld.messenger import mailbox
from cld.messenger.identity import resolve_self


def move(msg_id: str) -> None:
    name, root = resolve_self()
    if not mailbox.archive_message(root, name, msg_id):
        print(f"Message not found in inbox: {msg_id}", file=sys.stderr)
        sys.exit(1)
    print(f"archived: {msg_id}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m cld.messenger.archive")
    ap.add_argument("id")
    move(ap.parse_args().id)


if __name__ == "__main__":
    main()
