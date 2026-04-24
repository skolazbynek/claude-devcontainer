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
