"""Tests for pure helpers in cld.docker."""

import os

import pytest

from cld.docker import (
    _to_host_path,
    build_session_name,
    find_jj_root,
    load_dotenv,
    mount_home_path,
)


class TestBuildSessionName:
    def test_explicit_suffix(self):
        assert build_session_name("agent", "feature") == "agent_feature"

    def test_auto_suffix_is_5_digits(self):
        prefix, _, suffix = build_session_name("cld").partition("_")
        assert prefix == "cld"
        assert suffix.isdigit() and len(suffix) == 5

    def test_auto_suffix_varies(self):
        # Collision across 20 random 5-digit picks is astronomically unlikely
        assert len({build_session_name("x") for _ in range(20)}) > 1


class TestFindJjRoot:
    def test_finds_in_start_dir(self, tmp_path):
        (tmp_path / ".jj").mkdir()
        assert find_jj_root(tmp_path) == tmp_path

    def test_walks_up_from_nested(self, tmp_path):
        (tmp_path / ".jj").mkdir()
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert find_jj_root(nested) == tmp_path

    def test_workspace_origin_env_takes_priority(self, tmp_path, monkeypatch):
        origin = tmp_path / "origin"
        (origin / ".jj").mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        (elsewhere / ".jj").mkdir(parents=True)
        monkeypatch.setenv("WORKSPACE_ORIGIN", str(origin))
        assert find_jj_root(elsewhere) == origin

    def test_exits_when_not_found(self, tmp_path):
        with pytest.raises(SystemExit):
            find_jj_root(tmp_path)


class TestLoadDotenv:
    def test_loads_key_value(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FOO", raising=False)
        env = tmp_path / ".env"
        env.write_text("FOO=bar\n")
        load_dotenv(env)
        assert os.environ["FOO"] == "bar"

    def test_ignores_comments_and_blanks(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BAZ", raising=False)
        env = tmp_path / ".env"
        env.write_text("# comment\n\n   \nBAZ=qux\n")
        load_dotenv(env)
        assert os.environ["BAZ"] == "qux"

    def test_strips_whitespace(self, tmp_path, monkeypatch):
        monkeypatch.delenv("K", raising=False)
        env = tmp_path / ".env"
        env.write_text("  K  =  v  \n")
        load_dotenv(env)
        assert os.environ["K"] == "v"

    def test_missing_file_is_noop(self, tmp_path):
        load_dotenv(tmp_path / "nonexistent")


class TestToHostPath:
    def test_translates_workspace_current(self, monkeypatch):
        monkeypatch.setenv("HOST_PROJECT_DIR", "/host/proj")
        assert _to_host_path("/workspace/current/file.py") == "/host/proj/file.py"

    def test_translates_workspace_origin(self, monkeypatch):
        monkeypatch.setenv("HOST_PROJECT_DIR", "/host/proj")
        assert _to_host_path("/workspace/origin/.jj") == "/host/proj/.jj"

    def test_translates_home(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/container")
        monkeypatch.setenv("HOST_HOME", "/home/host")
        assert _to_host_path("/home/container/.claude") == "/home/host/.claude"

    def test_no_env_no_translation(self):
        assert _to_host_path("/anywhere/else") == "/anywhere/else"

    def test_non_matching_path_untouched(self, monkeypatch):
        monkeypatch.setenv("HOST_PROJECT_DIR", "/host/proj")
        assert _to_host_path("/unrelated/path") == "/unrelated/path"


class TestMountHomePath:
    def test_missing_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert mount_home_path(".missing", "/dst") == []

    def test_existing_file_returns_v_args(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".gitconfig").write_text("x")
        args = mount_home_path(".gitconfig", "/dst:ro")
        assert args[0] == "-v"
        assert args[1].endswith(":/dst:ro")

    def test_applies_host_home_translation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HOST_HOME", "/host-home")
        (tmp_path / ".gitconfig").write_text("x")
        assert mount_home_path(".gitconfig", "/dst:ro") == [
            "-v", "/host-home/.gitconfig:/dst:ro",
        ]
