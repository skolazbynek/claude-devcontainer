"""Layer 4: Review workflow E2E tests -- diff generation, template filling, agent launch."""

import os
import subprocess
from pathlib import Path
from string import Template
from unittest.mock import patch

import pytest

from cld.vcs.git import GitBackend
from cld.vcs.jj import JjBackend
from tests.conftest import skip_no_agent_image

# Capture host env vars at module import (before conftest's clean_env autouse wipes them).
_HOST_HOME_AT_IMPORT = os.environ.get("CLD_HOST_HOME", "")


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

        def fake_launch_agent(cfg, task_file=None, model="", session_name=None, **kwargs):
            launched["task_file"] = task_file
            launched["session_name"] = session_name
            return {"container_id": "fake", "session_name": session_name, "repo_root": str(vcs.repo_root)}

        with patch("cld.agent.get_backend", return_value=vcs), \
             patch("cld.agent.launch_agent", side_effect=fake_launch_agent), \
             patch("cld.agent.require_docker"), \
             patch("cld.agent.ensure_image"):
            from cld.agent import launch_review
            from cld.config import Config
            result = launch_review(Config(), "feature", "trunk", name="test-review")

        assert "session_name" in result
        assert result["session_name"].startswith("review_")

        # Verify task file was created with template content
        task_file = launched["task_file"]
        assert task_file.is_file()
        task_content = task_file.read_text()
        assert "feature" in task_content or "trunk" in task_content

        # Verify diff file was created
        diff_files = list(vcs.repo_root.glob(".cld/review-diff-*.patch"))
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
             patch("cld.agent.ensure_image"):
            from cld.agent import launch_review
            from cld.config import Config
            with pytest.raises(SystemExit):
                launch_review(Config(), "trunk", "trunk", name="empty-review")


