"""Tests for CLI argument validation via typer's CliRunner."""

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
