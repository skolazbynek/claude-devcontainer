"""Tests for Config TOML layering and resolution order."""

from pathlib import Path

import pytest

from cld.config import Config, _find_project_config, _load_toml


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for var in (
        "CLD_BASE_IMAGE", "CLD_DEVCONTAINER_IMAGE", "CLD_AGENT_IMAGE",
        "CLD_MYSQL_CONFIG", "CLD_AGENT_TIMEOUT", "CLD_POLL_INTERVAL", "CLD_DEBUG",
    ):
        monkeypatch.delenv(var, raising=False)


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


class TestTomlLayering:
    def test_user_only(self, tmp_path):
        user = _write(tmp_path / "user.toml", 'base_image = "u-base"\nagent_timeout = 99\n')
        cfg = Config.from_env(user_config=user, project_config=tmp_path / "missing")
        assert cfg.base_image == "u-base"
        assert cfg.agent_timeout == 99

    def test_project_only(self, tmp_path):
        proj = _write(tmp_path / ".cld.config", 'base_image = "p-base"\n')
        cfg = Config.from_env(user_config=tmp_path / "missing", project_config=proj)
        assert cfg.base_image == "p-base"

    def test_project_overrides_user(self, tmp_path):
        user = _write(tmp_path / "user.toml", 'base_image = "u"\nagent_image = "u-agent"\n')
        proj = _write(tmp_path / ".cld.config", 'base_image = "p"\n')
        cfg = Config.from_env(user_config=user, project_config=proj)
        assert cfg.base_image == "p"
        assert cfg.agent_image == "u-agent"  # only project's keys override

    def test_env_overrides_toml(self, tmp_path, monkeypatch):
        proj = _write(tmp_path / ".cld.config", 'base_image = "p"\n')
        monkeypatch.setenv("CLD_BASE_IMAGE", "env-base")
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.base_image == "env-base"

    def test_dotenv_overrides_toml(self, tmp_path):
        proj = _write(tmp_path / ".cld.config", 'base_image = "p"\n')
        dotenv = _write(tmp_path / ".env", "CLD_BASE_IMAGE=dotenv-base\n")
        cfg = Config.from_env(dotenv=dotenv, user_config=tmp_path / "u", project_config=proj)
        assert cfg.base_image == "dotenv-base"

    def test_missing_files_use_defaults(self, tmp_path):
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=tmp_path / "p")
        assert cfg.base_image == "claude-base:latest"
        assert cfg.agent_timeout == 1800
        assert cfg.debug is False

    def test_unknown_key_warns_but_loads(self, tmp_path, capsys):
        proj = _write(tmp_path / ".cld.config", 'base_image = "p"\nbogus = 1\n')
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.base_image == "p"
        err = capsys.readouterr().err
        assert "unknown key 'bogus'" in err

    def test_malformed_toml_does_not_crash(self, tmp_path, capsys):
        proj = _write(tmp_path / ".cld.config", "this = is = not valid toml\n")
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.base_image == "claude-base:latest"
        assert "failed to read" in capsys.readouterr().err

    def test_int_and_bool_types(self, tmp_path):
        proj = _write(tmp_path / ".cld.config", "agent_timeout = 42\npoll_interval = 7\ndebug = true\n")
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.agent_timeout == 42
        assert cfg.poll_interval == 7
        assert cfg.debug is True


class TestFindProjectConfig:
    def test_finds_in_start_dir(self, tmp_path):
        cfg = _write(tmp_path / ".cld.config", "")
        assert _find_project_config(tmp_path) == cfg

    def test_walks_up_from_nested(self, tmp_path):
        cfg = _write(tmp_path / ".cld.config", "")
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert _find_project_config(nested) == cfg

    def test_returns_none_if_absent(self, tmp_path):
        assert _find_project_config(tmp_path) is None


class TestLoadToml:
    def test_filters_unknown_keys(self, tmp_path):
        p = _write(tmp_path / "c.toml", 'base_image = "x"\nbogus = 1\n')
        assert _load_toml(p) == {"base_image": "x"}
