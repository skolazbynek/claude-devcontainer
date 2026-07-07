"""List the caller's mailbox (inbox by default, or archive with --all)."""

import argparse

from cld.messenger import mailbox
from cld.messenger.identity import resolve_self


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m cld.messenger.inbox")
    ap.add_argument("--all", action="store_true", help="Include archived messages")
    args = ap.parse_args()

    name, root = resolve_self()
    unread = mailbox.list_inbox(root, name, unread_only=True)
    archived = mailbox.list_inbox(root, name, unread_only=False) if args.all else []

    if not unread and not archived:
        print(f"(no messages for {name})")
        return

    print(f"# {name}")
    if unread:
        print(f"\n## inbox ({len(unread)})")
        _print_rows(unread)
    if archived:
        print(f"\n## archive ({len(archived)})")
        _print_rows(archived)


def _print_rows(rows: list[dict]) -> None:
    for m in rows:
        print(f"  {m['id']}  {m['ts']}  {m['from']:<30}  {m['subject']}")


if __name__ == "__main__":
    main()
