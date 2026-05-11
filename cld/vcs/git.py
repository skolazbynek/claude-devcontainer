"""Git backend implementation -- fallback when jujutsu is not available."""

import subprocess
from pathlib import Path

from cld.vcs.base import VcsBackend


class GitBackend(VcsBackend):
    """VCS backend that delegates all operations to the ``git`` CLI.

    Used as a fallback when jujutsu is not installed. Maps jj-style workspace
    isolation to git worktrees, bookmarks to branches, and revsets to standard
    git revision syntax.
    """

    @property
    def name(self) -> str:
        return "git"

    @property
    def dir_name(self) -> str:
        return ".git"

    # -- repository discovery -------------------------------------------------

    @classmethod
    def detect_root(cls, start: Path | None = None) -> Path | None:
        """Walk upward from *start* looking for a ``.git/`` directory or file."""
        d = start or Path.cwd()
        while d != d.parent:
            git_path = d / ".git"
            if git_path.is_dir() or git_path.is_file():
                return cls._resolve_worktree_root(d)
            d = d.parent
        return None

    @staticmethod
    def _resolve_worktree_root(path: Path) -> Path:
        """If path is a git worktree, resolve to the main repository root."""
        if not (path / ".git").is_file():
            return path
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse",
             "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return path
        return Path(result.stdout.strip()).parent

    @staticmethod
    def _current_worktree_branch(path: Path) -> str:
        """Return the branch name checked out in the worktree at *path*."""
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return ""
        branch = result.stdout.strip()
        return branch if branch != "HEAD" else ""

    # -- helpers --------------------------------------------------------------

    def _run_git(self, args: list[str], **kwargs) -> subprocess.CompletedProcess:
        """Run a git command against this repository."""
        return self.run(args, **kwargs)

    # -- workspace / worktree isolation ---------------------------------------

    def create_workspace(self, name: str, path: str, revision: str = "") -> str:
        """Create a git worktree with a new branch *name* at *revision*.

        Equivalent to jj's ``workspace add``. The branch is created automatically
        by ``git worktree add -b``.
        """
        cmd = ["worktree", "add", "-b", name, path]
        if revision:
            cmd.append(revision)
        result = self._run_git(cmd)
        return result.stdout + result.stderr

    def forget_workspace(self, name: str, path: str = "") -> str:
        """Remove a git worktree and optionally delete the tracking branch.

        *path* must be provided for git (unlike jj where name suffices).
        """
        output = ""
        if path:
            result = self._run_git(["worktree", "remove", "--force", path])
            output = result.stdout + result.stderr
        # Also prune stale worktree entries
        self._run_git(["worktree", "prune"])
        return output

    # -- branch management ----------------------------------------------------

    def create_branch(self, name: str, revision: str = "") -> str:
        """Create a git branch at *revision* (default: HEAD)."""
        cmd = ["branch", name]
        if revision:
            cmd.append(revision)
        return self._run_git(cmd).stdout

    def set_branch(self, name: str, revision: str) -> str:
        """Force-update a git branch to point at *revision*."""
        return self._run_git(["branch", "-f", name, revision]).stdout

    def delete_branch(self, name: str) -> str:
        """Delete a git branch (force-delete to handle unmerged branches)."""
        return self._run_git(["branch", "-D", name]).stdout

    def list_branches(self) -> str:
        """List all git branches."""
        result = self._run_git(["branch", "-a"])
        return result.stdout if result.returncode == 0 else f"Error: {result.stderr.strip()}"

    # -- change creation and manipulation -------------------------------------

    def new_change(self, revision: str = "") -> str:
        """Check out *revision*, positioning HEAD for new work.

        In a worktree context this is typically a no-op since ``create_workspace``
        already positions the worktree at the right revision.
        """
        if not revision:
            return ""
        result = self._run_git(["checkout", revision])
        return result.stdout + result.stderr

    def commit(self, message: str) -> str:
        """Stage all changes and commit.

        Unlike jj (which auto-tracks), git requires an explicit ``add -A`` step.
        """
        self._run_git(["add", "-A"])
        result = self._run_git(["commit", "-m", message])
        return result.stdout + result.stderr

    def describe(self, revision: str, message: str) -> str:
        """Rewrite the commit message of *revision* using git plumbing.

        If *revision* is HEAD or a branch whose tip is reachable, this uses
        ``git commit-tree`` to create a replacement commit and force-updates
        the branch. This avoids needing a worktree checkout.
        """
        # Resolve the revision to a concrete commit
        rev_result = self._run_git(["rev-parse", revision])
        if rev_result.returncode != 0:
            return f"Error: {rev_result.stderr.strip()}"
        commit_hash = rev_result.stdout.strip()

        # Get the tree of the commit
        tree_result = self._run_git(["rev-parse", f"{commit_hash}^{{tree}}"])
        if tree_result.returncode != 0:
            return f"Error: {tree_result.stderr.strip()}"
        tree_hash = tree_result.stdout.strip()

        # Get parent(s)
        parent_result = self._run_git(["rev-parse", f"{commit_hash}^"])
        parents = []
        if parent_result.returncode == 0:
            parents = ["-p", parent_result.stdout.strip()]

        # Create new commit with same tree/parents but new message
        cmd = ["commit-tree", tree_hash] + parents + ["-m", message]
        new_commit_result = self._run_git(cmd)
        if new_commit_result.returncode != 0:
            return f"Error: {new_commit_result.stderr.strip()}"
        new_hash = new_commit_result.stdout.strip()

        # If revision is a branch name, update it; otherwise try to update HEAD
        branch_check = self._run_git(["rev-parse", "--verify", f"refs/heads/{revision}"])
        if branch_check.returncode == 0:
            self._run_git(["branch", "-f", revision, new_hash])
        else:
            # Revision might be HEAD or a raw hash -- update HEAD if it matches
            head_result = self._run_git(["rev-parse", "HEAD"])
            if head_result.returncode == 0 and head_result.stdout.strip() == commit_hash:
                self._run_git(["reset", "--soft", new_hash])

        return new_hash

    def squash(self, from_rev: str, into_rev: str) -> str:
        """Squash changes from *from_rev* into *into_rev* via cherry-pick and amend."""
        original = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
        self._run_git(["checkout", into_rev])
        self._run_git(["cherry-pick", "--no-commit", from_rev])
        result = self._run_git(["commit", "--amend", "--no-edit"])
        if original and original != "HEAD":
            self._run_git(["checkout", original])
        return result.stdout + result.stderr

    # -- diff -----------------------------------------------------------------

    def diff(self, revision: str = "", *, stat: bool = False) -> str:
        """Show the diff for a revision or the working tree.

        Without *revision*: working tree vs HEAD.
        With *revision*: changes introduced by that commit (parent..commit).
        """
        if not revision:
            cmd = ["diff", "HEAD"]
        else:
            parent_check = self._run_git(["rev-parse", f"{revision}~1"])
            base = f"{revision}~1" if parent_check.returncode == 0 else "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
            cmd = ["diff", f"{base}..{revision}"]
        if stat:
            cmd.append("--stat")
        result = self._run_git(cmd)
        if result.returncode != 0:
            return f"Error: {result.stderr.strip()}"
        return result.stdout

    def diff_between(self, from_rev: str, to_rev: str) -> str:
        """Generate a unified diff between two git revisions."""
        result = self._run_git(["diff", f"{from_rev}..{to_rev}"])
        if result.returncode != 0:
            return f"Error: {result.stderr.strip()}"
        return result.stdout

    def has_changes(self) -> bool:
        """Check whether the working tree has uncommitted changes."""
        result = self._run_git(["status", "--porcelain"])
        return bool(result.stdout.strip())

    def diff_stat_summary(self) -> tuple[int, str]:
        """Parse ``git diff --stat`` output to extract file count and names.

        Examines both staged and unstaged changes against HEAD.
        """
        result = self._run_git(["diff", "HEAD", "--stat"])
        if result.returncode != 0:
            return 0, ""
        lines = [l for l in result.stdout.strip().splitlines() if "|" in l]
        names = [l.split("|")[0].strip() for l in lines]
        return len(names), ", ".join(names)

    # -- file access ----------------------------------------------------------

    def file_show(self, revision: str, path: str) -> str | None:
        """Read a file from a git revision via ``git show <rev>:<path>``."""
        result = self._run_git(["show", f"{revision}:{path}"])
        if result.returncode != 0:
            return None
        return result.stdout

    # -- history / log --------------------------------------------------------

    def log(self, revision: str = "", template: str = "") -> str:
        """Run ``git log`` for a revision.

        *template* is treated as a git ``--format`` string if provided.
        Defaults to a compact one-line-per-commit format.
        """
        cmd = ["log"]
        if revision:
            cmd.append(revision)
        if template:
            cmd += [f"--format={template}"]
        else:
            cmd += ["--oneline", "-20"]
        result = self._run_git(cmd)
        if result.returncode != 0:
            return f"Error: {result.stderr.strip()}"
        return result.stdout

    def resolve_revision(self, revision: str) -> str:
        """Resolve a git revision spec to a full commit SHA."""
        result = self._run_git(["rev-parse", revision])
        if result.returncode != 0:
            raise RuntimeError(f"Failed to resolve revision '{revision}': {result.stderr.strip()}")
        return result.stdout.strip()

    def get_description(self, revision: str) -> str:
        """Get the commit message of a git revision."""
        result = self._run_git(["log", "--format=%B", "-1", revision])
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    # -- merge-base -----------------------------------------------------------

    def fork_point(self, branch_a: str, branch_b: str) -> str:
        """Find the merge-base (common ancestor) of two git branches."""
        result = self._run_git(["merge-base", branch_a, branch_b])
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to find merge-base of '{branch_a}' and '{branch_b}': "
                f"{result.stderr.strip()}"
            )
        return result.stdout.strip()
