"""Tests for CLI argument validation via typer's CliRunner."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from cld.cli import app


runner = CliRunner()


class TestAgentCommand:
    def test_no_task_no_prompt_errors(self):
        result = runner.invoke(app, ["agent"])
        assert result.exit_code == 1
        assert "Provide a task file" in result.output

    def test_missing_task_file_errors(self, tmp_path):
        result = runner.invoke(app, ["agent", str(tmp_path / "nope.md")])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestLoopCommand:
    def test_no_task_no_prompt_errors(self):
        result = runner.invoke(app, ["loop"])
        assert result.exit_code == 1
        assert "Provide a task file" in result.output

    def test_missing_task_file_errors(self, tmp_path):
        result = runner.invoke(app, ["loop", str(tmp_path / "nope.md")])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestVersion:
    def test_version_flag_prints_and_exits(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "cld " in result.output



class TestReviewCommand:
    def test_review_requires_feature_branch(self):
        result = runner.invoke(app, ["review"])
        assert result.exit_code != 0


class TestReviewTrunkAutoDetection:
    """Cover trunk-branch auto-detection in cld/cli.py:167-180."""

    def _invoke(self, branches_output, argv=("review", "feature")):
        backend = MagicMock()
        backend.list_branches.return_value = branches_output
        with patch("cld.cli.get_backend", return_value=backend), \
             patch("cld.cli.launch_review") as lr:
            result = runner.invoke(app, list(argv))
        return result, backend, lr

    def test_auto_detects_main(self):
        result, _, lr = self._invoke("  main\n  feature\n* foo\n")
        assert result.exit_code == 0, result.output
        assert lr.call_args.args[2] == "main"

    def test_auto_detects_master_when_main_absent(self):
        result, _, lr = self._invoke("  master\n  feature\n")
        assert result.exit_code == 0, result.output
        assert lr.call_args.args[2] == "master"

    def test_auto_detects_trunk_when_main_master_absent(self):
        result, _, lr = self._invoke("  trunk\n  feature\n")
        assert result.exit_code == 0, result.output
        assert lr.call_args.args[2] == "trunk"

    def test_candidate_precedence_main_wins(self):
        result, _, lr = self._invoke("  main\n  master\n  trunk\n  feature\n")
        assert result.exit_code == 0, result.output
        assert lr.call_args.args[2] == "main"

    def test_no_candidate_found_raises(self):
        result, _, lr = self._invoke("  develop\n* feature\n")
        assert result.exit_code == 1
        assert not lr.called
        for candidate in ("main", "master", "trunk"):
            assert candidate in result.output

    def test_jj_bookmark_format_parsed(self):
        result, _, lr = self._invoke("main: abc123 [hash]\nfeature: def456 [hash]\n")
        assert result.exit_code == 0, result.output
        assert lr.call_args.args[2] == "main"

    def test_git_branch_format_parsed(self):
        result, _, lr = self._invoke("* feature\n  main\n  remotes/origin/main\n")
        assert result.exit_code == 0, result.output
        assert lr.call_args.args[2] == "main"

    def test_explicit_trunk_skips_detection(self):
        result, backend, lr = self._invoke(
            "", argv=("review", "feature", "explicit-trunk"),
        )
        assert result.exit_code == 0, result.output
        assert not backend.list_branches.called
        assert lr.call_args.args[2] == "explicit-trunk"


class TestAgentAtNotation:
    """Tests for @<name> prompt shortcut in cld agent."""

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
            result = runner.invoke(app, ["agent", "@no-such-prompt-xuniq", "-p", "task"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_at_notation_ambiguous_name_errors(self, tmp_path):
        (tmp_path / "prompts").mkdir(parents=True)
        (tmp_path / "prompts" / "test-xdup-zz9.md").write_text("a")
        (tmp_path / "prompts" / "personas").mkdir()
        (tmp_path / "prompts" / "personas" / "test-xdup-zz9.md").write_text("b")
        with patch("cld.cli.find_repo_root", return_value=tmp_path):
            result = runner.invoke(app, ["agent", "@test-xdup-zz9", "-p", "task"])
        assert result.exit_code == 1
        assert "Ambiguous" in result.output

    def test_at_notation_task_file_calls_launch_agent(self, tmp_path):
        task, _ = self._make_prompts(tmp_path)
        with patch("cld.cli.find_repo_root", return_value=tmp_path), \
             patch("cld.cli.launch_agent") as la:
            result = runner.invoke(app, ["agent", f"@{self._TASK_NAME}"])
        assert result.exit_code == 0, result.output
        assert la.called
        call_kwargs = la.call_args.kwargs
        assert call_kwargs["task_file"] == task
        assert call_kwargs.get("system_prompt_file") is None

    def test_at_notation_persona_with_prompt_calls_launch_agent(self, tmp_path):
        _, persona = self._make_prompts(tmp_path)
        vcs_mock = MagicMock()
        vcs_mock.repo_root = tmp_path
        with patch("cld.cli.find_repo_root", return_value=tmp_path), \
             patch("cld.cli.get_backend", return_value=vcs_mock), \
             patch("cld.cli.resolve_anchor", return_value="abc123"), \
             patch("cld.cli.create_editable_root"), \
             patch("cld.cli.agent_workspace_path", return_value=tmp_path / ".cld/ws"), \
             patch("cld.cli.launch_agent") as la:
            (tmp_path / ".cld/ws/.cld-run").mkdir(parents=True)
            result = runner.invoke(app, ["agent", f"@{self._PERSONA_NAME}", "-p", "do the task"])
        assert result.exit_code == 0, result.output
        assert la.called
        call_kwargs = la.call_args.kwargs
        assert call_kwargs.get("system_prompt_file") is not None
        assert call_kwargs.get("task_file") is None
        assert call_kwargs["inline_prompt"] == "do the task"

    def test_at_notation_persona_without_prompt_errors(self, tmp_path):
        self._make_prompts(tmp_path)
        with patch("cld.cli.find_repo_root", return_value=tmp_path):
            result = runner.invoke(app, ["agent", f"@{self._PERSONA_NAME}"])
        assert result.exit_code == 1
        assert "Provide a task file" in result.output


class TestDevcontainerCommand:
    def test_devcontainer_help(self):
        result = runner.invoke(app, ["devcontainer", "--help"])
        assert result.exit_code == 0
        assert "devcontainer" in result.output.lower()


class TestBuildCommand:
    def test_build_help(self):
        result = runner.invoke(app, ["build", "--help"])
        assert result.exit_code == 0
        assert "no-cache" in result.output
