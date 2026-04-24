"""Tests for pure helpers in cld.loop."""

import pytest

from cld.loop import _format_duration, _parse_review_severity


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
