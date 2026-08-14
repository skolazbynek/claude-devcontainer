"""List cld containers (masters, repo agents, task-agents) via Docker labels."""

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
        "--kind", choices=("agent", "master", "task-agent"), help="Restrict to one kind"
    )
    show(ap.parse_args().kind)


if __name__ == "__main__":
    main()
