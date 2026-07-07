"""Tests for cld.run.launch_run input validation.

Task composition (merging task file + inline prompt) was moved into the
container entrypoint so the resulting file lands on the agent's VCS change,
not on the host's working copy. Only the host-side validation is unit-tested
here; composition is exercised by integration tests.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cld.run import launch_run
from cld.config import Config


class TestLaunchAgentValidation:
    def test_neither_task_file_nor_prompt_exits(self):
        with patch("cld.run.require_docker"):
            with pytest.raises(SystemExit):
                launch_run(Config())


def _args_contain(cmd, *args):
    """Return True if args appear consecutively in cmd."""
    n = len(args)
    for i in range(len(cmd) - n + 1):
        if cmd[i : i + n] == list(args):
            return True
    return False


class TestLaunchAgentExtensions:
    """Verify system_prompt_file and extra_env produce the right docker run args."""

    def _capture_docker_cmd(self, **kwargs):
        """Patch all externals; return the full docker-run command list."""
        session = "agent_testxx"

        mock_vcs = MagicMock()
        mock_vcs.name = "jj"
        mock_vcs.repo_root = Path("/fake/repo")
        mock_vcs.workspace_revision = ""

        docker_result = MagicMock()
        docker_result.returncode = 0
        docker_result.stdout = "abc123def\n"
        docker_result.stderr = ""

        with (
            patch("cld.run.require_docker"),
            patch("cld.run.find_repo_context", return_value=(Path("/fake/repo"), "")),
            patch("cld.run.get_backend", return_value=mock_vcs),
            patch("cld.run.ensure_image"),
            patch("cld.run.build_session_name", return_value=session),
            patch("cld.run.build_container_args", return_value=[]),
            patch("cld.run.session_workspace_path", return_value=Path("/fake/repo/.cld/workspaces") / session),
            patch("cld.run.resolve_anchor", return_value="deadbeef1234"),
            patch("cld.run.subprocess.run", return_value=docker_result) as mock_run,
        ):
            launch_run(Config(), quiet=True, **kwargs)
            return list(mock_run.call_args.args[0])

    def test_system_prompt_file_adds_mount_and_env(self):
        cmd = self._capture_docker_cmd(
            task_file=Path("/tmp/task.md"),
            system_prompt_file=Path("/tmp/p.md"),
        )
        assert _args_contain(cmd, "-v", "/tmp/p.md:/config/persona.md:ro")
        assert _args_contain(cmd, "-e", "AGENT_SYSTEM_PROMPT_FILE=/config/persona.md")

    def test_extra_env_adds_env_args(self):
        cmd = self._capture_docker_cmd(
            task_file=Path("/tmp/task.md"),
            extra_env={"FOO": "bar"},
        )
        assert _args_contain(cmd, "-e", "FOO=bar")

    def test_backwards_compat_no_new_params(self):
        """Omitting new params produces no persona mount or AGENT_SYSTEM_PROMPT_FILE."""
        cmd = self._capture_docker_cmd(task_file=Path("/tmp/task.md"))
        cmd_str = " ".join(cmd)
        assert "/config/persona.md" not in cmd_str
        assert "AGENT_SYSTEM_PROMPT_FILE" not in cmd_str
