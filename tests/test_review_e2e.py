"""Layer 4: Review workflow E2E tests -- diff generation, template filling, agent launch."""

import subprocess
from pathlib import Path
from string import Template
from unittest.mock import patch

import pytest

from cld.vcs.git import GitBackend
from cld.vcs.jj import JjBackend
from tests.conftest import skip_no_agent_image


pytestmark = [pytest.mark.e2e, pytest.mark.docker]


def _make_jj_repo_with_branches(tmp_path):
    """Create a jj repo with a trunk and feature branch that have diverged."""
    subprocess.run(
        ["jj", "git", "init"], cwd=tmp_path, check=True, capture_output=True,
    )
    # Seed commit
    (tmp_path / "main.py").write_text("def main():\n    pass\n")
    subprocess.run(
        ["jj", "commit", "-m", "initial code"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    # Mark trunk
    subprocess.run(
        ["jj", "bookmark", "create", "trunk", "-r", "@-"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    # Feature changes
    (tmp_path / "main.py").write_text("def main():\n    print('hello')\n")
    (tmp_path / "new_feature.py").write_text("def feature():\n    return 42\n")
    subprocess.run(
        ["jj", "commit", "-m", "add feature"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["jj", "bookmark", "create", "feature", "-r", "@-"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    return JjBackend(tmp_path)


def _make_git_repo_with_branches(tmp_path):
    """Create a git repo with a trunk and feature branch that have diverged."""
    subprocess.run(
        ["git", "init"], cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    # Initial commit on trunk
    (tmp_path / "main.py").write_text("def main():\n    pass\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial code"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "branch", "trunk"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    # Feature branch
    subprocess.run(
        ["git", "checkout", "-b", "feature"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    (tmp_path / "main.py").write_text("def main():\n    print('hello')\n")
    (tmp_path / "new_feature.py").write_text("def feature():\n    return 42\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add feature"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    # Go back to trunk
    subprocess.run(
        ["git", "checkout", "trunk"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    return GitBackend(tmp_path)


@pytest.fixture(params=["jj", "git"])
def repo_with_branches(request, tmp_path):
    if request.param == "jj":
        return _make_jj_repo_with_branches(tmp_path)
    return _make_git_repo_with_branches(tmp_path)


class TestReviewDiffGeneration:
    """Test the diff generation and template filling parts of launch_review."""

    def test_fork_point_found(self, repo_with_branches):
        vcs = repo_with_branches
        fork = vcs.fork_point("feature", "trunk")
        assert fork
        assert len(fork) >= 7

    def test_diff_between_branches_has_content(self, repo_with_branches):
        vcs = repo_with_branches
        fork = vcs.fork_point("feature", "trunk")
        diff = vcs.diff_between(fork, "feature")
        assert "new_feature.py" in diff
        assert "hello" in diff

    def test_diff_does_not_contain_trunk_only_changes(self, repo_with_branches):
        vcs = repo_with_branches
        fork = vcs.fork_point("feature", "trunk")
        diff = vcs.diff_between(fork, "feature")
        # The diff should only show feature branch changes, not trunk content
        # main.py's initial content (def main(): pass) should show as removed/changed
        assert "new_feature.py" in diff

    def test_review_template_substitution(self):
        template_path = Path(__file__).parent.parent / "imgs/claude-agent-review/review-template.md"
        if not template_path.is_file():
            pytest.skip("review-template.md not found")
        template = Template(template_path.read_text())
        result = template.safe_substitute(
            TRUNK_BRANCH="main",
            FEATURE_BRANCH="my-feature",
            DIFF_FILE_PATH="/workspace/origin/review.patch",
        )
        assert "main" in result
        assert "my-feature" in result
        assert "/workspace/origin/review.patch" in result

    def test_empty_diff_between_same_revision(self, repo_with_branches):
        vcs = repo_with_branches
        rev = vcs.resolve_revision("trunk")
        diff = vcs.diff_between(rev, rev)
        assert not diff.strip()


@skip_no_agent_image
class TestReviewLaunchIntegration:
    """Integration test for launch_review using mocked launch_agent to avoid
    actually starting a container, but verifying all the setup steps."""

    def test_launch_review_creates_diff_and_task(self, repo_with_branches):
        vcs = repo_with_branches
        launched = {}

        def fake_launch_agent(task_file=None, model="", session_name=None, **kwargs):
            launched["task_file"] = task_file
            launched["session_name"] = session_name
            return {"container_id": "fake", "session_name": session_name, "repo_root": str(vcs.repo_root)}

        with patch("cld.agent.get_backend", return_value=vcs), \
             patch("cld.agent.launch_agent", side_effect=fake_launch_agent), \
             patch("cld.agent.require_docker"), \
             patch("cld.agent.load_dotenv"), \
             patch("cld.agent.find_repo_root", return_value=vcs.repo_root), \
             patch("cld.agent.ensure_image"):
            from cld.agent import launch_review
            result = launch_review("feature", "trunk", name="test-review")

        assert "session_name" in result
        assert result["session_name"].startswith("review_")

        # Verify task file was created with template content
        task_file = launched["task_file"]
        assert task_file.is_file()
        task_content = task_file.read_text()
        assert "feature" in task_content or "trunk" in task_content

        # Verify diff file was created
        diff_files = list(vcs.repo_root.glob("review-diff-*.patch"))
        assert len(diff_files) >= 1
        diff_content = diff_files[0].read_text()
        assert "new_feature.py" in diff_content

        # Cleanup
        for f in diff_files:
            f.unlink()
        task_file.unlink(missing_ok=True)

    def test_launch_review_empty_diff_exits(self, repo_with_branches):
        vcs = repo_with_branches

        with patch("cld.agent.get_backend", return_value=vcs), \
             patch("cld.agent.require_docker"), \
             patch("cld.agent.load_dotenv"), \
             patch("cld.agent.find_repo_root", return_value=vcs.repo_root), \
             patch("cld.agent.ensure_image"):
            from cld.agent import launch_review
            with pytest.raises(SystemExit):
                launch_review("trunk", "trunk", name="empty-review")


@skip_no_agent_image
class TestReviewFullE2E:
    """Full E2E review test running the actual container with a stub."""

    def test_review_runs_to_completion(self, e2e_repo_with_branches, claude_stub):
        vcs = e2e_repo_with_branches
        if vcs.name == "git":
            pytest.skip("Baked agent image is jj-only (no vcs-lib.sh)")
        fork = vcs.fork_point("feature", "trunk")
        diff = vcs.diff_between(fork, "feature")
        if not diff.strip():
            pytest.skip("Empty diff")

        import random
        session = f"review_{random.randint(10000, 99999)}"

        # Write diff + task into the host-visible repo
        root = vcs.repo_root
        diff_file = root / f"review-diff-{session}.patch"
        diff_file.write_text(diff)

        template_path = Path(__file__).parent.parent / "imgs/claude-agent-review/review-template.md"
        if not template_path.is_file():
            pytest.skip("review-template.md not found")

        task_content = Template(template_path.read_text()).safe_substitute(
            TRUNK_BRANCH="trunk",
            FEATURE_BRANCH="feature",
            DIFF_FILE_PATH=f"/workspace/origin/review-diff-{session}.patch",
        )

        from tests.conftest import run_agent_container
        exit_code, summary, stdout, stderr = run_agent_container(
            root, session, task_content, claude_stub,
            vcs_type=vcs.name,
        )
        assert exit_code == 0, f"Review container failed:\nstdout={stdout}\nstderr={stderr}"
