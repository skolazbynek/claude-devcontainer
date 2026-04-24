"""Tests for pure helpers in cld.mcp.orchestrator."""

from pathlib import Path

import pytest

from cld.mcp.orchestrator import _is_host_visible, _parse_description


class TestIsHostVisible:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("/workspace/origin/foo", True),
            ("/workspace/current/foo", True),
            ("/workspace/other", False),
            ("/tmp/foo", False),
            ("/home/user/foo", False),
        ],
        ids=["origin", "current", "other-workspace", "tmp", "home"],
    )
    def test_host_visibility(self, path, expected):
        assert _is_host_visible(Path(path)) is expected


class TestParseDescription:
    def test_extracts_from_frontmatter(self, tmp_path):
        f = tmp_path / "p.md"
        f.write_text("---\nname: fix\ndescription: Fix bug\n---\nbody\n")
        assert _parse_description(f) == "Fix bug"

    def test_no_frontmatter_returns_empty(self, tmp_path):
        f = tmp_path / "p.md"
        f.write_text("just body\n")
        assert _parse_description(f) == ""

    def test_unterminated_frontmatter_returns_empty(self, tmp_path):
        f = tmp_path / "p.md"
        f.write_text("---\ndescription: x\n")
        assert _parse_description(f) == ""

    def test_missing_description_key_returns_empty(self, tmp_path):
        f = tmp_path / "p.md"
        f.write_text("---\nname: fix\n---\nbody\n")
        assert _parse_description(f) == ""

    def test_missing_file_returns_empty(self, tmp_path):
        assert _parse_description(tmp_path / "nonexistent") == ""

    def test_case_insensitive_key(self, tmp_path):
        f = tmp_path / "p.md"
        f.write_text("---\nDescription: Capitalized\n---\n")
        assert _parse_description(f) == "Capitalized"
