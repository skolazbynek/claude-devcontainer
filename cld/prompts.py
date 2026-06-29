"""Prompt resolution: search prompts/ trees, strip frontmatter, stage files."""

from pathlib import Path

from cld.log import get_logger

log = get_logger(__name__)


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


def stage_persona_without_frontmatter(src: Path, dst_dir: Path) -> Path:
    """Strip YAML frontmatter from src and write the result into dst_dir.

    Claude rejects system prompts starting with `---`; cld personas use
    frontmatter for discovery. Returns the staged path.
    """
    text = src.read_text()
    if text.lstrip().startswith("---"):
        stripped = text.lstrip()
        end = stripped.find("---", 3)
        if end != -1:
            text = stripped[end + 3:].lstrip()
    dst = dst_dir / src.name
    dst.write_text(text)
    return dst
