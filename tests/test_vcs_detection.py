"""Layer 1b: VCS backend auto-detection tests."""

import subprocess
from unittest.mock import patch

import pytest

from cld.vcs.detect import get_backend
from cld.vcs.git import GitBackend
from cld.vcs.jj import JjBackend


pytestmark = pytest.mark.integration


class TestGetBackend:
    def test_prefers_jj_when_both_present(self, tmp_path):
        subprocess.run(
            ["jj", "git", "init"], cwd=tmp_path, check=True, capture_output=True,
        )
        # jj git init creates both .jj and .git
        assert (tmp_path / ".jj").is_dir()
        backend = get_backend(tmp_path)
        assert isinstance(backend, JjBackend)

    def test_falls_back_to_git(self, tmp_path):
        subprocess.run(
            ["git", "init"], cwd=tmp_path, check=True, capture_output=True,
        )
        with patch("cld.vcs.detect.shutil.which", side_effect=lambda x: None if x == "jj" else "/usr/bin/git"):
            backend = get_backend(tmp_path)
        assert isinstance(backend, GitBackend)

    def test_jj_dir_no_binary_falls_to_git(self, tmp_path):
        subprocess.run(
            ["jj", "git", "init"], cwd=tmp_path, check=True, capture_output=True,
        )
        with patch("cld.vcs.detect.shutil.which", side_effect=lambda x: None if x == "jj" else "/usr/bin/git"):
            backend = get_backend(tmp_path)
        assert isinstance(backend, GitBackend)

    def test_workspace_origin_env_overrides(self, tmp_path, monkeypatch):
        origin = tmp_path / "origin"
        origin.mkdir()
        subprocess.run(
            ["jj", "git", "init"], cwd=origin, check=True, capture_output=True,
        )
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.setenv("WORKSPACE_ORIGIN", str(origin))
        backend = get_backend(elsewhere)
        assert isinstance(backend, JjBackend)
        assert backend.repo_root == origin

    def test_no_repo_raises(self, tmp_path):
        with pytest.raises(RuntimeError, match="No VCS repository found"):
            get_backend(tmp_path)

    def test_walks_up_from_nested(self, tmp_path):
        subprocess.run(
            ["jj", "git", "init"], cwd=tmp_path, check=True, capture_output=True,
        )
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        backend = get_backend(nested)
        assert backend.repo_root == tmp_path

    def test_main_workspace_path_equals_repo_root(self, tmp_path):
        subprocess.run(
            ["jj", "git", "init"], cwd=tmp_path, check=True, capture_output=True,
        )
        backend = get_backend(tmp_path)
        assert backend.workspace_path == backend.repo_root

    def test_jj_secondary_workspace_sets_workspace_path(self, tmp_path):
        main_root = tmp_path / "main"
        main_root.mkdir()
        subprocess.run(
            ["jj", "git", "init"], cwd=main_root, check=True, capture_output=True,
        )
        subprocess.run(
            ["jj", "commit", "-m", "seed"], cwd=main_root, check=True, capture_output=True,
        )
        ws_path = tmp_path / "secondary"
        subprocess.run(
            ["jj", "workspace", "add", "--name", "secondary", str(ws_path)],
            cwd=main_root, check=True, capture_output=True,
        )
        backend = get_backend(ws_path)
        assert isinstance(backend, JjBackend)
        assert backend.repo_root == main_root
        assert backend.workspace_path == ws_path

    def test_git_worktree_sets_workspace_path(self, tmp_path):
        main_root = tmp_path / "main"
        main_root.mkdir()
        subprocess.run(["git", "init"], cwd=main_root, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "t@t.com"],
            cwd=main_root, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"],
            cwd=main_root, check=True, capture_output=True,
        )
        (main_root / "f.txt").write_text("x\n")
        subprocess.run(["git", "add", "-A"], cwd=main_root, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "seed"], cwd=main_root, check=True, capture_output=True,
        )
        wt_path = tmp_path / "worktree"
        subprocess.run(
            ["git", "worktree", "add", "-b", "wt-branch", str(wt_path)],
            cwd=main_root, check=True, capture_output=True,
        )
        with patch("cld.vcs.detect.shutil.which", side_effect=lambda x: None if x == "jj" else "/usr/bin/git"):
            backend = get_backend(wt_path)
        assert isinstance(backend, GitBackend)
        assert backend.repo_root == main_root
        assert backend.workspace_path == wt_path
