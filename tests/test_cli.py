"""Tests for CLI argument validation via typer's CliRunner."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cld.cli import (
    _forget_session_bookmark,
    _persistent_container_name,
    _persistent_container_status,
    _shutdown_persistent_container,
    app,
)
from cld.docker import agent_container_name, master_container_name


runner = CliRunner()


class TestRunCommand:
    def test_no_task_no_prompt_errors(self):
        result = runner.invoke(app, ["run"])
        assert result.exit_code == 1
        assert "Provide a task file" in result.output

    def test_missing_task_file_errors(self, tmp_path):
        result = runner.invoke(app, ["run", str(tmp_path / "nope.md")])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestVersion:
    def test_version_flag_prints_and_exits(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "cld " in result.output


class TestRunAtNotation:
    """Tests for @<name> prompt shortcut in cld run."""

    # Use names that don't exist in the real /workspace/current/prompts/ to
    # avoid duplicate-match errors when cld_root == the live repo root.
    _TASK_NAME = "test-xuniq-task-zz9"
    _PERSONA_NAME = "test-xuniq-persona-zz9"

    def _make_prompts(self, tmp_path: Path):
        task = tmp_path / "prompts" / f"{self._TASK_NAME}.md"
        task.parent.mkdir(parents=True)
        task.write_text("# Task\nDo something\n")
        persona = tmp_path / "prompts" / "personas" / f"{self._PERSONA_NAME}.md"
        persona.parent.mkdir(parents=True)
        persona.write_text(f"---\nname: {self._PERSONA_NAME}\n---\n# Test persona\n")
        return task, persona

    def test_at_notation_unknown_name_errors(self, tmp_path):
        with patch("cld.cli.find_repo_root", return_value=tmp_path):
            (tmp_path / "prompts").mkdir(parents=True)
            result = runner.invoke(app, ["run", "@no-such-prompt-xuniq", "-p", "task"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_at_notation_ambiguous_name_errors(self, tmp_path):
        (tmp_path / "prompts").mkdir(parents=True)
        (tmp_path / "prompts" / "test-xdup-zz9.md").write_text("a")
        (tmp_path / "prompts" / "personas").mkdir()
        (tmp_path / "prompts" / "personas" / "test-xdup-zz9.md").write_text("b")
        with patch("cld.cli.find_repo_root", return_value=tmp_path):
            result = runner.invoke(app, ["run", "@test-xdup-zz9", "-p", "task"])
        assert result.exit_code == 1
        assert "Ambiguous" in result.output

    def test_at_notation_task_file_calls_launch_run(self, tmp_path):
        task, _ = self._make_prompts(tmp_path)
        with patch("cld.cli.find_repo_root", return_value=tmp_path), \
             patch("cld.cli.launch_run") as la:
            result = runner.invoke(app, ["run", f"@{self._TASK_NAME}"])
        assert result.exit_code == 0, result.output
        assert la.called
        call_kwargs = la.call_args.kwargs
        assert call_kwargs["task_file"] == task
        assert call_kwargs.get("system_prompt_file") is None

    def test_at_notation_with_inline_prompt(self, tmp_path):
        """`cld run @name -p prompt` resolves the @-ref as task_file and passes prompt."""
        task, _ = self._make_prompts(tmp_path)
        with patch("cld.cli.find_repo_root", return_value=tmp_path), \
             patch("cld.cli.launch_run") as la:
            result = runner.invoke(app, ["run", f"@{self._TASK_NAME}", "-p", "do the task"])
        assert result.exit_code == 0, result.output
        assert la.called
        call_kwargs = la.call_args.kwargs
        assert call_kwargs["task_file"] == task
        assert call_kwargs["inline_prompt"] == "do the task"


class TestBareDevcontainer:
    def test_bare_invokes_run_devcontainer(self, tmp_path):
        with patch("cld.cli._run_devcontainer") as rd:
            result = runner.invoke(app, [])
        assert result.exit_code == 0, result.output
        assert rd.called
        # Signature: (task_file, name, model, revision, prompt, extra_args)
        args = rd.call_args.args
        assert args[0] is None  # task_file
        assert args[1] == ""     # name

    def test_bare_with_options(self):
        with patch("cld.cli._run_devcontainer") as rd:
            result = runner.invoke(app, ["-n", "foo", "-m", "opus"])
        assert result.exit_code == 0, result.output
        assert rd.called
        args = rd.call_args.args
        assert args[1] == "foo"   # name
        assert args[2] == "opus"  # model


class TestPersistentContainerHelpers:
    def test_name_master(self, tmp_path):
        assert _persistent_container_name("master", tmp_path) == master_container_name(tmp_path)

    def test_name_agent(self, tmp_path):
        assert _persistent_container_name("agent", tmp_path) == agent_container_name(tmp_path)

    def test_status_delegates_to_master(self):
        with patch("cld.cli.docker_master_status", return_value="running") as m:
            assert _persistent_container_status("master", "x") == "running"
            m.assert_called_once_with("x")

    def test_status_delegates_to_agent(self):
        with patch("cld.cli.docker_agent_status", return_value="stopped") as a:
            assert _persistent_container_status("agent", "y") == "stopped"
            a.assert_called_once_with("y")


class TestAgentSubcommand:
    def test_agent_starts_new_container_without_attaching(self, tmp_path):
        repo_root = tmp_path / "myrepo"
        repo_root.mkdir()
        with patch("cld.cli.require_docker"), \
             patch("cld.cli.ensure_image"), \
             patch("cld.cli.find_target_repo", return_value=repo_root), \
             patch("cld.cli.docker_agent_status", return_value="absent"), \
             patch("cld.cli.anchor_env_args", return_value=["-e", "AGENT_ANCHOR_HASH=def456"]), \
             patch("cld.cli.build_container_args", return_value=["--rm"]) as bca, \
             patch("cld.cli.stage_home_ro", return_value=[]), \
             patch("cld.cli.stage_ssh_agent", return_value=[]), \
             patch("cld.cli.subprocess.run") as run_mock, \
             patch("cld.cli.os.execvp") as execvp_mock, \
             patch("cld.cli._wait_for_container_ready", return_value=True):
            result = runner.invoke(app, ["agent"])
        assert result.exit_code == 0, result.output
        assert "started for" in result.output
        assert not execvp_mock.called
        assert bca.call_args.kwargs["agent"] is True
        assert bca.call_args.kwargs["master"] is False
        run_calls = [c.args[0] for c in run_mock.call_args_list]
        assert any(c[:3] == ["docker", "run", "-d"] for c in run_calls)

    def test_agent_reattach_running_does_not_attach(self, tmp_path):
        with patch("cld.cli.require_docker"), \
             patch("cld.cli.ensure_image"), \
             patch("cld.cli.find_target_repo", return_value=tmp_path), \
             patch("cld.cli.docker_agent_status", return_value="running"), \
             patch("cld.cli.os.execvp") as execvp_mock:
            result = runner.invoke(app, ["agent"])
        assert result.exit_code == 0, result.output
        assert not execvp_mock.called
        assert "is running" in result.output

    def test_agent_reattach_stopped_starts_container(self, tmp_path):
        with patch("cld.cli.require_docker"), \
             patch("cld.cli.ensure_image"), \
             patch("cld.cli.find_target_repo", return_value=tmp_path), \
             patch("cld.cli.docker_agent_status", return_value="stopped"), \
             patch("cld.cli.subprocess.run") as run_mock:
            result = runner.invoke(app, ["agent", "-r", "somerev"])
        assert result.exit_code == 0, result.output
        assert any(c.args[0][:2] == ["docker", "start"] for c in run_mock.call_args_list)


class TestAgentStatusCommand:
    def test_absent(self, tmp_path):
        with patch("cld.cli.find_target_repo", return_value=tmp_path), \
             patch("cld.cli.docker_agent_status", return_value="absent"):
            result = runner.invoke(app, ["agent", "status"])
        assert result.exit_code == 0, result.output
        assert "absent" in result.output

    def test_reads_state_json(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        name = agent_container_name(repo_root)
        mailbox_root = tmp_path / "mailboxes"
        (mailbox_root / name).mkdir(parents=True)
        state = {
            "phase": "idle", "session_id": "sid123", "msg_count": 3,
            "cost_usd_total": 1.2345, "current": None,
        }
        (mailbox_root / name / "state.json").write_text(json.dumps(state))
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(mailbox_root))

        with patch("cld.cli.find_target_repo", return_value=repo_root), \
             patch("cld.cli.docker_agent_status", return_value="running"):
            result = runner.invoke(app, ["agent", "status"])
        assert result.exit_code == 0, result.output
        assert "idle" in result.output
        assert "sid123" in result.output
        assert "3" in result.output


class TestMasterStatusCommand:
    def test_master_status_skips_state_json(self, tmp_path):
        with patch("cld.cli.find_target_repo", return_value=tmp_path), \
             patch("cld.cli.docker_master_status", return_value="absent"):
            result = runner.invoke(app, ["master", "status"])
        assert result.exit_code == 0, result.output
        assert "Supervisor" not in result.output


class TestAgentLogsCommand:
    def test_absent_errors(self, tmp_path):
        with patch("cld.cli.require_docker"), \
             patch("cld.cli.find_target_repo", return_value=tmp_path), \
             patch("cld.cli.docker_agent_status", return_value="absent"):
            result = runner.invoke(app, ["agent", "logs"])
        assert result.exit_code == 1
        assert "No agent container found" in result.output

    def test_tails_docker_logs(self, tmp_path):
        fake_result = MagicMock(stdout="log line 1\nlog line 2\n", stderr="")
        with patch("cld.cli.require_docker"), \
             patch("cld.cli.find_target_repo", return_value=tmp_path), \
             patch("cld.cli.docker_agent_status", return_value="running"), \
             patch("cld.cli.subprocess.run", return_value=fake_result) as run_mock:
            result = runner.invoke(app, ["agent", "logs", "-n", "50"])
        assert result.exit_code == 0, result.output
        assert "log line 1" in result.output
        called_args = run_mock.call_args.args[0]
        assert called_args[:2] == ["docker", "logs"]
        assert "50" in called_args


class TestAgentShutdown:
    def test_absent(self, tmp_path):
        with patch("cld.cli.require_docker"), \
             patch("cld.cli.find_target_repo", return_value=tmp_path), \
             patch("cld.cli.docker_agent_status", return_value="absent"):
            result = runner.invoke(app, ["agent", "shutdown"])
        assert result.exit_code == 0, result.output
        assert "No agent container found" in result.output

    def test_all_uses_docker_agent_list_not_master(self):
        with patch("cld.cli.require_docker"), \
             patch("cld.cli.docker_agent_list", return_value=[]) as agent_list, \
             patch("cld.cli.docker_master_list") as master_list:
            result = runner.invoke(app, ["agent", "shutdown", "--all"])
        assert result.exit_code == 0, result.output
        assert agent_list.called
        assert not master_list.called
        assert "No agent containers found" in result.output


class TestAgentRestart:
    def test_absent_errors_with_hint(self, tmp_path):
        with patch("cld.cli.require_docker"), \
             patch("cld.cli.find_target_repo", return_value=tmp_path), \
             patch("cld.cli.docker_agent_status", return_value="absent"):
            result = runner.invoke(app, ["agent", "restart"])
        assert result.exit_code == 1
        assert "agent" in result.output


class TestShutdownForgetsBookmark:
    """Shutdown drops the session bookmark so the next launch is a fresh lifecycle."""

    def _jj_backend(self):
        backend = MagicMock()
        backend.name = "jj"
        backend.run.return_value = MagicMock(returncode=0, stderr="")
        return backend

    def test_jj_backend_runs_bookmark_forget(self, tmp_path):
        backend = self._jj_backend()
        with patch("cld.cli.get_backend", return_value=backend), \
             patch("cld.cli._stop_and_remove_container") as stop_mock:
            ok = _shutdown_persistent_container("master", "cld_master_x", str(tmp_path), "cld_master_x")
        assert ok
        stop_mock.assert_called_once_with("cld_master_x")
        backend.run.assert_called_once_with(["bookmark", "forget", "cld_master_x"])

    def test_git_backend_skips_bookmark_forget(self, tmp_path):
        backend = MagicMock()
        backend.name = "git"
        with patch("cld.cli.get_backend", return_value=backend), \
             patch("cld.cli._stop_and_remove_container"):
            ok = _shutdown_persistent_container("agent", "cld_agent_x", str(tmp_path), "cld_agent_x")
        assert ok
        backend.run.assert_not_called()

    def test_bookmark_forget_failure_is_non_fatal(self, tmp_path):
        backend = MagicMock()
        backend.name = "jj"
        backend.run.return_value = MagicMock(returncode=1, stderr="conflict")
        with patch("cld.cli.get_backend", return_value=backend), \
             patch("cld.cli._stop_and_remove_container"):
            ok = _shutdown_persistent_container("master", "cld_master_x", str(tmp_path), "cld_master_x")
        assert ok

    def test_missing_repo_root_is_non_fatal(self, tmp_path):
        gone = tmp_path / "gone"
        with patch("cld.cli.get_backend") as get_backend_mock, \
             patch("cld.cli._stop_and_remove_container"):
            _forget_session_bookmark(str(gone), "cld_master_x")
        get_backend_mock.assert_not_called()

    def test_get_backend_failure_is_non_fatal(self, tmp_path):
        with patch("cld.cli.get_backend", side_effect=RuntimeError("no vcs")), \
             patch("cld.cli._stop_and_remove_container"):
            ok = _shutdown_persistent_container("master", "cld_master_x", str(tmp_path), "cld_master_x")
        assert ok

    def test_shutdown_all_forgets_each_bookmark(self, tmp_path):
        repo_a = tmp_path / "a"; repo_a.mkdir()
        repo_b = tmp_path / "b"; repo_b.mkdir()
        containers = [
            {"name": "cld_master_a", "repo_root": str(repo_a), "session": "cld_master_a"},
            {"name": "cld_master_b", "repo_root": str(repo_b), "session": "cld_master_b"},
        ]
        backend = self._jj_backend()
        with patch("cld.cli.require_docker"), \
             patch("cld.cli.docker_master_list", return_value=containers), \
             patch("cld.cli.get_backend", return_value=backend), \
             patch("cld.cli._stop_and_remove_container"):
            result = runner.invoke(app, ["master", "shutdown", "--all"])
        assert result.exit_code == 0, result.output
        forget_calls = [c.args[0] for c in backend.run.call_args_list]
        assert ["bookmark", "forget", "cld_master_a"] in forget_calls
        assert ["bookmark", "forget", "cld_master_b"] in forget_calls


class TestRestartPreservesBookmark:
    """Restart bypasses `_shutdown_persistent_container` so the bookmark survives."""

    def test_master_restart_does_not_forget_bookmark(self, tmp_path):
        with patch("cld.cli.require_docker"), \
             patch("cld.cli.find_target_repo", return_value=tmp_path), \
             patch("cld.cli.docker_master_status", return_value="running"), \
             patch("cld.cli._shutdown_persistent_container") as shutdown_mock, \
             patch("cld.cli._forget_session_bookmark") as forget_mock, \
             patch("cld.cli._stop_and_remove_container") as stop_mock, \
             patch("cld.cli._run_persistent_devcontainer") as launch_mock:
            result = runner.invoke(app, ["master", "restart"])
        assert result.exit_code == 0, result.output
        shutdown_mock.assert_not_called()
        forget_mock.assert_not_called()
        stop_mock.assert_called_once()
        launch_mock.assert_called_once()

    def test_agent_restart_does_not_forget_bookmark(self, tmp_path):
        with patch("cld.cli.require_docker"), \
             patch("cld.cli.find_target_repo", return_value=tmp_path), \
             patch("cld.cli.docker_agent_status", return_value="running"), \
             patch("cld.cli._shutdown_persistent_container") as shutdown_mock, \
             patch("cld.cli._forget_session_bookmark") as forget_mock, \
             patch("cld.cli._stop_and_remove_container") as stop_mock, \
             patch("cld.cli._run_persistent_devcontainer") as launch_mock:
            result = runner.invoke(app, ["agent", "restart"])
        assert result.exit_code == 0, result.output
        shutdown_mock.assert_not_called()
        forget_mock.assert_not_called()
        stop_mock.assert_called_once()
        launch_mock.assert_called_once()


class TestMasterReposCommand:
    def test_errors_when_not_in_master(self):
        result = runner.invoke(app, ["master", "repos"])
        assert result.exit_code == 1
        assert "master" in result.output.lower()

    def test_lists_own_and_targets(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MASTER_MODE", "1")
        monkeypatch.setenv("CLD_HOST_PROJECT_DIR", "/host/side/cld")
        proj = tmp_path / ".cld.config"
        proj.write_text('master_targets = ["/host/side/foo", "/host/side/bar"]\n')
        with patch("cld.cli.Config.from_env", return_value=__import__(
            "cld.config", fromlist=["Config"],
        ).Config(host_project_dir="/host/side/cld",
                 master_targets=("/host/side/foo", "/host/side/bar"))):
            result = runner.invoke(app, ["master", "repos"])
        assert result.exit_code == 0, result.output
        assert "/host/side/cld\town" in result.output
        assert "/host/side/foo\ttarget" in result.output
        assert "/host/side/bar\ttarget" in result.output


class TestChainBlockedInMaster:
    def test_run_chain_blocked_inside_master(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MASTER_MODE", "1")
        from cld.chain import run_chain
        from cld.config import Config
        chain_file = tmp_path / "c.yaml"
        chain_file.write_text("name: dummy\nsteps: []\n")
        with pytest.raises(RuntimeError, match="not yet supported from inside a master"):
            run_chain(Config(), chain_file)


class TestBuildCommand:
    def test_build_help(self):
        result = runner.invoke(app, ["build", "--help"])
        assert result.exit_code == 0
        assert "no-cache" in result.output
