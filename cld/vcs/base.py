"""Abstract base class defining all VCS operations the application needs."""

import subprocess
from abc import ABC, abstractmethod
from pathlib import Path


class VcsBackend(ABC):
    """Backend-agnostic interface for version control operations.

    Concrete implementations (JjBackend, GitBackend) translate these high-level
    operations into VCS-specific commands. All methods that execute commands use
    ``self.repo_root`` as the working directory.
    """

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for the backend ('jj' or 'git')."""

    @property
    @abstractmethod
    def dir_name(self) -> str:
        """Name of the metadata directory at the repo root ('.jj' or '.git')."""

    # -- low-level -----------------------------------------------------------

    def run(self, args: list[str], **kwargs) -> subprocess.CompletedProcess:
        """Execute a raw VCS command rooted at repo_root."""
        defaults = {"capture_output": True, "text": True, "cwd": str(self.repo_root)}
        defaults.update(kwargs)
        return subprocess.run([self.name] + args, **defaults)

    # -- repository discovery -------------------------------------------------

    @classmethod
    @abstractmethod
    def detect_root(cls, start: Path | None = None) -> Path | None:
        """Walk upward from *start* looking for a repo root. Return None if not found."""

    # -- workspace / worktree isolation ---------------------------------------

    @abstractmethod
    def create_workspace(self, name: str, path: str, revision: str = "") -> str:
        """Create an isolated workspace (jj workspace / git worktree) at *path*.

        *name* becomes the branch/bookmark tracking this workspace.
        *revision* is the starting point (default: current HEAD / @).
        Returns command output.
        """

    @abstractmethod
    def forget_workspace(self, name: str, path: str = "") -> str:
        """Remove a previously created workspace/worktree.

        For jj, *name* suffices. For git, *path* to the worktree is needed.
        Returns command output.
        """

    # -- branch / bookmark management -----------------------------------------

    @abstractmethod
    def create_branch(self, name: str, revision: str = "") -> str:
        """Create a named branch/bookmark pointing at *revision* (default: current)."""

    @abstractmethod
    def set_branch(self, name: str, revision: str) -> str:
        """Force-update a branch/bookmark to point at *revision*."""

    @abstractmethod
    def delete_branch(self, name: str) -> str:
        """Delete a branch/bookmark."""

    @abstractmethod
    def list_branches(self) -> str:
        """List all branches/bookmarks. Returns human-readable output."""

    # -- change creation and manipulation -------------------------------------

    @abstractmethod
    def new_change(self, revision: str = "") -> str:
        """Create a new empty change on top of *revision*.

        jj: ``jj new <rev>``
        git: ``git checkout <rev>`` (in a worktree context, already positioned).
        """

    @abstractmethod
    def commit(self, message: str) -> str:
        """Snapshot all pending changes into a commit with *message*.

        jj: ``jj commit -m``
        git: ``git add -A && git commit -m``
        """

    @abstractmethod
    def describe(self, revision: str, message: str) -> str:
        """Rewrite the commit message of *revision*.

        jj: ``jj describe -r <rev> -m``
        git: uses commit-tree plumbing to rewrite the branch tip.
        """

    @abstractmethod
    def squash(self, from_rev: str, into_rev: str) -> str:
        """Squash changes from *from_rev* into *into_rev*.

        jj: ``jj squash --from <a> --into <b>``
        git: ``git reset --soft HEAD~1 && git commit --amend --no-edit``
        """

    # -- diff -----------------------------------------------------------------

    @abstractmethod
    def diff(self, revision: str = "", *, stat: bool = False) -> str:
        """Show the diff introduced by *revision* (default: working copy).

        Returns unified diff text, or stat summary if *stat* is True.
        """

    @abstractmethod
    def diff_between(self, from_rev: str, to_rev: str) -> str:
        """Generate a unified diff between two revisions in git-compatible format."""

    @abstractmethod
    def has_changes(self) -> bool:
        """Return True if the working copy has uncommitted changes."""

    @abstractmethod
    def diff_stat_summary(self) -> tuple[int, str]:
        """Return (file_count, comma_separated_filenames) for the current change."""

    # -- file access ----------------------------------------------------------

    @abstractmethod
    def file_show(self, revision: str, path: str) -> str | None:
        """Read file contents from a specific revision. Return None on failure."""

    # -- history / log --------------------------------------------------------

    @abstractmethod
    def log(self, revision: str = "", template: str = "") -> str:
        """Show log output for *revision*. *template* is backend-specific formatting."""

    @abstractmethod
    def resolve_revision(self, revision: str) -> str:
        """Resolve a symbolic revision to a concrete commit hash."""

    @abstractmethod
    def get_description(self, revision: str) -> str:
        """Return the commit message of *revision*."""

    # -- merge-base -----------------------------------------------------------

    @abstractmethod
    def fork_point(self, branch_a: str, branch_b: str) -> str:
        """Find the common ancestor (fork point / merge-base) of two branches."""
