"""Jujutsu (jj) backend implementation."""

import subprocess
from pathlib import Path

from cld.vcs.base import VcsBackend


class JjBackend(VcsBackend):
    """VCS backend that delegates all operations to the ``jj`` CLI.

    This is the preferred backend. Jujutsu provides native workspace isolation,
    automatic change tracking, and first-class conflict recording.
    """

    @property
    def name(self) -> str:
        return "jj"

    @property
    def dir_name(self) -> str:
        return ".jj"

    # -- repository discovery -------------------------------------------------

    @classmethod
    def detect_root(cls, start: Path | None = None) -> Path | None:
        """Walk upward from *start* looking for a ``.jj/`` directory."""
        d = start or Path.cwd()
        while d != d.parent:
            if (d / ".jj").is_dir():
                return cls._resolve_secondary_workspace(d)
            d = d.parent
        return None

    @staticmethod
    def _resolve_secondary_workspace(path: Path) -> Path:
        """If path is a jj secondary workspace, follow .jj/repo pointer to the main root."""
        repo_file = path / ".jj" / "repo"
        if not repo_file.is_file():
            return path
        store_path = Path(repo_file.read_text().strip())
        if not store_path.is_absolute():
            store_path = (repo_file.parent / store_path).resolve()
        return store_path.parent.parent

    @staticmethod
    def _current_workspace_name(path: Path) -> str:
        """Return the jj workspace name for the working directory at *path*."""
        result = subprocess.run(
            ["jj", "--no-pager", "workspace", "list", "--color=never",
             "--ignore-working-copy", "-T",
             'if(target.current_working_copy(), name ++ "\\n")'],
            capture_output=True, text=True, cwd=str(path),
        )
        if result.returncode != 0:
            return ""
        lines = result.stdout.strip().splitlines()
        return lines[0] if lines else ""

    # -- workspace isolation --------------------------------------------------

    def create_workspace(self, name: str, path: str, revision: str = "") -> str:
        """Create a jj workspace via ``jj workspace add``."""
        cmd = ["workspace", "add", "--name", name]
        if revision:
            cmd += ["-r", revision]
        cmd.append(path)
        return self.run(cmd).stdout

    def forget_workspace(self, name: str, path: str = "") -> str:
        """Forget a jj workspace via ``jj workspace forget``."""
        cmd = ["workspace", "forget"]
        if name:
            cmd.append(name)
        return self.run(cmd).stdout

    # -- branch / bookmark management -----------------------------------------

    def create_branch(self, name: str, revision: str = "") -> str:
        """Create a jj bookmark at *revision* (default: current change)."""
        cmd = ["bookmark", "create", name]
        if revision:
            cmd += ["-r", revision]
        return self.run(cmd).stdout

    def set_branch(self, name: str, revision: str) -> str:
        """Move an existing jj bookmark to *revision*."""
        return self.run(["bookmark", "set", name, "-r", revision]).stdout

    def delete_branch(self, name: str) -> str:
        """Delete a jj bookmark."""
        return self.run(["bookmark", "delete", name]).stdout

    def list_branches(self) -> str:
        """List all jj bookmarks."""
        result = self.run(["bookmark", "list"])
        return result.stdout if result.returncode == 0 else f"Error: {result.stderr.strip()}"

    # -- change creation and manipulation -------------------------------------

    def new_change(self, revision: str = "") -> str:
        """Create a new empty jj change on top of *revision*."""
        cmd = ["new"]
        if revision:
            cmd.append(revision)
        return self.run(cmd).stdout

    def commit(self, message: str) -> str:
        """Commit the current jj working copy."""
        return self.run(["commit", "-m", message]).stdout

    def describe(self, revision: str, message: str) -> str:
        """Rewrite the description of a jj change."""
        return self.run(["describe", "-r", revision, "-m", message]).stdout

    def squash(self, from_rev: str, into_rev: str) -> str:
        """Squash one jj change into another."""
        return self.run(["squash", "--from", from_rev, "--into", into_rev]).stdout

    # -- diff -----------------------------------------------------------------

    def diff(self, revision: str = "", *, stat: bool = False) -> str:
        """Show the diff of a jj change (default: working copy)."""
        cmd = ["diff"]
        if revision:
            cmd += ["-r", revision]
        if stat:
            cmd.append("--stat")
        result = self.run(cmd)
        if result.returncode != 0:
            return f"Error: {result.stderr.strip()}"
        return result.stdout

    def diff_between(self, from_rev: str, to_rev: str) -> str:
        """Generate a git-format diff between two jj revisions."""
        result = self.run(["diff", "--from", from_rev, "--to", to_rev, "--git"])
        if result.returncode != 0:
            return f"Error: {result.stderr.strip()}"
        return result.stdout

    def has_changes(self) -> bool:
        """Check whether the jj working copy has uncommitted changes."""
        result = self.run(["diff", "--stat"])
        return any("|" in line for line in result.stdout.splitlines())

    def diff_stat_summary(self) -> tuple[int, str]:
        """Parse ``jj diff --stat`` output to extract file count and names."""
        result = self.run(["diff", "--stat", "--no-pager"])
        if result.returncode != 0:
            return 0, ""
        lines = [l for l in result.stdout.strip().splitlines() if "|" in l]
        names = [l.split("|")[0].strip() for l in lines]
        return len(names), ", ".join(names)

    # -- file access ----------------------------------------------------------

    def file_show(self, revision: str, path: str) -> str | None:
        """Read a file from a jj revision via ``jj file show``."""
        result = self.run(["file", "show", "-r", revision, path])
        if result.returncode != 0:
            return None
        return result.stdout

    # -- history / log --------------------------------------------------------

    def log(self, revision: str = "", template: str = "") -> str:
        """Run ``jj log`` with optional revset and template."""
        cmd = ["log"]
        if revision:
            cmd += ["-r", revision]
        if template:
            cmd += ["-T", template]
        result = self.run(cmd)
        if result.returncode != 0:
            return f"Error: {result.stderr.strip()}"
        return result.stdout

    def resolve_revision(self, revision: str) -> str:
        """Resolve a jj revset to a concrete commit ID."""
        result = self.run(["log", "-r", revision, "--no-graph", "-T", "commit_id", "-n", "1"])
        if result.returncode != 0:
            raise RuntimeError(f"Failed to resolve revision '{revision}': {result.stderr.strip()}")
        return result.stdout.strip()

    def get_description(self, revision: str) -> str:
        """Get the description (commit message) of a jj change."""
        result = self.run(["log", "-r", revision, "--no-graph", "-T", "description"])
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    # -- merge-base -----------------------------------------------------------

    def fork_point(self, branch_a: str, branch_b: str) -> str:
        """Compute the fork point of two branches using jj's revset algebra."""
        revset = f"fork_point({branch_a} | {branch_b})"
        return self.resolve_revision(revset)
