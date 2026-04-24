"""Layer 3: Agent entrypoint E2E tests -- full container lifecycle with claude stub.

NOTE: The baked claude-agent image is jj-only (no git/VCS abstraction yet).
Git E2E tests are skipped until the image is rebuilt with vcs-lib.sh support.
"""

import random
import subprocess

import pytest

from tests.conftest import run_agent_container, skip_no_agent_image


pytestmark = [pytest.mark.e2e, pytest.mark.docker]

TASK_CONTENT = "Create a file called AGENT-RESULT.txt with some content."

# Summary fields present in the current baked agent image.
_REQUIRED_SUMMARY_FIELDS = (
    "status", "agent_name", "commit_hash",
    "timestamp", "duration_seconds", "claude_exit_code",
    "changes", "output",
)


def _session(prefix="test"):
    return f"{prefix}_{random.randint(10000, 99999)}"


@skip_no_agent_image
class TestAgentJj:
    """Agent E2E tests against a jujutsu repository."""

    def test_completes_successfully(self, e2e_jj_repo, claude_stub):
        session = _session("eagjj")
        exit_code, summary, stdout, stderr = run_agent_container(
            e2e_jj_repo.repo_root, session, TASK_CONTENT, claude_stub,
            vcs_type="jj",
        )
        assert exit_code == 0, f"Container failed:\nstdout={stdout}\nstderr={stderr}"
        assert summary is not None, f"No summary.json.\nstdout={stdout}\nstderr={stderr}"
        assert summary["status"] == "success"
        assert summary["agent_name"] == session

    def test_stub_file_committed(self, e2e_jj_repo, claude_stub):
        session = _session("eafjj")
        run_agent_container(
            e2e_jj_repo.repo_root, session, TASK_CONTENT, claude_stub,
            vcs_type="jj",
        )
        content = e2e_jj_repo.file_show(session, "AGENT-RESULT.txt")
        assert content is not None, "AGENT-RESULT.txt not found on branch"
        assert "stub change" in content

    def test_summary_has_required_fields(self, e2e_jj_repo, claude_stub):
        session = _session("easjj")
        _, summary, stdout, stderr = run_agent_container(
            e2e_jj_repo.repo_root, session, TASK_CONTENT, claude_stub,
            vcs_type="jj",
        )
        assert summary is not None, f"No summary.\nstdout={stdout}\nstderr={stderr}"
        for field in _REQUIRED_SUMMARY_FIELDS:
            assert field in summary, f"Missing field: {field}"
        assert summary["agent_name"] == session
        assert summary["changes"]["files_modified"] > 0

    def test_workspace_cleaned_up(self, e2e_jj_repo, claude_stub):
        session = _session("eawjj")
        run_agent_container(
            e2e_jj_repo.repo_root, session, TASK_CONTENT, claude_stub,
            vcs_type="jj",
        )
        result = subprocess.run(
            ["jj", "workspace", "list"],
            cwd=e2e_jj_repo.repo_root, capture_output=True, text=True,
        )
        assert session not in result.stdout

    def test_bookmark_persists_after_cleanup(self, e2e_jj_repo, claude_stub):
        session = _session("eapjj")
        run_agent_container(
            e2e_jj_repo.repo_root, session, TASK_CONTENT, claude_stub,
            vcs_type="jj",
        )
        branches = e2e_jj_repo.list_branches()
        assert session in branches

    def test_log_file_written(self, e2e_jj_repo, claude_stub):
        session = _session("ealjj")
        run_agent_container(
            e2e_jj_repo.repo_root, session, TASK_CONTENT, claude_stub,
            vcs_type="jj",
        )
        log_content = e2e_jj_repo.file_show(
            session, f"agent-output-{session}/agent.log",
        )
        assert log_content is not None
        assert "Agent" in log_content
        assert session in log_content

    def test_result_file_written(self, e2e_jj_repo, claude_stub):
        session = _session("earjj")
        run_agent_container(
            e2e_jj_repo.repo_root, session, TASK_CONTENT, claude_stub,
            vcs_type="jj",
        )
        result_content = e2e_jj_repo.file_show(
            session, f"agent-output-{session}/result.json",
        )
        assert result_content is not None


@skip_no_agent_image
class TestAgentGit:
    """Agent E2E tests against a git repository.

    Skipped: the baked claude-agent:latest image is jj-only.
    Remove skip when image is rebuilt with vcs-lib.sh support.
    """

    @pytest.mark.skip(reason="Baked agent image is jj-only (no vcs-lib.sh)")
    def test_creates_branch_and_commits(self, e2e_git_repo, claude_stub):
        session = _session("eaggit")
        exit_code, summary, stdout, stderr = run_agent_container(
            e2e_git_repo.repo_root, session, TASK_CONTENT, claude_stub,
            vcs_type="git",
        )
        assert exit_code == 0, f"Container failed:\nstdout={stdout}\nstderr={stderr}"
        assert summary is not None
        assert summary["status"] == "success"

    @pytest.mark.skip(reason="Baked agent image is jj-only (no vcs-lib.sh)")
    def test_workspace_cleaned_up(self, e2e_git_repo, claude_stub):
        session = _session("eawgit")
        run_agent_container(
            e2e_git_repo.repo_root, session, TASK_CONTENT, claude_stub,
            vcs_type="git",
        )
        result = subprocess.run(
            ["git", "worktree", "list"],
            cwd=e2e_git_repo.repo_root, capture_output=True, text=True,
        )
        lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
        assert len(lines) == 1
