"""Shared anchor-change helpers.

Three primitives that every cld subcommand uses to satisfy the contract
described in docs/design-anchor-change.md:

- ``resolve_anchor``      pin a (possibly symbolic) revision to a commit hash.
- ``create_editable_root``  create a workspace whose @ is an empty child of
                            that hash, with a named branch pointing at @.
- ``assert_descendant``   verify a revision lies in the anchor's descendants.

The path-pointer rewrites (jj's ``.jj/repo``, git's ``.git`` worktree file) are
done here because they are a property of "host-side workspace created for
container use", independent of which command consumes the workspace.
"""

from pathlib import Path

from cld.docker import WORKSPACE_BASE
from cld.log import get_logger
from cld.vcs import VcsBackend
from cld.vcs.jj import JjBackend

log = get_logger(__name__)


def resolve_anchor(vcs: VcsBackend, revision: str) -> str:
    """Pin the anchor revision to a concrete commit hash.

    Empty ``revision`` defaults to ``@`` (jj) or ``HEAD`` (git), respecting
    ``vcs.workspace_revision`` when invoked from a secondary workspace.
    """
    if not revision:
        revision = vcs.workspace_revision or ("@" if vcs.name == "jj" else "HEAD")
    return vcs.resolve_revision(revision)


def create_editable_root(
    vcs: VcsBackend,
    anchor_hash: str,
    workspace_path: Path,
    branch: str,
) -> None:
    """Create ``workspace_path`` as an empty descendant of ``anchor_hash``.

    Postcondition: ``workspace_path``'s ``@`` is empty, ``branch`` points at
    ``@``, and ``@``'s only parent is ``anchor_hash``.

    Rewrites the workspace's internal repo pointer to a container-visible
    path so the same directory works when bind-mounted into a container.
    """
    workspace_path.parent.mkdir(parents=True, exist_ok=True)
    if workspace_path.exists():
        raise RuntimeError(f"Workspace path already exists: {workspace_path}")

    log.debug(
        "create_editable_root: vcs=%s branch=%s path=%s anchor=%s",
        vcs.name, branch, workspace_path, anchor_hash[:12],
    )
    out = vcs.create_workspace(branch, str(workspace_path), anchor_hash)
    log.debug("create_workspace output: %s", out.strip() if out else "")

    if vcs.name == "jj":
        # jj workspace add creates an empty child but does not place a bookmark;
        # use a per-workspace backend to point the bookmark at this workspace's @.
        ws_vcs = JjBackend(repo_root=vcs.repo_root, workspace_path=workspace_path)
        ws_vcs.create_branch(branch)
        # Rewrite the workspace's .jj/repo pointer to the in-container path.
        repo_pointer = workspace_path / ".jj" / "repo"
        repo_pointer.write_text(f"{WORKSPACE_BASE}/origin/.jj/repo")
    else:
        # git worktree add -b already created the branch at anchor_hash.
        # Add an explicit empty commit so the editable root is a real child.
        ws_vcs = type(vcs)(
            repo_root=vcs.repo_root, workspace_path=workspace_path,
        )
        ws_vcs.run(["commit", "--allow-empty", "-m", "cld: editable root"])
        # Rewrite the .git pointer to the in-container path.
        dotgit = workspace_path / ".git"
        if dotgit.is_file():
            content = dotgit.read_text().strip()
            if content.startswith("gitdir:"):
                abs_target = Path(content.split(":", 1)[1].strip())
                try:
                    rel_to_repo = abs_target.relative_to(vcs.repo_root)
                except ValueError:
                    rel_to_repo = None
                if rel_to_repo is not None:
                    dotgit.write_text(f"gitdir: {WORKSPACE_BASE}/origin/{rel_to_repo}\n")


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
