"""Tests for CLI argument validation via typer's CliRunner."""

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
        with patch("cld.vcs.get_backend", return_value=backend), \
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
