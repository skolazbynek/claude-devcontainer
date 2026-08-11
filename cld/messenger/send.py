"""Deliver a message to another container's mailbox.

Goes through the same ``gated_send`` as the messenger MCP tool, so an agent-to-agent
send is counted against that edge's hop budget here too -- this path is the one the
`messenger-send` skill documents, so a gate it skipped would be no gate at all.
"""

import argparse
import sys
from pathlib import Path

from cld.config import Config
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
    result = mailbox.gated_send(
        root, frm, args.to, args.subject, body,
        default_limit=Config.from_env().peer_absolute_limit,
    )
    if "error" in result:
        print(f"error: {result['error']}", file=sys.stderr)
        sys.exit(1)

    hops = f"  (hop {result['hops']}/{result['limit']})" if "hops" in result else ""
    print(f"sent: {result['id']}  {frm} -> {result['to']}{hops}")


if __name__ == "__main__":
    main()
