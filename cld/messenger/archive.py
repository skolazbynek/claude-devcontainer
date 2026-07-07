"""Move a message from the caller's inbox to their archive."""

import argparse
import sys

from cld.messenger import mailbox
from cld.messenger.identity import resolve_self


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m cld.messenger.archive")
    ap.add_argument("id")
    args = ap.parse_args()

    name, root = resolve_self()
    if not mailbox.archive_message(root, name, args.id):
        print(f"Message not found in inbox: {args.id}", file=sys.stderr)
        sys.exit(1)
    print(f"archived: {args.id}")


if __name__ == "__main__":
    main()