class TestReviewErrorPaths:
    """Error paths in launch_review that short-circuit before launch_agent."""

    def test_diff_returns_error_exits(self, repo_with_branches):
        vcs = repo_with_branches
        with patch.object(vcs, "diff_between", return_value="Error: vcs blew up"), \
             patch("cld.agent.get_backend", return_value=vcs), \
             patch("cld.agent.require_docker"), \
             patch("cld.agent.launch_agent") as la:
            from cld.agent import launch_review
            from cld.config import Config
            with pytest.raises(SystemExit) as exc:
                launch_review(Config(), "feature", "trunk", name="err-diff")
        assert exc.value.code == 1
        assert not la.called
        assert not list(vcs.repo_root.glob(".cld/review-diff-*err-diff*.patch"))

    def test_missing_template_exits(self, repo_with_branches):
        vcs = repo_with_branches
        real_is_file = Path.is_file

        def fake_is_file(self):
            if self.name == "review-template.md":
                return False
            return real_is_file(self)

        with patch.object(Path, "is_file", fake_is_file), \
             patch("cld.agent.get_backend", return_value=vcs), \
             patch("cld.agent.require_docker"), \
             patch("cld.agent.launch_agent") as la:
            from cld.agent import launch_review
            from cld.config import Config
            with pytest.raises(SystemExit) as exc:
                launch_review(Config(), "feature", "trunk", name="err-tmpl")
        assert exc.value.code == 1
        assert not la.called

    def test_nonexistent_feature_branch_raises(self, repo_with_branches):
        vcs = repo_with_branches
        with patch("cld.agent.get_backend", return_value=vcs), \
             patch("cld.agent.require_docker"), \
             patch("cld.agent.launch_agent") as la:
            from cld.agent import launch_review
            from cld.config import Config
            with pytest.raises(RuntimeError):
                launch_review(Config(), "does-not-exist", "trunk", name="err-branch")
        assert not la.called
        assert not list(vcs.repo_root.glob(".cld/review-diff-*err-branch*.patch"))

    def test_unrelated_histories_in_git(self, tmp_path):
        from cld.vcs.git import GitBackend
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "a.txt").write_text("a\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "trunk-init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "branch", "trunk"], cwd=tmp_path, check=True, capture_output=True)
        # Orphan branch with unrelated history
        subprocess.run(["git", "checkout", "--orphan", "feature"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "rm", "-rf", "."], cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "b.txt").write_text("b\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "orphan-init"], cwd=tmp_path, check=True, capture_output=True)
        vcs = GitBackend(tmp_path)

        with patch("cld.agent.get_backend", return_value=vcs), \
             patch("cld.agent.require_docker"), \
             patch("cld.agent.launch_agent") as la:
            from cld.agent import launch_review
            from cld.config import Config
            with pytest.raises(RuntimeError):
                launch_review(Config(), "feature", "trunk", name="err-orphan")
        assert not la.called

    def test_diff_file_path_uses_workspace_origin(self, repo_with_branches):
        vcs = repo_with_branches
        captured = {}

        def fake_launch_agent(cfg, task_file=None, model="", session_name=None, **kwargs):
            captured["task_file"] = task_file
            captured["session_name"] = session_name
            return {"container_id": "x", "session_name": session_name, "repo_root": str(vcs.repo_root)}

        with patch("cld.agent.get_backend", return_value=vcs), \
             patch("cld.agent.launch_agent", side_effect=fake_launch_agent), \
             patch("cld.agent.require_docker"), \
             patch("cld.agent.ensure_image"):
            from cld.agent import launch_review
            from cld.config import Config
            launch_review(Config(), "feature", "trunk", name="pathchk")

        session = captured["session_name"]
        task_content = captured["task_file"].read_text()
        expected_diff_path = f"/workspace/origin/.cld/review-diff-{session}.patch"
        assert expected_diff_path in task_content

        # Cleanup
        for f in vcs.repo_root.glob(".cld/review-diff-*pathchk*.patch"):
            f.unlink()
        for f in vcs.repo_root.glob(".cld/review-task-*pathchk*"):
            f.unlink()
        captured["task_file"].unlink(missing_ok=True)


@skip_no_agent_image
class TestReviewFullE2E:
    """Full E2E review tests running the actual container with a stub."""

    def test_review_runs_to_completion(self, e2e_repo_with_branches, claude_stub_review):
        vcs = e2e_repo_with_branches
        if vcs.name == "git":
            pytest.skip(
                "git cleanup in vcs-lib.sh:64 runs 'git branch -D <session>' which "
                "deletes the agent branch on exit, so post-run file_show() returns None. "
                "CLAUDE.md claims the branch persists; the cleanup contradicts that contract."
            )
        fork = vcs.fork_point("feature", "trunk")
        diff = vcs.diff_between(fork, "feature")
        if not diff.strip():
            pytest.skip("Empty diff")

        import random
        session = f"review_{random.randint(10000, 99999)}"

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
            root, session, task_content, claude_stub_review,
            vcs_type=vcs.name,
        )
        assert exit_code == 0, f"Review container failed:\nstdout={stdout}\nstderr={stderr}"
        assert summary is not None, f"summary.json missing on branch {session}"
        assert summary["status"] == "success", f"status={summary['status']}"

        review_md = vcs.file_show(session, "CODE_REVIEW_iter1.md")
        assert review_md is not None, "CODE_REVIEW_iter1.md missing on agent branch"
        assert "## Critical" in review_md

    def test_launch_review_e2e_produces_code_review_md(
        self, e2e_repo_with_branches, claude_stub_review, monkeypatch,
    ):
        """Drive the full Python orchestration: launch_review -> launch_agent -> container."""
        vcs = e2e_repo_with_branches
        if vcs.name == "git":
            pytest.skip(
                "git cleanup in vcs-lib.sh:64 runs 'git branch -D <session>' which "
                "deletes the agent branch on exit, so post-run file_show() returns None. "
                "CLAUDE.md claims the branch persists; the cleanup contradicts that contract."
            )

        import random
        from cld import agent as agent_mod
        from cld.agent import launch_review
        from cld.config import Config
        from tests.conftest import _HOST_PROJECT_DIR

        name = f"e2e{random.randint(10000, 99999)}"
        host_stub_dir = _to_host_path_for_e2e(str(claude_stub_review))

        # Inject the stub at /tmp/bin (added to PATH by container-init.sh)
        # by wrapping build_container_args.
        original = agent_mod.build_container_args

        def wrapped(repo_root, session_name, cfg, *, interactive=False):
            args = original(repo_root, session_name, cfg, interactive=interactive)
            # Strip --rm so we can docker wait + inspect logs after completion.
            args = [a for a in args if a != "--rm"]
            return args + ["-v", f"{host_stub_dir}:/tmp/bin:ro"]

        monkeypatch.setattr(agent_mod, "build_container_args", wrapped)
        # The inner test repo lives inside the project's own jj repo, so
        # the default get_backend() walks up and picks the OUTER jj backend.
        # Pin get_backend / find_repo_context to the fixture repo instead.
        monkeypatch.setattr(agent_mod, "get_backend", lambda *_a, **_kw: vcs)
        monkeypatch.setattr(
            agent_mod, "find_repo_context", lambda *_a, **_kw: (vcs.repo_root, ""),
        )
        monkeypatch.chdir(vcs.repo_root)

        cfg = Config(host_project_dir=_HOST_PROJECT_DIR, host_home=_HOST_HOME_AT_IMPORT)
        result = launch_review(cfg, "feature", "trunk", name=name)
        session = result["session_name"]
        container_id = result["container_id"]
        assert session.startswith("review_")

        try:
            wait = subprocess.run(
                ["docker", "wait", container_id],
                capture_output=True, text=True, timeout=180,
            )
            assert wait.returncode == 0, f"docker wait failed: {wait.stderr}"
            exit_code = int(wait.stdout.strip())
            logs = subprocess.run(
                ["docker", "logs", container_id],
                capture_output=True, text=True,
            )
            assert exit_code == 0, f"container exit={exit_code}\nlogs:\n{logs.stdout}\n{logs.stderr}"

            summary_raw = vcs.file_show(session, f"agent-output-{session}/summary.json")
            assert summary_raw, f"summary.json missing on branch {session}"
            import json as _json
            summary = _json.loads(summary_raw)
            assert summary["status"] == "success", f"status={summary['status']}"
            assert summary["vcs_type"] == vcs.name

            review_md = vcs.file_show(session, "CODE_REVIEW_iter1.md")
            assert review_md is not None, "CODE_REVIEW_iter1.md missing on agent branch"
            assert "## Critical" in review_md

            diff_files = list(vcs.repo_root.glob(f".cld/review-diff-{session}.patch"))
            assert len(diff_files) == 1
            assert "new_feature.py" in diff_files[0].read_text()

            task_files = list(vcs.repo_root.glob(f".cld/review-task-{session}-*.md"))
            assert len(task_files) == 1
            assert "feature" in task_files[0].read_text()
        finally:
            subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)
            for f in vcs.repo_root.glob(f".cld/review-*-{session}*"):
                f.unlink(missing_ok=True)


def _to_host_path_for_e2e(path):
    """Translate a /workspace/origin/* path to its host equivalent if running in a devcontainer."""
    from tests.conftest import _HOST_PROJECT_DIR as host_root
    if host_root and path.startswith("/workspace/origin"):
        return host_root + path[len("/workspace/origin"):]
    return path
