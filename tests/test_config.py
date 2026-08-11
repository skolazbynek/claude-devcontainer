"""Tests for Config TOML layering and resolution order."""

from pathlib import Path

import pytest

from cld.config import Config, _find_project_config, _load_toml


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for var in (
        "CLD_BASE_IMAGE", "CLD_DEVCONTAINER_IMAGE", "CLD_AGENT_IMAGE",
        "CLD_MYSQL_CONFIG", "CLD_AGENT_TIMEOUT", "CLD_POLL_INTERVAL", "CLD_DEBUG",
        "CLD_MAILBOX_ROOT", "CLD_AGENT_MAX_TURNS", "CLD_AGENT_KICKOFF_PERSONA",
        "CLD_MAX_TASK_AGENTS", "CLD_PEER_ABSOLUTE_LIMIT",
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
        proj = _write(tmp_path / ".cld/config.toml", 'base_image = "p-base"\n')
        cfg = Config.from_env(user_config=tmp_path / "missing", project_config=proj)
        assert cfg.base_image == "p-base"

    def test_project_overrides_user(self, tmp_path):
        user = _write(tmp_path / "user.toml", 'base_image = "u"\nrun_image = "u-run"\n')
        proj = _write(tmp_path / ".cld/config.toml", 'base_image = "p"\n')
        cfg = Config.from_env(user_config=user, project_config=proj)
        assert cfg.base_image == "p"
        assert cfg.run_image == "u-run"  # only project's keys override

    def test_env_overrides_toml(self, tmp_path, monkeypatch):
        proj = _write(tmp_path / ".cld/config.toml", 'base_image = "p"\n')
        monkeypatch.setenv("CLD_BASE_IMAGE", "env-base")
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.base_image == "env-base"

    def test_dotenv_overrides_toml(self, tmp_path):
        proj = _write(tmp_path / ".cld/config.toml", 'base_image = "p"\n')
        dotenv = _write(tmp_path / ".env", "CLD_BASE_IMAGE=dotenv-base\n")
        cfg = Config.from_env(dotenv=dotenv, user_config=tmp_path / "u", project_config=proj)
        assert cfg.base_image == "dotenv-base"

    def test_missing_files_use_defaults(self, tmp_path):
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=tmp_path / "p")
        assert cfg.base_image == "claude-base:latest"
        assert cfg.agent_timeout == 1800
        assert cfg.debug is False

    def test_unknown_key_warns_but_loads(self, tmp_path, capsys):
        proj = _write(tmp_path / ".cld/config.toml", 'base_image = "p"\nbogus = 1\n')
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.base_image == "p"
        err = capsys.readouterr().err
        assert "unknown key 'bogus'" in err

    def test_malformed_toml_does_not_crash(self, tmp_path, capsys):
        proj = _write(tmp_path / ".cld/config.toml", "this = is = not valid toml\n")
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.base_image == "claude-base:latest"
        assert "failed to read" in capsys.readouterr().err

    def test_int_and_bool_types(self, tmp_path):
        proj = _write(tmp_path / ".cld/config.toml", "agent_timeout = 42\npoll_interval = 7\ndebug = true\n")
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.agent_timeout == 42
        assert cfg.poll_interval == 7
        assert cfg.debug is True


class TestMessengerConfig:
    def test_defaults(self, tmp_path):
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=tmp_path / "p")
        assert cfg.mailbox_root.endswith(".cld/mailboxes")
        assert cfg.agent_max_turns == 30
        assert cfg.agent_kickoff_persona == "agent"

    def test_toml_overrides(self, tmp_path):
        proj = _write(
            tmp_path / ".cld/config.toml",
            'mailbox_root = "/custom/mailboxes"\nagent_max_turns = 10\nagent_kickoff_persona = "custom-persona"\n',
        )
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.mailbox_root == "/custom/mailboxes"
        assert cfg.agent_max_turns == 10
        assert cfg.agent_kickoff_persona == "custom-persona"

    def test_env_overrides_toml(self, tmp_path, monkeypatch):
        proj = _write(tmp_path / ".cld/config.toml", 'mailbox_root = "/toml/mailboxes"\n')
        monkeypatch.setenv("CLD_MAILBOX_ROOT", "/env/mailboxes")
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.mailbox_root == "/env/mailboxes"


class TestTaskAgentConfig:
    def test_defaults(self, tmp_path):
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=tmp_path / "p")
        assert cfg.max_task_agents == 4
        assert cfg.peer_absolute_limit == 10

    def test_toml_overrides_without_warning(self, tmp_path, capsys):
        proj = _write(tmp_path / ".cld/config.toml", "max_task_agents = 2\npeer_absolute_limit = 25\n")
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.max_task_agents == 2
        assert cfg.peer_absolute_limit == 25
        assert "unknown key" not in capsys.readouterr().err

    def test_env_overrides_toml(self, tmp_path, monkeypatch):
        proj = _write(tmp_path / ".cld/config.toml", "max_task_agents = 2\npeer_absolute_limit = 25\n")
        monkeypatch.setenv("CLD_MAX_TASK_AGENTS", "8")
        monkeypatch.setenv("CLD_PEER_ABSOLUTE_LIMIT", "3")
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.max_task_agents == 8
        assert cfg.peer_absolute_limit == 3


class TestFindProjectConfig:
    def test_finds_in_start_dir(self, tmp_path):
        cfg = _write(tmp_path / ".cld/config.toml", "")
        assert _find_project_config(tmp_path) == cfg

    def test_walks_up_from_nested(self, tmp_path):
        cfg = _write(tmp_path / ".cld/config.toml", "")
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert _find_project_config(nested) == cfg

    def test_returns_none_if_absent(self, tmp_path):
        assert _find_project_config(tmp_path) is None


class TestLoadToml:
    def test_filters_unknown_keys(self, tmp_path):
        p = _write(tmp_path / "c.toml", 'base_image = "x"\nbogus = 1\n')
        assert _load_toml(p) == {"base_image": "x"}


class TestMasterTargets:
    def test_master_targets_loaded(self, tmp_path):
        proj = _write(
            tmp_path / ".cld/config.toml",
            'master_targets = ["~/projects/foo", "/abs/bar"]\n',
        )
        cfg = Config.from_env(user_config=tmp_path / "u", project_config=proj)
        assert cfg.master_targets == ("~/projects/foo", "/abs/bar")

    def test_deprecated_key_errors_with_migration_hint(self, tmp_path):
        proj = _write(tmp_path / ".cld/config.toml", 'master_extra_mounts_ro = ["~/repos"]\n')
        with pytest.raises(RuntimeError) as excinfo:
            _load_toml(proj)
        assert "master_extra_mounts_ro" in str(excinfo.value)
        assert "master_targets" in str(excinfo.value)
