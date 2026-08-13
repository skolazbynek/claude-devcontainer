"""Prompt resolution: search prompts/ trees, strip frontmatter, stage files."""

import re
from pathlib import Path

from cld.log import get_logger

log = get_logger(__name__)

# A persona is a file *name* under prompts/personas/, never a path. Without this,
# `../../../../etc/passwd` resolves happily -- harmless for a human on the host, but
# `cld task-agent start` mounts the resolved file into a container, and that command is
# reachable from inside a master through the host broker (docs/design-task-agents.md §9),
# which exists precisely to keep a container off the host filesystem.
# Leading alphanumeric (same shape as a task slug) so `.`, `..` and dotfiles are out too.
_PERSONA_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def find_prompt_matches(name: str, repo_root: Path, cld_root: Path) -> list[Path]:
    """Return all files in prompts/ trees whose basename matches name.

    Appends .md when name has no extension. Deduplicates by resolved path.
    Skips cld_root when it equals repo_root to avoid double-counting.
    """
    has_ext = "." in Path(name).name
    candidates = [name] if has_ext else [name, name + ".md"]
    roots = [repo_root / "prompts"]
    if cld_root.resolve() != repo_root.resolve():
        roots.append(cld_root / "prompts")

    seen: set[Path] = set()
    matches: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for cand in candidates:
            for p in root.rglob(cand):
                if not p.is_file():
                    continue
                resolved = p.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    matches.append(p)
                    log.debug("prompt candidate: %s", p)
    return matches


def parse_description(path: Path) -> str:
    """The `description:` field of a prompt's frontmatter, or "" when there is none."""
    lines = path.read_text().splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("description:"):
            return line[len("description:"):].strip()
    return ""


def list_prompt_items(prompts_dir: Path) -> list[tuple[str, str]]:
    """``(ref, description)`` for every prompt under *prompts_dir*, recursively.

    The ref is the extension-less path relative to the tree, which is what an
    `@<name>` argument accepts. Shared by the host and container `cld prompts`.
    """
    return [
        (str(path.relative_to(prompts_dir).with_suffix("")), parse_description(path))
        for path in sorted(prompts_dir.rglob("*.md"))
    ]


def resolve_prompt_ref(name: str, repo_root: Path, cld_root: Path) -> Path:
    """Resolve @<name> to a path.

    Raises FileNotFoundError on no match, ValueError listing all paths on duplicates.
    """
    matches = find_prompt_matches(name, repo_root, cld_root)
    if not matches:
        searched = [str(repo_root / "prompts")]
        if cld_root.resolve() != repo_root.resolve():
            searched.append(str(cld_root / "prompts"))
        raise FileNotFoundError(
            f"Prompt '@{name}' not found. Searched: {', '.join(searched)}"
        )
    if len(matches) > 1:
        paths_str = "\n".join(f"  {p}" for p in matches)
        raise ValueError(f"Ambiguous prompt '@{name}' — multiple matches:\n{paths_str}")
    return matches[0]


def persona_resolve(name: str, repo_root: Path, cld_root: Path) -> Path:
    """Resolve a persona name to a file under prompts/personas/, repo first.

    Narrower than ``resolve_prompt_ref``: personas live in one directory per
    root, so this is an exact lookup rather than an rglob, and a name that
    matches a task prompt elsewhere in prompts/ is not a candidate.

    Raises ValueError for anything that isn't a bare file name (see the note on
    ``_PERSONA_NAME_RE``).
    """
    if not _PERSONA_NAME_RE.match(name):
        raise ValueError(
            f"invalid persona name {name!r}: a persona is a file name under "
            "prompts/personas/ (starting alphanumeric; letters, digits, dot, dash, underscore), not a path"
        )
    candidates = [name, f"{name}.md"] if not name.endswith(".md") else [name]
    for candidate in candidates:
        for base in (repo_root, cld_root):
            path = base / "prompts" / "personas" / candidate
            if path.is_file():
                return path
    raise FileNotFoundError(
        f"Persona '{name}' not found in {repo_root}/prompts/personas/ "
        f"or {cld_root}/prompts/personas/"
    )


def strip_frontmatter(text: str) -> str:
    """Drop a leading YAML frontmatter block, if there is one.

    cld personas carry frontmatter for discovery, but it is metadata, not prompt
    content: claude rejects a *system* prompt starting with `---`, and inside a
    composed kickoff prompt the block is noise. Text with no leading block, or an
    unterminated one, comes back unchanged.
    """
    if not text.lstrip().startswith("---"):
        return text
    stripped = text.lstrip()
    end = stripped.find("---", 3)
    if end == -1:
        return text
    return stripped[end + 3:].lstrip()


def stage_persona_without_frontmatter(src: Path, dst_dir: Path) -> Path:
    """Strip YAML frontmatter from src and write the result into dst_dir.

    Returns the staged path.
    """
    dst = dst_dir / src.name
    dst.write_text(strip_frontmatter(src.read_text()))
    return dst
