"""Prompt refs: resolve them, classify them, compose them into one brief.

Every command that takes a persona or a task file takes an ordered list of **prompt
refs** instead -- `@<path-under-prompts>` or a filesystem path, personas and task files
interchangeable -- plus one inline description. The refs are composed host-side, in
argument order, into a single brief. See docs/design-prompt-chaining.md.
"""

from collections.abc import Sequence
from pathlib import Path

from cld.log import get_logger

log = get_logger(__name__)

# A guard against `cld run prompts/*` glob accidents, not a real ceiling.
MAX_PROMPT_REFS = 8

PERSONA_DIR = "personas"


def _prompt_roots(repo_root: Path, cld_root: Path) -> list[Path]:
    """The prompts trees to search, repo first. Skips cld_root when it is repo_root."""
    roots = [repo_root / "prompts"]
    if cld_root.resolve() != repo_root.resolve():
        roots.append(cld_root / "prompts")
    return roots


def find_prompt_matches(name: str, repo_root: Path, cld_root: Path) -> list[Path]:
    """Return all files in prompts/ trees whose basename matches name.

    Appends .md when name has no extension. Deduplicates by resolved path.
    Skips cld_root when it equals repo_root to avoid double-counting.
    """
    has_ext = "." in Path(name).name
    candidates = [name] if has_ext else [name, name + ".md"]

    seen: set[Path] = set()
    matches: list[Path] = []
    for root in _prompt_roots(repo_root, cld_root):
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


def prompt_kind(path: Path) -> str:
    """``"persona"`` when *path* sits under a personas/ directory, else ``"task"``.

    Display metadata only (the roster's Persona column, the launch banner): the two
    kinds are interchangeable as prompt content, which is the point of the interface.
    """
    return "persona" if PERSONA_DIR in path.parts else "task"


def _exact_under_root(ref: str, root: Path) -> Path | None:
    """Resolve *ref* as a path relative to *root*, or None.

    ``.md`` is appended when the ref has no extension. The resolved path must stay
    inside *root*: `cld task-agent start` is reachable from inside a container through
    the broker, which resolves refs on the *host* and mounts what they name, so a ref
    that escapes the prompts tree would be a container reading arbitrary host files.
    """
    candidates = [ref] if "." in Path(ref).name else [ref, f"{ref}.md"]
    for cand in candidates:
        path = (root / cand).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError(
                f"prompt ref '@{ref}' escapes the prompts tree at {root} -- a ref names "
                "a file under prompts/, not a path"
            )
        if path.is_file():
            return path
    return None


def resolve_prompt_ref(ref: str, repo_root: Path, cld_root: Path) -> tuple[Path, str]:
    """Resolve a ref (without its leading ``@``) to ``(path, kind)``.

    Two steps: the ref as a path relative to each prompts root (`personas/architect`),
    then -- failing that -- a basename search anywhere in the trees (`architect`).

    Raises FileNotFoundError on no match, ValueError listing every path on an ambiguous
    basename or on a ref that escapes the prompts tree.
    """
    roots = _prompt_roots(repo_root, cld_root)
    for root in roots:
        hit = _exact_under_root(ref, root)
        if hit:
            return hit, prompt_kind(hit)

    # Per root, repo first: a repo that ships its own `implementer` shadows cld's rather
    # than colliding with it. Ambiguity inside one root is still an error -- there the
    # ref genuinely names two different prompts.
    for root in roots:
        matches = find_prompt_matches(ref, root.parent, root.parent)
        if len(matches) > 1:
            paths_str = "\n".join(f"  {p}" for p in matches)
            raise ValueError(f"Ambiguous prompt '@{ref}' — multiple matches:\n{paths_str}")
        if matches:
            return matches[0], prompt_kind(matches[0])
    raise FileNotFoundError(
        f"Prompt '@{ref}' not found. Searched: {', '.join(str(r) for r in roots)}"
    )


def resolve_prompt_arg(arg: str, repo_root: Path, cld_root: Path) -> tuple[Path, str]:
    """Resolve one positional prompt argument: ``@<ref>`` or a filesystem path."""
    if arg.startswith("@"):
        return resolve_prompt_ref(arg[1:], repo_root, cld_root)
    path = Path(arg)
    if not path.is_file():
        raise FileNotFoundError(f"Prompt file not found: {arg}")
    return path, prompt_kind(path.resolve())


def resolve_prompt_args(
    args: Sequence[str], repo_root: Path, cld_root: Path
) -> list[tuple[Path, str]]:
    """Resolve every positional prompt argument, in order. Enforces MAX_PROMPT_REFS."""
    if len(args) > MAX_PROMPT_REFS:
        raise ValueError(
            f"too many prompt refs ({len(args)}, max {MAX_PROMPT_REFS}): "
            f"{', '.join(args[:3])}, ... -- did a glob expand?"
        )
    return [resolve_prompt_arg(a, repo_root, cld_root) for a in args]


def strip_frontmatter(text: str) -> str:
    """Drop a leading YAML frontmatter block, if there is one.

    cld prompts carry frontmatter for discovery, but it is metadata, not prompt
    content: claude rejects a *system* prompt starting with `---`, and inside a
    composed brief the block is noise. Text with no leading block, or an unterminated
    one, comes back unchanged.
    """
    if not text.lstrip().startswith("---"):
        return text
    stripped = text.lstrip()
    end = stripped.find("---", 3)
    if end == -1:
        return text
    return stripped[end + 3:].lstrip()


def compose_brief(paths: Sequence[Path], inline: str = "") -> str:
    """Join the resolved refs, in order, then the inline description last.

    No headers between blocks: with N ordered blocks the order *is* the semantics,
    which is what the old "## Additional Instructions" / "TASK INSTRUCTIONS:" glue was
    standing in for. Frontmatter is stripped from every block; the inline text is
    appended verbatim, so a `$VAR` in a task description survives.
    """
    blocks = [strip_frontmatter(p.read_text()).strip() for p in paths]
    if inline:
        blocks.append(inline)
    return "\n\n".join(b for b in blocks if b) + "\n"


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

    The ref is the extension-less path relative to the tree, i.e. exactly what an
    `@<ref>` argument accepts. Shared by the host and container `cld prompts`.
    """
    return [
        (str(path.relative_to(prompts_dir).with_suffix("")), parse_description(path))
        for path in sorted(prompts_dir.rglob("*.md"))
    ]
