"""Layer 2: Docker integration tests against the real daemon."""

import os
import subprocess

import pytest

from cld.config import Config
from cld.docker import build_container_args, ensure_image, require_docker
from tests.conftest import skip_no_docker


pytestmark = [pytest.mark.integration, pytest.mark.docker]


@skip_no_docker
class TestRequireDocker:
    def test_passes_when_available(self):
        require_docker()


@skip_no_docker
class TestEnsureImage:
    def test_existing_image_is_noop(self, tmp_path):
        # Should not trigger a build for an already-existing image
        # Use a dummy dockerfile path since it shouldn't be touched
        ensure_image(
            "claude-devcontainer:latest",
            tmp_path / "nonexistent.Dockerfile",
            tmp_path,
        )

    def test_missing_image_would_build(self, tmp_path):
        # Verify the function detects missing images.
        # We don't actually build -- just confirm it tries (and fails on bad Dockerfile).
        with pytest.raises(subprocess.CalledProcessError):
            ensure_image(
                "test-nonexistent-image:never",
                tmp_path / "Dockerfile",
                tmp_path,
            )


@skip_no_docker
class TestBuildContainerArgs:
    def test_structure_has_required_flags(self, jj_repo):
        args = build_container_args(jj_repo.repo_root, "test-session", Config())
        assert "--rm" in args
        assert "--cap-drop=ALL" in args
        assert "--security-opt=no-new-privileges" in args

    def test_session_name_in_env(self, jj_repo):
        args = build_container_args(jj_repo.repo_root, "mysession", Config())
        env_pairs = [args[i+1] for i in range(len(args)-1) if args[i] == "-e"]
        assert "SESSION_NAME=mysession" in env_pairs

    def test_workspace_volume_mounted(self, jj_repo):
        args = build_container_args(jj_repo.repo_root, "test-session", Config())
        volume_args = [args[i+1] for i in range(len(args)-1) if args[i] == "-v"]
        assert any("/workspace/origin" in v for v in volume_args)

    def test_mysql_mount_when_configured(self, jj_repo, tmp_path):
        mysql_cnf = tmp_path / "mysql.cnf"
        mysql_cnf.write_text("[client]\nhost=localhost\n")
        cfg = Config(mysql_config=str(mysql_cnf))
        args = build_container_args(jj_repo.repo_root, "test-session", cfg)
        volume_args = [args[i+1] for i in range(len(args)-1) if args[i] == "-v"]
        assert any("mysql.cnf" in v for v in volume_args)

    def test_no_mysql_mount_without_config(self, jj_repo):
        args = build_container_args(jj_repo.repo_root, "test-session", Config())
        volume_args = [args[i+1] for i in range(len(args)-1) if args[i] == "-v"]
        assert not any("mysql.cnf" in v for v in volume_args)

    def test_interactive_mode_adds_it_flag(self, jj_repo):
        args = build_container_args(jj_repo.repo_root, "test-session", Config(), interactive=True)
        assert "-it" in args

    def test_non_interactive_mode_no_it_flag(self, jj_repo):
        args = build_container_args(jj_repo.repo_root, "test-session", Config())
        assert "-it" not in args


@skip_no_docker
class TestRunContainer:
    def test_detached_trivial_container(self):
        result = subprocess.run(
            ["docker", "run", "--rm", "--detach", "claude-devcontainer:latest",
             "echo", "hello"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        cid = result.stdout.strip()
        assert len(cid) >= 12
        # Wait for it to finish, then it auto-removes
        subprocess.run(
            ["docker", "wait", cid],
            capture_output=True, text=True, timeout=30,
        )

    def test_container_auto_removes(self):
        # Override entrypoint to skip VCS checks
        result = subprocess.run(
            ["docker", "run", "--rm", "--name", "test-auto-rm",
             "--entrypoint", "echo",
             "claude-devcontainer:latest", "hello"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        assert "hello" in result.stdout
        # Container should be gone (--rm)
        check = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=^test-auto-rm$", "--format", "{{.ID}}"],
            capture_output=True, text=True,
        )
        assert not check.stdout.strip()
