"""Tests for cld.prompts: find_prompt_matches, resolve_prompt_ref, stage_persona_without_frontmatter."""

import pytest
from pathlib import Path

from cld.prompts import (
    find_prompt_matches,
    persona_resolve,
    resolve_prompt_ref,
    stage_persona_without_frontmatter,
    strip_frontmatter,
)


def _make(path: Path, content: str = "# Hello\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


class TestFindPromptMatches:
    def test_single_hit_in_prompts_root(self, tmp_path):
        _make(tmp_path / "prompts" / "todo-agent.md")
        matches = find_prompt_matches("todo-agent", tmp_path, tmp_path)
        assert len(matches) == 1
        assert matches[0].name == "todo-agent.md"

    def test_single_hit_in_personas_subdir(self, tmp_path):
        _make(tmp_path / "prompts" / "personas" / "architect.md")
        matches = find_prompt_matches("architect", tmp_path, tmp_path)
        assert len(matches) == 1

    def test_md_auto_append_bare_name(self, tmp_path):
        _make(tmp_path / "prompts" / "my-task.md")
        assert len(find_prompt_matches("my-task", tmp_path, tmp_path)) == 1

    def test_md_auto_append_explicit_md(self, tmp_path):
        _make(tmp_path / "prompts" / "my-task.md")
        assert len(find_prompt_matches("my-task.md", tmp_path, tmp_path)) == 1

    def test_explicit_extension_matches_only_exact_basename(self, tmp_path):
        _make(tmp_path / "prompts" / "my.txt")
        _make(tmp_path / "prompts" / "my.txt.md")
        matches = find_prompt_matches("my.txt", tmp_path, tmp_path)
        assert len(matches) == 1
        assert matches[0].name == "my.txt"

    def test_no_match_returns_empty(self, tmp_path):
        (tmp_path / "prompts").mkdir(parents=True)
        assert find_prompt_matches("nonexistent", tmp_path, tmp_path) == []

    def test_duplicate_in_two_subdirs_same_root(self, tmp_path):
        _make(tmp_path / "prompts" / "dup.md")
        _make(tmp_path / "prompts" / "personas" / "dup.md")
        matches = find_prompt_matches("dup", tmp_path, tmp_path)
        assert len(matches) == 2

    def test_duplicate_across_repo_and_cld_roots(self, tmp_path):
        repo = tmp_path / "repo"
        cld = tmp_path / "cld"
        _make(repo / "prompts" / "shared.md")
        _make(cld / "prompts" / "shared.md")
        matches = find_prompt_matches("shared", repo, cld)
        assert len(matches) == 2

    def test_same_root_not_double_counted(self, tmp_path):
        _make(tmp_path / "prompts" / "task.md")
        matches = find_prompt_matches("task", tmp_path, tmp_path)
        assert len(matches) == 1

    def test_missing_prompts_dir_returns_empty(self, tmp_path):
        assert find_prompt_matches("anything", tmp_path, tmp_path) == []


class TestResolvePromptRef:
    def test_task_kind_for_prompts_root_file(self, tmp_path):
        _make(tmp_path / "prompts" / "my-task.md")
        path, kind = resolve_prompt_ref("my-task", tmp_path, tmp_path)
        assert kind == "task"
        assert path.name == "my-task.md"

    def test_persona_kind_for_personas_subdir(self, tmp_path):
        _make(tmp_path / "prompts" / "personas" / "architect.md")
        path, kind = resolve_prompt_ref("architect", tmp_path, tmp_path)
        assert kind == "persona"
        assert path.name == "architect.md"

    def test_no_match_raises_file_not_found(self, tmp_path):
        (tmp_path / "prompts").mkdir(parents=True)
        with pytest.raises(FileNotFoundError, match="nope"):
            resolve_prompt_ref("nope", tmp_path, tmp_path)

    def test_file_not_found_message_includes_searched_roots(self, tmp_path):
        (tmp_path / "prompts").mkdir(parents=True)
        with pytest.raises(FileNotFoundError) as exc_info:
            resolve_prompt_ref("nope", tmp_path, tmp_path)
        assert str(tmp_path / "prompts") in str(exc_info.value)

    def test_duplicate_raises_value_error(self, tmp_path):
        _make(tmp_path / "prompts" / "dup.md")
        _make(tmp_path / "prompts" / "personas" / "dup.md")
        with pytest.raises(ValueError, match="dup"):
            resolve_prompt_ref("dup", tmp_path, tmp_path)

    def test_duplicate_error_lists_all_paths(self, tmp_path):
        p1 = _make(tmp_path / "prompts" / "dup.md")
        p2 = _make(tmp_path / "prompts" / "personas" / "dup.md")
        with pytest.raises(ValueError) as exc_info:
            resolve_prompt_ref("dup", tmp_path, tmp_path)
        msg = str(exc_info.value)
        assert str(p1) in msg
        assert str(p2) in msg


class TestStagePersonaWithoutFrontmatter:
    def test_strips_yaml_frontmatter(self, tmp_path):
        src = tmp_path / "persona.md"
        src.write_text("---\nname: foo\ndescription: bar\n---\n# Content\nHello\n")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()
        result = stage_persona_without_frontmatter(src, dst_dir)
        assert result.read_text() == "# Content\nHello\n"

    def test_no_frontmatter_passes_through_unchanged(self, tmp_path):
        src = tmp_path / "persona.md"
        src.write_text("# No frontmatter\nSome content\n")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()
        result = stage_persona_without_frontmatter(src, dst_dir)
        assert result.read_text() == "# No frontmatter\nSome content\n"

    def test_output_path_uses_src_name_in_dst_dir(self, tmp_path):
        src = tmp_path / "my-persona.md"
        src.write_text("content")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()
        result = stage_persona_without_frontmatter(src, dst_dir)
        assert result.name == "my-persona.md"
        assert result.parent == dst_dir


class TestStripFrontmatter:
    def test_strips_leading_block(self):
        text = "---\nname: x\ndescription: y\n---\n\n# Role\n\nbody\n"
        assert strip_frontmatter(text) == "# Role\n\nbody\n"

    def test_no_frontmatter_unchanged(self):
        text = "# Role\n\nbody\n"
        assert strip_frontmatter(text) == text

    def test_horizontal_rule_in_body_preserved(self):
        text = "---\nname: x\n---\n\n# Role\n\nfirst\n\n---\n\nsecond\n"
        assert strip_frontmatter(text) == "# Role\n\nfirst\n\n---\n\nsecond\n"

    def test_unterminated_block_unchanged(self):
        text = "---\nname: x\nno closing marker\n"
        assert strip_frontmatter(text) == text


class TestPersonaResolve:
    """A persona is a file name under prompts/personas/, never a path."""

    def _tree(self, tmp_path):
        d = tmp_path / "repo" / "prompts" / "personas"
        d.mkdir(parents=True)
        (d / "implementer.md").write_text("# impl\n")
        return tmp_path / "repo", tmp_path / "cld"

    def test_resolves_bare_name(self, tmp_path):
        repo, cld = self._tree(tmp_path)
        assert persona_resolve("implementer", repo, cld).name == "implementer.md"

    def test_resolves_name_with_extension(self, tmp_path):
        repo, cld = self._tree(tmp_path)
        assert persona_resolve("implementer.md", repo, cld).name == "implementer.md"

    def test_repo_wins_over_cld_root(self, tmp_path):
        repo, cld = self._tree(tmp_path)
        (cld / "prompts" / "personas").mkdir(parents=True)
        (cld / "prompts" / "personas" / "implementer.md").write_text("# other\n")
        assert persona_resolve("implementer", repo, cld).parent.parent.parent == repo

    def test_unknown_name_raises_file_not_found(self, tmp_path):
        repo, cld = self._tree(tmp_path)
        with pytest.raises(FileNotFoundError):
            persona_resolve("nope", repo, cld)

    @pytest.mark.parametrize("name", [
        "../../../../etc/hostname",
        "/etc/hostname",
        "sub/dir",
        "..",
        "",
    ])
    def test_path_like_names_rejected(self, tmp_path, name):
        """`cld task-agent start` mounts the resolved file into a container, and that
        command is reachable from inside a master through the broker."""
        repo, cld = self._tree(tmp_path)
        with pytest.raises(ValueError, match="invalid persona name"):
            persona_resolve(name, repo, cld)

    def test_traversal_to_a_real_file_still_rejected(self, tmp_path):
        repo, cld = self._tree(tmp_path)
        secret = tmp_path / "secret.txt"
        secret.write_text("token\n")
        with pytest.raises(ValueError):
            persona_resolve("../../../secret.txt", repo, cld)
