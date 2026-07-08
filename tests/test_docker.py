"""Tests for pure helpers in cld.docker."""

import os

import pytest

from cld.config import Config, _load_dotenv
from cld.docker import (
    agent_container_name,
    anchor_env_args,
    build_session_name,
    find_repo_root,
    in_master_container,
    resolve_master_target,
    stage_home_ro,
    to_host_path,
)


class TestBuildSessionName:
    def test_explicit_suffix(self):
        assert build_session_name("agent", "feature") == "agent_feature"

    def test_auto_suffix_is_hex(self):
        prefix, _, suffix = build_session_name("cld").partition("_")
        assert prefix == "cld"
        assert len(suffix) == 6 and all(c in "0123456789abcdef" for c in suffix)

    def test_auto_suffix_varies(self):
        # secrets.token_hex(3) -> 6 hex chars; collisions in 20 picks are astronomical
        assert len({build_session_name("x") for _ in range(20)}) > 1


class TestFindJjRoot:
    def test_finds_in_start_dir(self, tmp_path):
        (tmp_path / ".jj").mkdir()
        assert find_repo_root(tmp_path) == tmp_path

    def test_walks_up_from_nested(self, tmp_path):
        (tmp_path / ".jj").mkdir()
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert find_repo_root(nested) == tmp_path

    def test_workspace_origin_env_takes_priority(self, tmp_path, monkeypatch):
        origin = tmp_path / "origin"
        (origin / ".jj").mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        (elsewhere / ".jj").mkdir(parents=True)
        monkeypatch.setenv("WORKSPACE_ORIGIN", str(origin))
        assert find_repo_root(elsewhere) == origin

    def test_exits_when_not_found(self, tmp_path):
        with pytest.raises(SystemExit):
            find_repo_root(tmp_path)


class TestAgentContainerName:
    def test_no_sha_disambiguator(self, tmp_path):
        repo = tmp_path / "myrepo"
        assert agent_container_name(repo) == "cld_agent_myrepo"

    def test_deterministic(self, tmp_path):
        repo = tmp_path / "myrepo"
        assert agent_container_name(repo) == agent_container_name(repo)


class TestLoadDotenv:
    def test_loads_key_value(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FOO", raising=False)
        env = tmp_path / ".env"
        env.write_text("FOO=bar\n")
        _load_dotenv(env)
        assert os.environ["FOO"] == "bar"

    def test_ignores_comments_and_blanks(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BAZ", raising=False)
        env = tmp_path / ".env"
        env.write_text("# comment\n\n   \nBAZ=qux\n")
        _load_dotenv(env)
        assert os.environ["BAZ"] == "qux"

    def test_strips_whitespace(self, tmp_path, monkeypatch):
        monkeypatch.delenv("K", raising=False)
        env = tmp_path / ".env"
        env.write_text("  K  =  v  \n")
        _load_dotenv(env)
        assert os.environ["K"] == "v"

    def test_missing_file_is_noop(self, tmp_path):
        _load_dotenv(tmp_path / "nonexistent")


class TestToHostPath:
    def test_workspace_current_not_translated(self):
        # /workspace/current lives inside the container's ephemeral filesystem
        # and has no host equivalent; to_host_path leaves it alone.
        cfg = Config(host_project_dir="/host/proj")
        assert to_host_path("/workspace/current/file.py", cfg) == "/workspace/current/file.py"

    def test_translates_workspace_origin(self):
        cfg = Config(host_project_dir="/host/proj")
        assert to_host_path("/workspace/origin/.jj", cfg) == "/host/proj/.jj"

    def test_translates_home(self):
        from cld.docker import CONTAINER_HOME
        cfg = Config(host_home="/home/host")
        assert to_host_path(f"{CONTAINER_HOME}/.claude", cfg) == "/home/host/.claude"

    def test_no_env_no_translation(self):
        assert to_host_path("/anywhere/else", Config()) == "/anywhere/else"

    def test_non_matching_path_untouched(self):
        cfg = Config(host_project_dir="/host/proj")
        assert to_host_path("/unrelated/path", cfg) == "/unrelated/path"


class TestStageHomeRo:
    def test_missing_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert stage_home_ro(".missing", Config()) == []

    def test_existing_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".gitconfig").write_text("x")
        args = stage_home_ro(".gitconfig", Config())
        assert args[0] == "-v"
        assert args[1].endswith(":/tmp/host-config/.gitconfig:ro")
        assert str(tmp_path / ".gitconfig") in args[1]

    def test_existing_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".config" / "anthropic").mkdir(parents=True)
        args = stage_home_ro(".config/anthropic", Config())
        assert args[0] == "-v"
        assert args[1].endswith(":/tmp/host-config/.config/anthropic:ro")

    def test_nested_rel_preserved(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".local" / "state" / "nvim").mkdir(parents=True)
        args = stage_home_ro(".local/state/nvim", Config())
        assert args[1].endswith(":/tmp/host-config/.local/state/nvim:ro")

    def test_to_host_path_translation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".bashrc").write_text("x")
        cfg = Config(host_home="/host/home")
        # tmp_path stands in for $CONTAINER_HOME via HOME env; to_host_path
        # only rewrites paths starting with CONTAINER_HOME, which tmp_path does
        # not, so the host-translated string is just the resolved tmp path.
        args = stage_home_ro(".bashrc", cfg)
        assert args[1].startswith(str(tmp_path.resolve()) + "/.bashrc:")


