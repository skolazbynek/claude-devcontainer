"""List cld containers (task-agents, tickets) via Docker labels.

``agent`` stays an accepted ``--kind`` so a container left over from before the
standing per-repo agent role was removed is still listable.
"""

import argparse

from cld.messenger import mailbox


def show(kind: str | None = None) -> None:
    containers = mailbox.list_containers(kind)
    if not containers:
        print("(no cld containers)")
        return

    for c in containers:
        print(f"  {c['status']:<8}  {c['kind']:<7}  {c['name']:<40}  {c['repo']}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m cld.messenger.agents")
    ap.add_argument(
        "--kind", choices=("agent", "task-agent", "ticket"), help="Restrict to one kind"
    )
    show(ap.parse_args().kind)


if __name__ == "__main__":
    main()
