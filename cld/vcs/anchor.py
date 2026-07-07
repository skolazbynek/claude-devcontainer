"""Shared anchor-change helpers.

Two primitives that every cld subcommand uses to satisfy the contract
described in docs/design-anchor-change.md:

- ``resolve_anchor``     pin a (possibly symbolic) revision to a commit hash.
- ``assert_descendant``  verify a revision lies in the anchor's descendants.

Anchor staging (creating a child commit B of the resolved anchor A containing
`.cld-run/*`) lives in :mod:`cld.vcs.scratch`. The container ephemeral
workspace layout means no host-side workspace directory is ever created; jj
bookmarks + watchman snapshots against the origin's `.jj/repo/store` provide
persistence across `docker rm && docker run`.
"""

from cld.log import get_logger
from cld.vcs import VcsBackend

log = get_logger(__name__)


def resolve_anchor(vcs: VcsBackend, revision: str) -> str:
    """Pin the anchor revision to a concrete commit hash.

    Empty ``revision`` defaults to ``@`` (jj) or ``HEAD`` (git), respecting
    ``vcs.workspace_revision`` when invoked from a secondary workspace.
    """
    if not revision:
        revision = vcs.workspace_revision or ("@" if vcs.name == "jj" else "HEAD")
    return vcs.resolve_revision(revision)


def assert_descendant(vcs: VcsBackend, anchor_hash: str, candidate: str) -> None:
    """Raise RuntimeError if ``candidate`` does not have ``anchor_hash`` in its ancestry."""
    if vcs.name == "jj":
        result = vcs.run([
            "log", "-r", f"ancestors({candidate}) & {anchor_hash}",
            "--no-graph", "-T", "commit_id", "-n", "1",
        ])
        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError(
                f"anchor_violation: {candidate} is not a descendant of {anchor_hash}"
            )
    else:
        result = vcs.run(["merge-base", "--is-ancestor", anchor_hash, candidate])
        if result.returncode != 0:
            raise RuntimeError(
                f"anchor_violation: {candidate} is not a descendant of {anchor_hash}"
            )