class TestResolveMasterTarget:
    def test_errors_when_not_in_master(self, tmp_path):
        # clean_env fixture already unsets MASTER_MODE
        with pytest.raises(RuntimeError, match="not running inside a cld master"):
            resolve_master_target(tmp_path, Config())

    def test_own_repo_via_workspace_origin(self, monkeypatch):
        monkeypatch.setenv("MASTER_MODE", "1")
        cfg = Config(host_project_dir="/host/side/cld")
        # /workspace/current is master's ephemeral workspace path. Path.resolve
        # is lenient about non-existent paths so this works even on the host.
        from pathlib import Path
        assert resolve_master_target(Path("/workspace/current"), cfg) == "/host/side/cld"
        assert resolve_master_target(Path("/workspace/origin/sub"), cfg) == "/host/side/cld"

    def test_own_repo_errors_without_host_project_dir(self, monkeypatch):
        monkeypatch.setenv("MASTER_MODE", "1")
        from pathlib import Path
        with pytest.raises(RuntimeError, match="CLD_HOST_PROJECT_DIR is unset"):
            resolve_master_target(Path("/workspace/current"), Config())

    def test_matches_master_targets_entry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MASTER_MODE", "1")
        target = tmp_path / "projects" / "foo"
        (target / "subdir").mkdir(parents=True)
        monkeypatch.setenv("MASTER_TARGETS", f"{target}:{tmp_path}/other")
        assert resolve_master_target(target, Config()) == str(target)
        assert resolve_master_target(target / "subdir", Config()) == str(target)

    def test_unknown_cwd_errors(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MASTER_MODE", "1")
        monkeypatch.setenv("MASTER_TARGETS", "")
        elsewhere = tmp_path / "unrelated"
        elsewhere.mkdir()
        with pytest.raises(RuntimeError, match="not a registered target"):
            resolve_master_target(elsewhere, Config())


class TestAnchorEnvArgsMasterMode:
    def test_master_mode_emits_hint_and_scratch(self, monkeypatch):
        monkeypatch.setenv("MASTER_MODE", "1")
        args = anchor_env_args(Config(), "sess1", "myrev")
        # Args are alternating -e / KEY=VAL entries.
        values = args[1::2]
        assert "AGENT_REVISION_HINT=myrev" in values
        assert any(v.startswith("AGENT_SCRATCH=") for v in values)
        assert not any("AGENT_ANCHOR_HASH" in v for v in values)


class TestInMasterContainer:
    def test_true_when_env_set(self, monkeypatch):
        monkeypatch.setenv("MASTER_MODE", "1")
        assert in_master_container() is True

    def test_false_when_unset(self):
        assert in_master_container() is False
