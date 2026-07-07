"""Unit tests for cld.vcs.scratch.stage_anchor_with_scratch."""

import subprocess
from pathlib import Path

import pytest

from cld.vcs.git import GitBackend
from cld.vcs.jj import JjBackend
from cld.vcs.scratch import SCRATCH_DIR, stage_anchor_with_scratch


def _add_second_commit(path: Path) -> str:
    """Advance the seed repo by one commit; return the parent commit hash."""
    (path / "a.txt").write_text("hello\n")
    subprocess.run(
        ["jj", "commit", "-m", "add a.txt"],
        cwd=path, check=True, capture_output=True,
    )
    # Return the parent of @ (the commit we just made becomes @-).
    result = subprocess.run(
        ["jj", "log", "-r", "@-", "--no-graph", "-T", "commit_id"],
        cwd=path, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


class TestStageAnchorWithScratch:
    def test_produces_child_of_anchor_with_scratch(self, jj_repo, tmp_path):
        anchor = _add_second_commit(jj_repo.repo_root)
        b_hash = stage_anchor_with_scratch(
            jj_repo, anchor, "sess_a",
            {"task.md": b"# task body\n", "sub/x.txt": b"y\n"},
        )
        # B is a real child of A.
        parents = subprocess.run(
            ["jj", "log", "-r", b_hash, "--no-graph", "-T", 'parents.map(|p| p.commit_id()).join(",")'],
            cwd=jj_repo.repo_root, check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert anchor in parents
        # B contains the scratch files.
        summary = subprocess.run(
            ["jj", "log", "-r", b_hash, "--no-graph", "--summary", "-T", '""'],
            cwd=jj_repo.repo_root, check=True, capture_output=True, text=True,
        ).stdout
        assert f"{SCRATCH_DIR}/task.md" in summary
        assert f"{SCRATCH_DIR}/sub/x.txt" in summary

    def test_user_working_copy_scratch_removed(self, jj_repo):
        anchor = _add_second_commit(jj_repo.repo_root)
        stage_anchor_with_scratch(
            jj_repo, anchor, "sess_b", {"task.md": b"hi\n"},
        )
        # After the split, the user's @ should not contain .cld-run/ anymore.
        summary = subprocess.run(
            ["jj", "log", "-r", "@", "--no-graph", "--summary", "-T", '""'],
            cwd=jj_repo.repo_root, check=True, capture_output=True, text=True,
        ).stdout
        assert SCRATCH_DIR not in summary

    def test_git_backend_raises_not_implemented(self, git_repo):
        with pytest.raises(NotImplementedError):
            stage_anchor_with_scratch(git_repo, "HEAD", "s", {"x": b"y"})

    def test_ignored_scratch_dir_raises(self, jj_repo):
        anchor = _add_second_commit(jj_repo.repo_root)
        (jj_repo.repo_root / ".gitignore").write_text(f"{SCRATCH_DIR}/\n")
        with pytest.raises(RuntimeError, match=SCRATCH_DIR):
            stage_anchor_with_scratch(
                jj_repo, anchor, "sess_c", {"task.md": b"x\n"},
            )

    def test_failure_restores_working_copy(self, jj_repo, monkeypatch):
        anchor = _add_second_commit(jj_repo.repo_root)
        pre_hash = subprocess.run(
            ["jj", "log", "-r", "@", "--no-graph", "-T", "commit_id"],
            cwd=jj_repo.repo_root, check=True, capture_output=True, text=True,
        ).stdout.strip()

        # Force the split to fail by passing an unresolvable anchor.
        with pytest.raises(RuntimeError):
            stage_anchor_with_scratch(
                jj_repo, "nonexistent000000", "sess_d", {"task.md": b"x\n"},
            )

        # Working copy is back where we left it, and the scratch dir is gone.
        post_hash = subprocess.run(
            ["jj", "log", "-r", "@", "--no-graph", "-T", "commit_id"],
            cwd=jj_repo.repo_root, check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert post_hash == pre_hash
        assert not (jj_repo.repo_root / SCRATCH_DIR).exists()
