"""Tests for cld.prompts: ref resolution, classification and brief composition."""

import pytest
from pathlib import Path

from cld.prompts import (
    MAX_PROMPT_REFS,
    compose_brief,
    find_prompt_matches,
    prompt_kind,
    resolve_prompt_arg,
    resolve_prompt_args,
    resolve_prompt_ref,
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
        _make(tmp_path / "prompts" / "tasks" / "dup.md")
        _make(tmp_path / "prompts" / "personas" / "dup.md")
        with pytest.raises(ValueError, match="dup"):
            resolve_prompt_ref("dup", tmp_path, tmp_path)

    def test_duplicate_error_lists_all_paths(self, tmp_path):
        p1 = _make(tmp_path / "prompts" / "tasks" / "dup.md")
        p2 = _make(tmp_path / "prompts" / "personas" / "dup.md")
        with pytest.raises(ValueError) as exc_info:
            resolve_prompt_ref("dup", tmp_path, tmp_path)
        msg = str(exc_info.value)
        assert str(p1) in msg
        assert str(p2) in msg


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


class TestRefIsAPathUnderPrompts:
    """`@personas/architect` -- the ref form the interface documents."""

    def _tree(self, tmp_path):
        (tmp_path / "prompts" / "personas").mkdir(parents=True)
        (tmp_path / "prompts" / "personas" / "architect.md").write_text("# arch\n")
        (tmp_path / "prompts" / "my-task.md").write_text("# task\n")
        return tmp_path

    def test_relative_path_resolves(self, tmp_path):
        repo = self._tree(tmp_path)
        path, kind = resolve_prompt_ref("personas/architect", repo, repo)
        assert path.name == "architect.md"
        assert kind == "persona"

    def test_relative_path_with_extension(self, tmp_path):
        repo = self._tree(tmp_path)
        assert resolve_prompt_ref("personas/architect.md", repo, repo)[0].name == "architect.md"

    def test_repo_root_wins_over_cld_root(self, tmp_path):
        repo, cld = tmp_path / "repo", tmp_path / "cld"
        for base in (repo, cld):
            (base / "prompts" / "personas").mkdir(parents=True)
            (base / "prompts" / "personas" / "architect.md").write_text(f"# {base.name}\n")
        path, _ = resolve_prompt_ref("personas/architect", repo, cld)
        assert path.read_text() == "# repo\n"

    def test_exact_path_beats_a_basename_match_elsewhere(self, tmp_path):
        repo = self._tree(tmp_path)
        (repo / "prompts" / "personas" / "my-task.md").write_text("# other\n")
        # ambiguous by basename, unambiguous as a path
        path, kind = resolve_prompt_ref("my-task", repo, repo)
        assert path.parent.name == "prompts" and kind == "task"

    @pytest.mark.parametrize("ref", [
        "../../../etc/hostname",
        "personas/../../../../etc/hostname",
    ])
    def test_escaping_the_prompts_tree_is_refused(self, tmp_path, ref):
        """The broker resolves refs host-side and composes what they name into a
        container's brief, so an escaping ref would read arbitrary host files."""
        repo = self._tree(tmp_path)
        with pytest.raises(ValueError, match="escapes the prompts tree"):
            resolve_prompt_ref(ref, repo, repo)


class TestPromptKind:
    @pytest.mark.parametrize("rel,expected", [
        ("prompts/personas/architect.md", "persona"),
        ("prompts/personas/sub/architect.md", "persona"),
        ("prompts/todo-agent.md", "task"),
        ("tasks/thing.md", "task"),
    ])
    def test_classification_is_by_location(self, rel, expected):
        assert prompt_kind(Path("/repo") / rel) == expected


class TestResolvePromptArg:
    def test_at_ref_goes_through_the_prompts_tree(self, tmp_path):
        (tmp_path / "prompts").mkdir()
        (tmp_path / "prompts" / "t.md").write_text("# t\n")
        assert resolve_prompt_arg("@t", tmp_path, tmp_path)[0].name == "t.md"

    def test_plain_path_is_used_as_is(self, tmp_path):
        f = tmp_path / "task.md"
        f.write_text("# t\n")
        path, kind = resolve_prompt_arg(str(f), tmp_path, tmp_path)
        assert path == f and kind == "task"

    def test_missing_plain_path_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="not found"):
            resolve_prompt_arg(str(tmp_path / "nope.md"), tmp_path, tmp_path)

    def test_order_is_preserved(self, tmp_path):
        (tmp_path / "prompts").mkdir()
        for n in ("a", "b"):
            (tmp_path / "prompts" / f"{n}.md").write_text(f"# {n}\n")
        resolved = resolve_prompt_args(["@b", "@a"], tmp_path, tmp_path)
        assert [p.stem for p, _ in resolved] == ["b", "a"]

    def test_too_many_refs_refused(self, tmp_path):
        (tmp_path / "prompts").mkdir()
        (tmp_path / "prompts" / "a.md").write_text("# a\n")
        args = ["@a"] * (MAX_PROMPT_REFS + 1)
        with pytest.raises(ValueError, match="too many prompt refs"):
            resolve_prompt_args(args, tmp_path, tmp_path)


class TestComposeBrief:
    def _files(self, tmp_path, *bodies):
        paths = []
        for i, body in enumerate(bodies):
            f = tmp_path / f"{i}.md"
            f.write_text(body)
            paths.append(f)
        return paths

    def test_blocks_joined_in_order(self, tmp_path):
        paths = self._files(tmp_path, "# One\n", "# Two\n")
        assert compose_brief(paths) == "# One\n\n# Two\n"

    def test_inline_comes_last(self, tmp_path):
        paths = self._files(tmp_path, "# One\n")
        assert compose_brief(paths, "then this") == "# One\n\nthen this\n"

    def test_frontmatter_stripped_from_every_block(self, tmp_path):
        paths = self._files(tmp_path, "---\nname: a\n---\n# One\n", "---\nname: b\n---\n# Two\n")
        brief = compose_brief(paths)
        assert "name:" not in brief
        assert brief == "# One\n\n# Two\n"

    def test_inline_is_verbatim(self, tmp_path):
        """User text, not a template: a $VAR has to survive into the container."""
        assert "$DELIVERABLE_BRANCH" in compose_brief([], "use $DELIVERABLE_BRANCH")

    def test_no_headers_invented_between_blocks(self, tmp_path):
        paths = self._files(tmp_path, "# One\n")
        assert "Additional Instructions" not in compose_brief(paths, "more")

    def test_inline_only(self):
        assert compose_brief([], "just this") == "just this\n"

    def test_empty_blocks_dropped(self, tmp_path):
        paths = self._files(tmp_path, "# One\n", "   \n")
        assert compose_brief(paths) == "# One\n"
