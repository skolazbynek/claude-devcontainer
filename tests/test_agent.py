"""Tests for cld.agent._build_task_file."""

import pytest

from cld.agent import _build_task_file


class TestBuildTaskFile:
    def test_task_file_only_returns_resolved_path(self, tmp_path):
        task = tmp_path / "task.md"
        task.write_text("do the thing")
        assert _build_task_file(task, None, tmpdir=tmp_path) == task.resolve()

    def test_inline_only_writes_temp_file(self, tmp_path):
        path = _build_task_file(None, "inline task", tmpdir=tmp_path)
        assert path.parent == tmp_path
        assert path.read_text() == "inline task"

    def test_both_concatenates_with_heading(self, tmp_path):
        task = tmp_path / "task.md"
        task.write_text("base task")
        path = _build_task_file(task, "extra instructions", tmpdir=tmp_path)
        content = path.read_text()
        assert content.startswith("base task")
        assert "## Additional Instructions" in content
        assert content.endswith("extra instructions\n")

    def test_neither_exits(self):
        with pytest.raises(SystemExit):
            _build_task_file(None, None)
