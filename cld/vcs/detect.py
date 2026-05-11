"""VCS backend auto-detection -- prefer jujutsu, fall back to git."""

import os
import shutil
from pathlib import Path

from cld.vcs.base import VcsBackend


def get_backend(start: Path | None = None) -> VcsBackend:
    """Detect the repository type and available VCS tool, return the right backend.

    Detection order:
    1. If inside a container (``WORKSPACE_ORIGIN`` set), use that directory.
    2. Walk up from *start* (or cwd) looking for ``.jj/`` or ``.git/``.
    3. If ``.jj/`` found AND ``jj`` is installed -> JjBackend.
    4. If ``.git/`` found AND ``git`` is installed -> GitBackend.
       (This also covers jj repos with a git backend when jj is not installed.)
    5. Otherwise raise RuntimeError.
    """
    from cld.vcs.git import GitBackend
    from cld.vcs.jj import JjBackend

    # Inside a container, prefer the bind-mounted origin directory
    origin = os.environ.get("WORKSPACE_ORIGIN", "")
    if origin:
        origin_path = Path(origin)
        if (origin_path / ".jj").is_dir() and shutil.which("jj"):
            return JjBackend(origin_path)
        if _has_git_dir(origin_path) and shutil.which("git"):
            return GitBackend(origin_path)

    d = start or Path.cwd()
    jj_root = None
    git_root = None

    # Walk up once, recording the first .jj and .git roots we find
    cur = d
    while cur != cur.parent:
        if jj_root is None and (cur / ".jj").is_dir():
            jj_root = cur
        if git_root is None and _has_git_dir(cur):
            git_root = cur
        if jj_root and git_root:
            break
        cur = cur.parent

    # Prefer jj if both repo marker and binary exist
    if jj_root and shutil.which("jj"):
        resolved = JjBackend._resolve_secondary_workspace(jj_root)
        workspace_rev = ""
        if resolved != jj_root:
            name = JjBackend._current_workspace_name(jj_root)
            workspace_rev = f"{name}@" if name else ""
        return JjBackend(resolved, workspace_rev)

    # Fall back to git
    if git_root and shutil.which("git"):
        resolved = GitBackend._resolve_worktree_root(git_root)
        workspace_rev = ""
        if resolved != git_root:
            workspace_rev = GitBackend._current_worktree_branch(git_root)
        return GitBackend(resolved, workspace_rev)

    # Also fall back to git if we found .jj but not the jj binary,
    # and the repo has a .git (jj with git backend)
    if jj_root and not shutil.which("jj"):
        git_in_jj = jj_root if _has_git_dir(jj_root) else None
        if git_in_jj and shutil.which("git"):
            return GitBackend(git_in_jj)

    raise RuntimeError(
        "No VCS repository found. "
        "Expected a jujutsu (.jj/) or git (.git/) repository in the directory tree."
    )


def _has_git_dir(path: Path) -> bool:
    """Check whether *path* contains a .git directory or file (worktree pointer)."""
    git_path = path / ".git"
    return git_path.is_dir() or git_path.is_file()
