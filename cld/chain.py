"""In-memory representation of a parsed chain file."""

from __future__ import annotations

import re
import sys
import time
import yaml
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Iterator

from cld.agent import launch_agent
from cld.agent_runtime import wait_for_agent, read_agent_cost, format_duration
from cld.config import Config
from cld.docker import cld_tmpdir, find_repo_root
from cld.vcs import get_backend


def _dbg(cfg: Config, msg: str) -> None:
    if cfg.debug:
        print(f"[chain] {msg}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class ChainStep:
    name: str
    persona: str
    model: str = ""
    prompt: str = ""
    output: str = ""
    inputs: tuple[str, ...] = ()
    timeout: int = 0


@dataclass(frozen=True)
class ParallelGroup:
    siblings: tuple[ChainStep, ...]


@dataclass(frozen=True)
class ChainDefaults:
    model: str = "sonnet"
    timeout: int = 0


@dataclass(frozen=True)
class Chain:
    name: str
    description: str
    defaults: ChainDefaults
    steps: tuple[ChainStep | ParallelGroup, ...]


def is_parallel(item) -> bool:
    return isinstance(item, ParallelGroup)


def iter_steps(chain: Chain) -> Iterator[ChainStep]:
    for s in chain.steps:
        if isinstance(s, ParallelGroup):
            yield from s.siblings
        else:
            yield s


def load_chain(path: Path) -> Chain:
    """Parse a YAML chain file into a Chain dataclass.

    Performs only structural parsing; semantic validation is in `validate_chain`.
    """
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ValueError(f"Failed to parse {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level must be a YAML mapping")
    return _build_chain(data, path)


def _build_chain(data: dict, path: Path) -> Chain:
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{path}: missing required string 'name'")
    description = data.get("description", "") or ""
    defaults_raw = data.get("defaults", {}) or {}
    defaults = ChainDefaults(
        model=defaults_raw.get("model", "sonnet") or "sonnet",
        timeout=int(defaults_raw.get("timeout", 0) or 0),
    )
    steps_raw = data.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ValueError(f"{path}: 'steps' must be a non-empty list")
    steps = tuple(_build_step_or_group(s, path) for s in steps_raw)
    return Chain(name=name, description=description, defaults=defaults, steps=steps)


def _build_step_or_group(raw, path):
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: each step must be a mapping")
    if "parallel" in raw:
        if set(raw.keys()) - {"parallel"}:
            raise ValueError(f"{path}: parallel step cannot have sibling keys")
        siblings_raw = raw["parallel"]
        if not isinstance(siblings_raw, list) or not siblings_raw:
            raise ValueError(f"{path}: parallel block must be a non-empty list")
        return ParallelGroup(
            siblings=tuple(_build_step(s, path) for s in siblings_raw)
        )
    return _build_step(raw, path)


def _build_step(raw, path) -> ChainStep:
    if "parallel" in raw:
        raise ValueError(f"{path}: nested 'parallel' is not allowed")
    name = raw.get("name")
    persona = raw.get("persona")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{path}: step missing required 'name'")
    if not isinstance(persona, str) or not persona:
        raise ValueError(f"{path}: step '{name}' missing required 'persona'")
    inputs = raw.get("inputs", []) or []
    return ChainStep(
        name=name,
        persona=persona,
        model=str(raw.get("model", "") or ""),
        prompt=str(raw.get("prompt", "") or ""),
        output=str(raw.get("output", "") or ""),
        inputs=tuple(inputs),
        timeout=int(raw.get("timeout", 0) or 0),
    )


def persona_resolve(name: str, repo_root: Path, cld_root: Path) -> Path:
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


def _stage_persona_without_frontmatter(
    persona_path: Path, chain: Chain, step: ChainStep, repo_root: Path,
) -> Path:
    """Strip YAML frontmatter from a persona and stage it under .cld/.

    Claude's CLI rejects system prompts that start with `---` (it tries to
    parse the frontmatter as YAML). The cld personas use frontmatter for
    `cld chain list` discovery; we strip it before passing as system prompt.
    """
    text = persona_path.read_text()
    if text.lstrip().startswith("---"):
        stripped = text.lstrip()
        end = stripped.find("---", 3)
        if end != -1:
            text = stripped[end + 3:].lstrip()
    staged = cld_tmpdir(repo_root) / f"persona-{chain.name}-{step.name}.md"
    staged.write_text(text)
    return staged


_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def validate_chain(chain: Chain, repo_root: Path, cld_root: Path) -> None:
    """Validate a parsed Chain. Raises ValueError on the first problem.

    Checks:
    - Chain name matches [a-zA-Z0-9_-]+.
    - All step names match the same regex and are unique within the chain.
    - Every persona resolves to a file (uses persona_resolve).
    - `inputs:` reference only step names that appear EARLIER in the chain
      (no forward references, no parallel siblings referencing each other).
    """
    if not _NAME_RE.match(chain.name):
        raise ValueError(f"chain name '{chain.name}' must match {_NAME_RE.pattern}")

    seen_names: set[str] = set()
    declared_so_far: set[str] = set()

    for item in chain.steps:
        if isinstance(item, ParallelGroup):
            group_names: set[str] = set()
            for s in item.siblings:
                _validate_step_shape(s, seen_names, repo_root, cld_root, declared_so_far)
                if any(i in {sib.name for sib in item.siblings if sib is not s} for i in s.inputs):
                    raise ValueError(
                        f"step '{s.name}': parallel siblings cannot list each other in inputs"
                    )
                seen_names.add(s.name)
                group_names.add(s.name)
            declared_so_far |= group_names
        else:
            _validate_step_shape(item, seen_names, repo_root, cld_root, declared_so_far)
            seen_names.add(item.name)
            declared_so_far.add(item.name)


def _validate_step_shape(s: ChainStep, seen_names, repo_root, cld_root, declared_so_far):
    if not _NAME_RE.match(s.name):
        raise ValueError(f"step name '{s.name}' must match {_NAME_RE.pattern}")
    if s.name in seen_names:
        raise ValueError(f"duplicate step name '{s.name}'")
    try:
        persona_resolve(s.persona, repo_root, cld_root)
    except FileNotFoundError as e:
        raise ValueError(f"step '{s.name}': {e}") from e
    for ref in s.inputs:
        if ref not in declared_so_far:
            raise ValueError(
                f"step '{s.name}' inputs references unknown or future step '{ref}'"
            )


EMBED_THRESHOLD_BYTES = 10_000  # > this → stage as mounted file
CHAIN_OUTPUT_DIR = "chain-outputs"  # relative to repo root (NOT under .cld/, which is gitignored)


def chain_output_path(chain_name: str, step: ChainStep) -> str:
    """Relative path the step's agent will write to."""
    filename = step.output or f"{step.name}.md"
    return f"{CHAIN_OUTPUT_DIR}/{chain_name}/{filename}"


def compose_task(
    *,
    chain: Chain,
    step: ChainStep,
    initial_task: str | None,
    prior_outputs: list[tuple[str, str]],
    repo_root: Path,
    cld_root: Path,
) -> Path:
    """Compose the task file for this step.

    Returns the path to the staged task file under <repo_root>/.cld/.
    The agent should be launched with this as task_file=.
    """
    sections = []

    if step.prompt:
        sections.append(step.prompt.strip())

    if initial_task is not None:
        sections.append("# Initial Task\n\n" + initial_task.strip())

    if prior_outputs:
        header = "# Previous Step Output" if len(prior_outputs) == 1 \
                 else "# Previous Step Outputs (parallel)"
        parts = [header]
        for src, body in prior_outputs:
            if len(prior_outputs) > 1:
                parts.append(f"\n### {src}\n")
            parts.append(body.strip() or "_(no text output)_")
        sections.append("\n".join(parts))

    footer_tpl = (cld_root / "cld/prompts/chain-step-footer.md").read_text()
    output_path = chain_output_path(chain.name, step)
    footer = Template(footer_tpl).safe_substitute(OUTPUT_PATH=output_path)
    sections.append(footer)

    full = "\n\n".join(sections) + "\n"
    staged = cld_tmpdir(repo_root) / f"chain-{chain.name}-{step.name}.md"
    staged.write_text(full)
    return staged


def chain_branch(chain: Chain) -> str:
    return f"chain_{chain.name}"


def step_session(chain: Chain, step: ChainStep, group_idx: int | None = None) -> str:
    """Session/branch name for an individual step's agent."""
    prefix = chain_branch(chain)
    if group_idx is not None:
        return f"{prefix}_{group_idx}_{step.name}"
    return f"{prefix}_{step.name}"


def initialise_chain_branch(chain: Chain, vcs, revision: str) -> str:
    """Create the persistent chain branch at *revision* and return its name."""
    branch = chain_branch(chain)
    start = vcs.resolve_revision(revision)
    vcs.create_branch(branch, start)
    return branch


def advance_chain_branch(
    chain: Chain,
    vcs,
    successful_session: str,
    transient_sessions: list[str],
) -> None:
    """Advance the chain branch to a step's tip, then delete the transients.

    successful_session: the session whose tip becomes the new chain head
      (for parallel groups, this is the first sibling).
    transient_sessions: all per-step session branches to clean up afterwards
      (includes successful_session itself).
    """
    branch = chain_branch(chain)
    vcs.set_branch(branch, successful_session)
    for s in transient_sessions:
        try:
            vcs.delete_branch(s)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


@dataclass(frozen=True)
class StepResult:
    step_name: str
    session: str
    status: str            # "success", "failed", "timeout", "no_changes", "unknown"
    output_text: str       # raw contents of declared output file (may be "")
    failure_md: str        # contents of AGENT-FAILURE.md if present, else ""
    summary: dict
    cost_usd: float        # from result.json, 0.0 if not present
    duration_seconds: float


def execute_step(
    *,
    cfg: Config,
    vcs,
    chain: Chain,
    step: ChainStep,
    session_name: str,
    initial_task: str | None,
    prior_outputs: list[tuple[str, str]],
    repo_root: Path,
    cld_root: Path,
) -> StepResult:
    """Launch one agent for one step, wait for completion, return its result."""
    persona_path = persona_resolve(step.persona, repo_root, cld_root)
    persona_path = _stage_persona_without_frontmatter(persona_path, chain, step, repo_root)
    task_file = compose_task(
        chain=chain, step=step,
        initial_task=initial_task,
        prior_outputs=prior_outputs,
        repo_root=repo_root, cld_root=cld_root,
    )
    _dbg(cfg, f"composed task file: {task_file}")
    if cfg.debug:
        _dbg(cfg, task_file.read_text()[:500])
    model = step.model or chain.defaults.model or cfg.chain_default_model or ""
    revision = chain_branch(chain)
    start = time.monotonic()
    launch_agent(
        cfg,
        task_file=task_file,
        model=model,
        revision=revision,
        session_name=session_name,
        system_prompt_file=persona_path,
        quiet=True,
    )
    summary = wait_for_agent(session_name, vcs, cfg)
    output_path = chain_output_path(chain.name, step)
    output_text = vcs.file_show(session_name, output_path) or ""
    failure_md = vcs.file_show(
        session_name, f"agent-output-{session_name}/AGENT-FAILURE.md"
    ) or ""
    cost = read_agent_cost(session_name, vcs) or 0.0
    return StepResult(
        step_name=step.name,
        session=session_name,
        status=summary.get("status", "unknown"),
        output_text=output_text,
        failure_md=failure_md,
        summary=summary,
        cost_usd=cost,
        duration_seconds=time.monotonic() - start,
    )


@dataclass(frozen=True)
class ChainResult:
    chain_name: str
    chain_branch: str
    steps: tuple[StepResult, ...]
    success: bool
    total_cost_usd: float
    total_duration_seconds: float
    failure_reason: str    # "" when success


def run_chain(
    cfg: Config,
    chain_file: Path,
    *,
    initial_task: str | None = None,
    inline_prompt: str | None = None,
    revision: str = "",
    name_suffix: str = "",
) -> ChainResult:
    repo_root = find_repo_root()
    cld_root = Path(__file__).resolve().parent.parent
    vcs = get_backend()

    chain = load_chain(chain_file)
    validate_chain(chain, repo_root, cld_root)
    _dbg(cfg, f"run_chain: name={chain.name} file={chain_file} steps={len(chain.steps)}")

    default_rev = "@" if vcs.name == "jj" else "HEAD"
    start = revision or default_rev
    initialise_chain_branch(chain, vcs, start)

    initial_text = _merge_initial(initial_task, inline_prompt)

    results: list[StepResult] = []
    total_start = time.monotonic()
    failure_reason = ""

    try:
        for i, item in enumerate(chain.steps):
            if isinstance(item, ParallelGroup):
                _dbg(cfg, f"parallel group ({len(item.siblings)} siblings) launching")
                prior_outputs = _gather_prior_outputs(chain, item, results, i)
                step_initial = initial_text or None
                group_results = _run_parallel(
                    cfg=cfg, vcs=vcs, chain=chain, group=item, group_idx=i,
                    prior_outputs=prior_outputs,
                    initial_task=step_initial,
                    repo_root=repo_root, cld_root=cld_root,
                )
                results.extend(group_results)
                failures = [r for r in group_results if r.status not in ("success", "no_changes", "unknown")]
                if failures:
                    failure_reason = (
                        f"parallel group {i} had {len(failures)} failed siblings: "
                        + ", ".join(r.step_name for r in failures)
                    )
                    break
                first_success = next(
                    (r for r in group_results if r.status in ("success", "no_changes", "unknown")),
                    group_results[0],
                )
                advance_chain_branch(
                    chain, vcs,
                    successful_session=first_success.session,
                    transient_sessions=[r.session for r in group_results],
                )
                continue
            step = item
            session = step_session(chain, step)
            model_eff = step.model or chain.defaults.model or cfg.chain_default_model or ""
            _dbg(
                cfg,
                f"step {i+1}/{len(chain.steps)} '{step.name}' launching"
                f" (persona={step.persona}, model={model_eff},"
                f" revision={chain_branch(chain)}, session={session})",
            )
            prior_outputs = _gather_prior_outputs(chain, step, results, i)
            step_initial = initial_text or None

            result = execute_step(
                cfg=cfg, vcs=vcs, chain=chain, step=step,
                session_name=session,
                initial_task=step_initial,
                prior_outputs=prior_outputs,
                repo_root=repo_root,
                cld_root=cld_root,
            )
            results.append(result)
            _dbg(
                cfg,
                f"step '{step.name}' completed status={result.status}"
                f" cost=${result.cost_usd:.4f} duration={result.duration_seconds:.1f}s"
                f" output_bytes={len(result.output_text)}",
            )

            _TERMINAL_FAILURES = {"failed", "commit_failed", "timeout"}
            if result.status in _TERMINAL_FAILURES:
                first_line = (result.failure_md.splitlines() or [""])[0][:120]
                failure_reason = f"step '{step.name}' {result.status}"
                if first_line:
                    failure_reason += f": {first_line}"
                _dbg(cfg, f"step '{step.name}' FAILED ({result.status})")
                break
            if result.status not in {"success", "no_changes", "unknown"}:
                _dbg(cfg, f"step '{step.name}' unrecognised status={result.status!r}, treating as success")

            advance_chain_branch(
                chain, vcs, successful_session=session,
                transient_sessions=[session],
            )
            _dbg(cfg, f"chain branch advanced to {session}")
    except KeyboardInterrupt:
        failure_reason = "interrupted"

    total_cost = sum(r.cost_usd for r in results)
    total_dur = time.monotonic() - total_start
    _OK_STATUSES = {"success", "no_changes", "unknown"}
    success = not failure_reason and all(r.status in _OK_STATUSES for r in results)
    return ChainResult(
        chain_name=chain.name,
        chain_branch=chain_branch(chain),
        steps=tuple(results),
        success=success,
        total_cost_usd=total_cost,
        total_duration_seconds=total_dur,
        failure_reason=failure_reason,
    )


def print_chain_report(result: ChainResult, vcs) -> None:
    print()
    print("=" * 60)
    header = "Chain completed" if result.success else "Chain stopped"
    print(f"{header}: {result.chain_name} "
          f"({len(result.steps)} step{'s' if len(result.steps) != 1 else ''})")
    if result.failure_reason:
        print(f"Reason: {result.failure_reason}")
    print("=" * 60)
    print()

    name_w = max((len(s.step_name) for s in result.steps), default=8)
    sess_w = max((len(s.session) for s in result.steps), default=12)
    for s in result.steps:
        glyph = "✓" if s.status == "success" else "✗"
        print(
            f"  {glyph} {s.step_name:<{name_w}}  "
            f"{s.session:<{sess_w}}  "
            f"{format_duration(s.duration_seconds):>7}  "
            f"${s.cost_usd:>6.4f}  "
            f"status={s.status}"
            f"  output_bytes={len(s.output_text)}"
        )
        if s.failure_md:
            for line in s.failure_md.strip().splitlines()[:5]:
                print(f"      | {line}")
            if len(s.failure_md.splitlines()) > 5:
                print("      | ... (see branch for full failure)")

    print()
    print(f"  Branch:   {result.chain_branch}")
    print(f"  Total:    {format_duration(result.total_duration_seconds)}, "
          f"${result.total_cost_usd:.4f}")
    if vcs.name == "jj":
        print(f"  Inspect:  jj log -r '{result.chain_branch}'")
        print(f"  Diff:     jj diff -r {result.chain_branch}")
        print(f"  Merge:    jj squash --from {result.chain_branch}")
    else:
        print(f"  Inspect:  git log {result.chain_branch}")
        print(f"  Diff:     git diff {result.chain_branch}")
        print(f"  Merge:    git merge {result.chain_branch}")
    print()


def _merge_initial(task: str | None, prompt: str | None) -> str:
    if task and prompt:
        return f"{task}\n\n## Additional Instructions\n\n{prompt}\n"
    return task or prompt or ""


def _run_parallel(
    *,
    cfg: Config,
    vcs,
    chain: Chain,
    group: ParallelGroup,
    group_idx: int,
    prior_outputs: list[tuple[str, str]],
    initial_task: str | None,
    repo_root: Path,
    cld_root: Path,
) -> list[StepResult]:
    """Launch all siblings (serialized launch), wait concurrently, return results."""
    sessions: list[tuple[ChainStep, str, Path]] = []
    for sibling in group.siblings:
        session = step_session(chain, sibling, group_idx=group_idx)
        persona_path = persona_resolve(sibling.persona, repo_root, cld_root)
        persona_path = _stage_persona_without_frontmatter(persona_path, chain, sibling, repo_root)
        task_file = compose_task(
            chain=chain, step=sibling,
            initial_task=initial_task,
            prior_outputs=prior_outputs,
            repo_root=repo_root, cld_root=cld_root,
        )
        model = sibling.model or chain.defaults.model or cfg.chain_default_model or ""
        _dbg(cfg, f"parallel sibling '{sibling.name}' launching session={session}")
        launch_agent(
            cfg,
            task_file=task_file,
            model=model,
            revision=chain_branch(chain),
            session_name=session,
            system_prompt_file=persona_path,
            quiet=True,
        )
        sessions.append((sibling, session, task_file))

    results: list[StepResult] = []
    for sibling, session, _task in sessions:
        start = time.monotonic()
        summary = wait_for_agent(session, vcs, cfg)
        output_path = chain_output_path(chain.name, sibling)
        output_text = vcs.file_show(session, output_path) or ""
        failure_md = vcs.file_show(
            session, f"agent-output-{session}/AGENT-FAILURE.md"
        ) or ""
        cost = read_agent_cost(session, vcs) or 0.0
        results.append(StepResult(
            step_name=sibling.name,
            session=session,
            status=summary.get("status", "unknown"),
            output_text=output_text,
            summary=summary,
            cost_usd=cost,
            duration_seconds=time.monotonic() - start,
            failure_md=failure_md,
        ))
    return results


def _gather_prior_outputs(
    chain: Chain,
    step: ChainStep | ParallelGroup,
    results: list[StepResult],
    current_idx: int,
) -> list[tuple[str, str]]:
    """Resolve prior outputs for *step* (or the current *group*).

    - If step.inputs is set, use those step names (must exist in `results`).
    - Else, use the immediately previous chain entry. If it was a parallel
      group (T-017), concatenate all its siblings' outputs.
    - ParallelGroup has no inputs field; falls through to the default path.
    """
    inputs = getattr(step, "inputs", ())
    if inputs:
        return [
            (r.step_name, r.output_text)
            for r in results if r.step_name in inputs
        ]
    if current_idx == 0:
        return []
    prev_item = chain.steps[current_idx - 1]
    if isinstance(prev_item, ParallelGroup):
        sib_names = {s.name for s in prev_item.siblings}
        return [(r.step_name, r.output_text)
                for r in results if r.step_name in sib_names]
    # Previous was a single step.
    return [(results[-1].step_name, results[-1].output_text)]
