"""List cld containers (masters + agents) via Docker labels."""

import argparse

from cld.messenger import mailbox


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m cld.messenger.agents")
    ap.add_argument("--kind", choices=("agent", "master"), help="Restrict to one kind")
    args = ap.parse_args()

    containers = mailbox.list_containers(args.kind)
    if not containers:
        print("(no cld containers)")
        return

    for c in containers:
        print(f"  {c['status']:<8}  {c['kind']:<7}  {c['name']:<40}  {c['repo']}")


if __name__ == "__main__":
    main()
