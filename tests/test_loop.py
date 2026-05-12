"""Tests for cld.loop helpers and run_loop orchestration."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cld.config import Config
from cld.loop import _format_duration, _parse_review_severity, run_loop


class TestParseReviewSeverity:
    def test_counts_findings_by_section(self):
        content = (
            "## Critical\n"
            "### Missing null check\n"
            "### Broken auth\n"
            "## Major\n"
            "### Perf issue\n"
            "## Minor\n"
            "### Style nit\n"
            "### Typo\n"
            "### Another nit\n"
        )
        assert _parse_review_severity(content) == {
            "critical": 2, "major": 1, "minor": 3,
        }

    def test_empty_sections(self):
        content = "## Critical\n\n## Major\n\n## Minor\n"
        assert _parse_review_severity(content) == {
            "critical": 0, "major": 0, "minor": 0,
        }

    def test_ignores_findings_outside_sections(self):
        content = "### orphan\n## Critical\n### real\n"
        assert _parse_review_severity(content) == {
            "critical": 1, "major": 0, "minor": 0,
        }

    def test_unrelated_section_resets_current(self):
        content = (
            "## Critical\n### a\n"
            "## Summary\n### b\n"
            "## Major\n### c\n"
        )
        assert _parse_review_severity(content) == {
            "critical": 1, "major": 1, "minor": 0,
        }


class TestFormatDuration:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "0m00s"),
            (5, "0m05s"),
            (65, "1m05s"),
            (125, "2m05s"),
            (3599, "59m59s"),
        ],
        ids=["zero", "seconds-only", "one-minute-one-change", "simple", "just-under-hour"],
    )
    def test_formatting(self, seconds, expected):
        assert _format_duration(seconds) == expected


# --- run_loop orchestration tests ---------------------------------------------


_CLEAN_REVIEW = "## Critical\n\n## Major\n\n## Minor\n### tiny nit\n"
_DIRTY_REVIEW = "## Critical\n### Real bug\n\n## Major\n\n## Minor\n"


def _make_fake_agent_commit(vcs, revision, session_name, files):
    """Create a commit + branch named session_name on top of revision, detached
    (so for git no other branch tip advances). Mimics real agent commit behavior."""
    sha = vcs.resolve_revision(revision)
    vcs.new_change(sha)  # jj: new change on top of sha; git: detached checkout of sha
    for fname, content in files.items():
        target = vcs.repo_root / fname
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    vcs.commit(f"fake {session_name}")
    tip_alias = "@-" if vcs.name == "jj" else "HEAD"
    vcs.create_branch(session_name, tip_alias)


def _install_loop_mocks(monkeypatch, vcs, wait_results):
    la_mock = MagicMock()
    wait_mock = MagicMock(side_effect=list(wait_results))
    monkeypatch.setattr("cld.loop.launch_agent", la_mock)
    monkeypatch.setattr("cld.loop._wait_for_agent", wait_mock)
    monkeypatch.setattr("cld.loop.get_backend", lambda *_a, **_kw: vcs)
    monkeypatch.setattr("cld.loop._read_agent_cost", lambda *a, **k: 0.0)
    monkeypatch.chdir(vcs.repo_root)
    return la_mock, wait_mock


def _bookmark_names(vcs):
    """Return the set of bookmark/branch names at line-start, robust against
    descriptions or remote-tracking entries containing the same substring."""
    names = set()
    for line in vcs.list_branches().splitlines():
        s = line.strip().lstrip("* ")
        token = s.split(":")[0].split()[0]
        # Skip remote-tracking entries like 'origin/main' or refs with '/'
        if "/" in token:
            continue
        names.add(token)
    return names


def _loop_branch_name(vcs):
    """Find the single 'loop_*' branch left after run_loop completes."""
    for name in _bookmark_names(vcs):
        if name.startswith("loop_") and not name.startswith("loop_t_"):
            return name
    return None


class TestRunLoop:
    def test_single_iteration_clean_exits(self, vcs_repo, monkeypatch, capsys):
        vcs = vcs_repo

        wait_results = [{"status": "success"}, {"status": "success"}]
        la_mock, _ = _install_loop_mocks(monkeypatch, vcs, wait_results)

        def fake(cfg, *, revision="", session_name=None, **kw):
            files = {}
            if "_impl1" in session_name:
                files = {"src.py": "def f(): pass\n"}
            elif "_review1" in session_name:
                files = {"CODE_REVIEW_iter1.md": _CLEAN_REVIEW}
            _make_fake_agent_commit(vcs, revision, session_name, files)
            return {"container_id": f"fake-{session_name}", "session_name": session_name, "repo_root": str(vcs.repo_root)}

        la_mock.side_effect = fake

        run_loop(Config(), task_file=None, inline_prompt="Implement foo",
                 name="t", max_iterations=3)

        loop_branch = _loop_branch_name(vcs)
        assert loop_branch
        assert vcs.file_show(loop_branch, "src.py") == "def f(): pass\n"
        assert vcs.file_show(loop_branch, "CODE_REVIEW_iter1.md") == _CLEAN_REVIEW

        names = _bookmark_names(vcs)
        assert f"{loop_branch}_impl1" not in names
        assert f"{loop_branch}_review1" not in names

        out = capsys.readouterr().out
        assert "clean review" in out
        assert "1/3 iterations" in out
        assert la_mock.call_count == 2

    def test_multi_iteration_clean_after_retry(self, vcs_repo, monkeypatch, capsys):
        vcs = vcs_repo

        wait_results = [{"status": "success"}] * 4
        la_mock, _ = _install_loop_mocks(monkeypatch, vcs, wait_results)

        impl2_snapshot = {}

        def fake(cfg, *, revision="", session_name=None, task_file=None, inline_prompt=None, **kw):
            files = {}
            if "_impl1" in session_name:
                files = {"src.py": "v1\n"}
            elif "_review1" in session_name:
                files = {"CODE_REVIEW_iter1.md": _DIRTY_REVIEW}
            elif "_impl2" in session_name:
                impl2_snapshot["task_file"] = task_file
                impl2_snapshot["inline_prompt"] = inline_prompt
                impl2_snapshot["content"] = Path(task_file).read_text() if task_file else None
                files = {"src.py": "v2\n"}
            elif "_review2" in session_name:
                files = {"CODE_REVIEW_iter2.md": _CLEAN_REVIEW}
            _make_fake_agent_commit(vcs, revision, session_name, files)
            return {"container_id": f"fake-{session_name}", "session_name": session_name, "repo_root": str(vcs.repo_root)}

        la_mock.side_effect = fake

        run_loop(Config(), task_file=None, inline_prompt="Implement foo",
                 name="t", max_iterations=3)

        loop_branch = _loop_branch_name(vcs)
        assert loop_branch
        assert vcs.file_show(loop_branch, "src.py") == "v2\n"
        assert vcs.file_show(loop_branch, "CODE_REVIEW_iter1.md") == _DIRTY_REVIEW
        assert vcs.file_show(loop_branch, "CODE_REVIEW_iter2.md") == _CLEAN_REVIEW

        assert impl2_snapshot["inline_prompt"] is None
        assert impl2_snapshot["task_file"] is not None
        assert "Implement foo" in impl2_snapshot["content"]
        assert "# Review Findings (Iteration 1)" in impl2_snapshot["content"]
        assert "Real bug" in impl2_snapshot["content"]

        out = capsys.readouterr().out
        assert "clean review" in out
        assert "2/3 iterations" in out
        assert la_mock.call_count == 4

    def test_max_iterations_exhausted(self, vcs_repo, monkeypatch, capsys):
        vcs = vcs_repo

        wait_results = [{"status": "success"}] * 4
        la_mock, _ = _install_loop_mocks(monkeypatch, vcs, wait_results)

        def fake(cfg, *, revision="", session_name=None, **kw):
            files = {}
            if "_impl" in session_name:
                files = {"src.py": f"// from {session_name}\n"}
            elif "_review" in session_name:
                num = session_name.rsplit("_review", 1)[1]
                files = {f"CODE_REVIEW_iter{num}.md": _DIRTY_REVIEW}
            _make_fake_agent_commit(vcs, revision, session_name, files)
            return {"container_id": f"fake-{session_name}", "session_name": session_name, "repo_root": str(vcs.repo_root)}

        la_mock.side_effect = fake

        run_loop(Config(), task_file=None, inline_prompt="Implement foo",
                 name="t", max_iterations=2)

        assert la_mock.call_count == 4
        loop_branch = _loop_branch_name(vcs)
        assert vcs.file_show(loop_branch, "CODE_REVIEW_iter2.md") == _DIRTY_REVIEW

        out = capsys.readouterr().out
        assert "max iterations reached" in out
        assert "2/2 iterations" in out

    def test_implementer_failure_breaks(self, vcs_repo, monkeypatch, capsys):
        vcs = vcs_repo

        wait_results = [{"status": "failed", "error": "boom"}]
        la_mock, _ = _install_loop_mocks(monkeypatch, vcs, wait_results)

        def fake(cfg, *, revision="", session_name=None, **kw):
            _make_fake_agent_commit(vcs, revision, session_name, {"partial.py": "incomplete\n"})
            return {"container_id": "fake", "session_name": session_name, "repo_root": str(vcs.repo_root)}

        la_mock.side_effect = fake

        run_loop(Config(), task_file=None, inline_prompt="Implement foo",
                 name="t", max_iterations=3)

        assert la_mock.call_count == 1
        # iter-1 failure leaves no useful state on the loop branch; it is cleaned up.
        assert _loop_branch_name(vcs) is None

        out = capsys.readouterr().out
        assert "implementer failed (iteration 1)" in out

    def test_reviewer_no_output_breaks(self, vcs_repo, monkeypatch, capsys):
        vcs = vcs_repo

        wait_results = [{"status": "success"}, {"status": "success"}]
        la_mock, _ = _install_loop_mocks(monkeypatch, vcs, wait_results)

        def fake(cfg, *, revision="", session_name=None, **kw):
            files = {}
            if "_impl1" in session_name:
                files = {"src.py": "v1\n"}
            elif "_review1" in session_name:
                files = {"notes.txt": "agent went off-task\n"}
            _make_fake_agent_commit(vcs, revision, session_name, files)
            return {"container_id": "fake", "session_name": session_name, "repo_root": str(vcs.repo_root)}

        la_mock.side_effect = fake

        run_loop(Config(), task_file=None, inline_prompt="Implement foo",
                 name="t", max_iterations=3)

        loop_branch = _loop_branch_name(vcs)
        assert vcs.file_show(loop_branch, "notes.txt") == "agent went off-task\n"
        assert vcs.file_show(loop_branch, "src.py") == "v1\n"

        assert f"{loop_branch}_review1" not in _bookmark_names(vcs)

        out = capsys.readouterr().out
        assert "no review output (iteration 1)" in out

    def test_keyboard_interrupt_in_impl(self, vcs_repo, monkeypatch, capsys):
        vcs = vcs_repo
        (vcs.repo_root / ".cld").mkdir(exist_ok=True)
        leftover = vcs.repo_root / ".cld" / "loop-impl-leftover.md"
        leftover.write_text("stale\n")

        la_mock, _ = _install_loop_mocks(monkeypatch, vcs, [])
        la_mock.side_effect = KeyboardInterrupt()

        run_loop(Config(), task_file=None, inline_prompt="Implement foo",
                 name="t", max_iterations=3)

        assert not leftover.exists()

        out = capsys.readouterr().out
        assert "interrupted" in out
