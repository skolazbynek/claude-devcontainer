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


class TestVersion:
    def test_version_flag_prints_and_exits(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "cld " in result.output



class TestReviewCommand:
    def test_review_requires_feature_branch(self):
        result = runner.invoke(app, ["review"])
        assert result.exit_code != 0


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
