"""Tests for pure helpers in cld.docker."""

import os
from pathlib import Path

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
    stage_host_broker,
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

    def test_matches_via_container_mirror(self, monkeypatch):
        # Placeholder dirs live at the container mirror ($HOME/...) of a host
        # target; resolve translates cwd back to the host path before matching.
        monkeypatch.setenv("MASTER_MODE", "1")
        host_target = "/home/host/projects/foo"
        monkeypatch.setenv("MASTER_TARGETS", host_target)
        cfg = Config(host_home="/home/host")
        from pathlib import Path
        from cld.docker import CONTAINER_HOME
        mirror = Path(f"{CONTAINER_HOME}/projects/foo")
        assert resolve_master_target(mirror, cfg) == host_target
        assert resolve_master_target(mirror / "subdir", cfg) == host_target

    def test_unknown_cwd_errors(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MASTER_MODE", "1")
        monkeypatch.setenv("MASTER_TARGETS", "")
        elsewhere = tmp_path / "unrelated"
        elsewhere.mkdir()
        with pytest.raises(RuntimeError, match="not a registered target"):
            resolve_master_target(elsewhere, Config())


class TestEnsureImageNested:
    def test_trusts_host_image_when_present(self, monkeypatch):
        # Inside master (MASTER_MODE set) ensure_image must not build; it probes
        # the shared daemon and returns if the host-built image is present.
        monkeypatch.setenv("MASTER_MODE", "1")
        import cld.docker as docker_mod
        from pathlib import Path
        from unittest.mock import MagicMock, patch
        with patch.object(docker_mod.subprocess, "run") as run_mock:
            run_mock.return_value = MagicMock(stdout="deadbeef\n")
            result = docker_mod.ensure_image(
                "claude-devcontainer:latest",
                Path("/opt/cld/imgs/x/Dockerfile"),
                Path("/opt/cld"),
            )
        assert result == ""
        cmds = [c.args[0] for c in run_mock.call_args_list]
        assert all("build" not in cmd for cmd in cmds)  # never attempts a build
        assert any(cmd[:3] == ["docker", "images", "-q"] for cmd in cmds)

    def test_raises_when_host_image_missing(self, monkeypatch):
        monkeypatch.setenv("MASTER_MODE", "1")
        import cld.docker as docker_mod
        from pathlib import Path
        from unittest.mock import MagicMock, patch
        with patch.object(docker_mod.subprocess, "run") as run_mock:
            run_mock.return_value = MagicMock(stdout="")
            with pytest.raises(RuntimeError, match="cannot be built from inside a master"):
                docker_mod.ensure_image(
                    "missing:img",
                    Path("/opt/cld/imgs/x/Dockerfile"),
                    Path("/opt/cld"),
                )


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


class TestStageHostBroker:
    def test_no_key_is_noop(self):
        assert stage_host_broker(Config()) == []

    def test_missing_key_returns_empty(self, tmp_path):
        cfg = Config(host_broker_key=str(tmp_path / "nope"))
        assert stage_host_broker(cfg) == []

    def test_key_only_wires_gateway_and_endpoint(self, tmp_path):
        key = tmp_path / "broker_key"
        key.write_text("k")
        cfg = Config(host_broker_key=str(key), host_broker_endpoint="host.docker.internal:2222")
        args = stage_host_broker(cfg)
        assert args[:2] == ["--add-host", "host.docker.internal:host-gateway"]
        assert "-v" in args and f"{key}:/run/secrets/host-broker-key:ro" in args
        assert "-e" in args and "CLD_HOST_BROKER=host.docker.internal:2222" in args
        # No known_hosts mount when it isn't configured.
        assert not any("host-broker-known" in a for a in args)

    def test_known_hosts_mounted_when_present(self, tmp_path):
        key = tmp_path / "broker_key"; key.write_text("k")
        known = tmp_path / "known_hosts"; known.write_text("h")
        cfg = Config(host_broker_key=str(key), host_broker_known_hosts=str(known))
        args = stage_host_broker(cfg)
        assert f"{known}:/run/secrets/host-broker-known:ro" in args

    def test_endpoint_override_passed_through(self, tmp_path):
        key = tmp_path / "broker_key"; key.write_text("k")
        cfg = Config(host_broker_key=str(key), host_broker_endpoint="me@1.2.3.4:2200")
        args = stage_host_broker(cfg)
        assert "CLD_HOST_BROKER=me@1.2.3.4:2200" in args
