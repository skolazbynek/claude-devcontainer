"""Deliver a message to another container's mailbox."""

import argparse
import sys
from pathlib import Path

from cld.messenger import mailbox
from cld.messenger.identity import resolve_self


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m cld.messenger.send")
    ap.add_argument("--to", required=True, help="Recipient shortname or full container name")
    ap.add_argument("--subject", required=True)
    ap.add_argument("--body-file", required=True, help="Path to file containing the message body")
    args = ap.parse_args()

    body = Path(args.body_file).read_text()

    frm, root = resolve_self()
    try:
        resolved = mailbox.resolve_recipient(args.to)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    msg = mailbox.write_message(root, frm, resolved, args.subject, body)
    print(f"sent: {msg['id']}  {frm} -> {resolved}")


if __name__ == "__main__":
    main()
