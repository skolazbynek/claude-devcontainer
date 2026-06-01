"""CLI entry point for cld."""

import functools
import os
import subprocess
from pathlib import Path
from typing import Optional

import typer

from cld.agent import launch_agent, launch_review
from cld.chain import ParallelGroup, load_chain, print_chain_report, run_chain, validate_chain
from cld.config import Config
from cld.docker import (
    agent_extra_paths,
    base_extra_paths,
    build_container_args,
    build_session_name,
    devcontainer_extra_paths,
    ensure_image,
    find_repo_context,
    find_repo_root,
    require_docker,
    stage_home_ro,
    to_host_path,
)
from cld.log import get_logger, setup_logging
from cld.loop import run_loop
from cld.vcs import get_backend

log = get_logger(__name__)

app = typer.Typer()


def _handle_errors(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (RuntimeError, ValueError, subprocess.CalledProcessError, FileNotFoundError) as e:
            log.error("Command failed: %s", e)
            log.debug("traceback:", exc_info=True)
            raise typer.Exit(1)
    return wrapper


def _version_callback(value: bool):
    if value:
        from cld import __version__
        typer.echo(f"cld {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", callback=_version_callback, is_eager=True, help="Show version and exit"),
):
    if ctx.invoked_subcommand is None:
        ctx.invoke(devcontainer, task_file=None, name="", model="", revision="", prompt="", extra_args=None)


@app.command()
@_handle_errors
def agent(
    task_file: Optional[str] = typer.Argument(None, help="Path to task markdown file"),
    name: str = typer.Option("", "-n", "--name", help="Session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Claude model (e.g. opus, sonnet)"),
    revision: str = typer.Option("", "-r", "--revision", help="Anchor revision (default: current change -- @ for jj, HEAD for git)"),
    prompt: str = typer.Option("", "-p", "--prompt", help="Inline prompt (appended to task file if both given)"),
):
    """Launch an autonomous Claude agent."""
    task_path = Path(task_file) if task_file else None
    if task_path and not task_path.is_file():
        typer.echo(f"Error: Task file not found: {task_file}", err=True)
        raise typer.Exit(1)
    if not task_path and not prompt:
        typer.echo("Error: Provide a task file, --prompt, or both", err=True)
        raise typer.Exit(1)
    cfg = Config.from_env()
    setup_logging(cfg)
    log.info(
        "agent: name=%s, model=%s, revision=%s, task_file=%s, prompt=%s",
        name or "<auto>",
        model or "<default>",
        revision or "<default>",
        str(task_path) if task_path else "<none>",
        "<provided>" if prompt else "<none>",
    )
    launch_agent(
        cfg,
        task_file=task_path,
        inline_prompt=prompt or None,
        name=name,
        model=model,
        revision=revision,
    )



@app.command()
@_handle_errors
def devcontainer(
    task_file: Optional[str] = typer.Argument(None, help="Path to task markdown file"),
    name: str = typer.Option("", "-n", "--name", help="Session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Claude model (e.g. opus, sonnet)"),
    revision: str = typer.Option("", "-r", "--revision", help="Anchor revision (default: current change -- @ for jj, HEAD for git)"),
    prompt: str = typer.Option("", "-p", "--prompt", help="Inline prompt (appended to task file if both given)"),
    extra_args: Optional[list[str]] = typer.Argument(None, help="Extra args passed to container"),
):
    """Launch an interactive Claude devcontainer."""
    require_docker()
    task_path = Path(task_file) if task_file else None
    if task_path and not task_path.is_file():
        typer.echo(f"Error: Task file not found: {task_file}", err=True)
        raise typer.Exit(1)
    cfg = Config.from_env()
    setup_logging(cfg)
    log.info(
        "devcontainer: name=%s, model=%s, revision=%s, task_file=%s, prompt=%s",
        name or "<auto>",
        model or "<default>",
        revision or "<default>",
        str(task_path) if task_path else "<none>",
        "<provided>" if prompt else "<none>",
    )

    cld_root = Path(__file__).resolve().parent.parent
    ensure_image(
        cfg.devcontainer_image,
        cld_root / "imgs/claude-devcontainer/Dockerfile.claude-devcontainer",
        cld_root,
        extra_paths=devcontainer_extra_paths(cld_root),
        parent_image=(
            cfg.base_image,
            cld_root / "imgs/claude-base/Dockerfile.claude-base",
            cld_root,
            base_extra_paths(cld_root),
        ),
    )

    repo_root, _workspace_rev = find_repo_context()
    session = build_session_name("cld", name)

    from cld.agent import agent_workspace_path
    from cld.vcs import get_backend
    from cld.vcs.anchor import create_editable_root, resolve_anchor

    vcs = get_backend()
    anchor_hash = resolve_anchor(vcs, revision)
    ws_path = agent_workspace_path(repo_root, session)
    create_editable_root(vcs, anchor_hash, ws_path, session)
    log.info(f"Anchor: {anchor_hash[:12]}")

    args = build_container_args(repo_root, session, cfg, interactive=True)
    host_ws = to_host_path(str(ws_path), cfg)
    args += ["-v", f"{host_ws}:/workspace/current"]
    args += ["-e", "WORKSPACE_PREINITIALIZED=1"]
    args += ["-e", f"AGENT_ANCHOR_HASH={anchor_hash}"]
    if task_path:
        host_task = to_host_path(str(task_path.resolve()), cfg)
        args += ["-v", f"{host_task}:/config/task.md:ro"]
    if prompt:
        args += ["-e", f"AGENT_INLINE_PROMPT={prompt}"]
    if model:
        args += ["-e", f"AGENT_MODEL={model}"]

    skipped = []
    for rel in cfg.home_mounts_devcontainer:
        mnt = stage_home_ro(rel, cfg)
        if mnt:
            args += mnt
        else:
            skipped.append(rel)

    if skipped:
        log.warning(f"Optional host paths not found (skipped): {', '.join(skipped)}")

    args += [cfg.devcontainer_image]
    if extra_args:
        args += extra_args

    log.info("Starting Claude Code in container...")
    print()

    os.execvp("docker", ["docker", "run"] + args)


@app.command()
@_handle_errors
def review(
    feature_branch: str = typer.Argument(help="Feature branch to review"),
    trunk_branch: Optional[str] = typer.Argument(default=None, help="Trunk branch to diff against (auto-detected if omitted)"),
    name: str = typer.Option("", "-n", "--name", help="Session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Claude model"),
    revision: str = typer.Option("", "-r", "--revision", help="Anchor revision (default: current change -- @ for jj, HEAD for git)"),
):
    """Launch a code review agent."""
    cfg = Config.from_env()
    setup_logging(cfg)
    if trunk_branch is None:
        branches = get_backend().list_branches()
        branch_names = {
            line.strip().lstrip("* ").split(":")[0].split()[0]
            for line in branches.splitlines()
            if line.strip()
        }
        for candidate in cfg.trunk_candidates:
            if candidate in branch_names:
                trunk_branch = candidate
                break
        if trunk_branch is None:
            raise RuntimeError(f"Could not auto-detect trunk branch; none of {list(cfg.trunk_candidates)} found. Pass it explicitly.")
    log.info(
        "review: feature=%s, trunk=%s, model=%s",
        feature_branch,
        trunk_branch or "<auto>",
        model or "<default>",
    )
    launch_review(cfg, feature_branch, trunk_branch, name=name, model=model, revision=revision)


@app.command()
@_handle_errors
def loop(
    task_file: Optional[str] = typer.Argument(None, help="Path to task markdown file"),
    name: str = typer.Option("", "-n", "--name", help="Loop session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Model for implementer agent"),
    review_model: str = typer.Option("", "--review-model", help="Model for reviewer agent"),
    revision: str = typer.Option("", "-r", "--revision", help="Revision to base workspace on (default: last committed change -- @- for jj, HEAD for git)"),
    max_iterations: int = typer.Option(3, "--max-iterations", help="Hard cap on iterations; loop also stops early on a clean review (no critical/major findings)"),
    prompt: str = typer.Option("", "-p", "--prompt", help="Inline prompt (appended to task file if both given)"),
    approve: bool = typer.Option(False, "--approve", help="Pause after each review for approval (continue/stop/view/edit findings)"),
):
    """Run an automated implement-review loop.

    Each iteration runs an implementer agent, then a reviewer agent. Review
    findings feed into the next implementer. Stops on a clean review, hitting
    --max-iterations, or an agent failure. All iterations land on a single
    'loop_<name>' branch; the final report prints inspection/merge commands.
    """
    if not task_file and not prompt:
        typer.echo("Error: Provide a task file, --prompt, or both", err=True)
        raise typer.Exit(1)
    if max_iterations < 1:
        typer.echo("Error: --max-iterations must be at least 1", err=True)
        raise typer.Exit(1)
    task_path = Path(task_file) if task_file else None
    if task_path and not task_path.is_file():
        typer.echo(f"Error: Task file not found: {task_file}", err=True)
        raise typer.Exit(1)

    cfg = Config.from_env()
    setup_logging(cfg)
    log.info(
        "loop: task_file=%s, prompt=%s, model=%s, review_model=%s, max_iterations=%d, approve=%s",
        str(task_path) if task_path else "<none>",
        "<provided>" if prompt else "<none>",
        model or "<default>",
        review_model or "<default>",
        max_iterations,
        approve,
    )
    run_loop(
        cfg,
        task_path,
        inline_prompt=prompt or None,
        name=name,
        model=model,
        review_model=review_model,
        revision=revision,
        max_iterations=max_iterations,
        approve=approve,
    )


@app.command()
@_handle_errors
def build(no_cache: bool = typer.Option(False, "--no-cache", help="Force rebuild without cache")):
    """Build base, devcontainer, and agent images (base first)."""
    require_docker()
    cfg = Config.from_env()
    setup_logging(cfg)
    log.info("build: no_cache=%s", no_cache)
    cld_root = Path(__file__).resolve().parent.parent
    ensure_image(
        cfg.base_image,
        cld_root / "imgs/claude-base/Dockerfile.claude-base",
        cld_root,
        extra_paths=base_extra_paths(cld_root),
        force=True, no_cache=no_cache,
    )
    ensure_image(
        cfg.devcontainer_image,
        cld_root / "imgs/claude-devcontainer/Dockerfile.claude-devcontainer",
        cld_root,
        extra_paths=devcontainer_extra_paths(cld_root),
        parent_image=(
            cfg.base_image,
            cld_root / "imgs/claude-base/Dockerfile.claude-base",
            cld_root,
            base_extra_paths(cld_root),
        ),
        force=True, no_cache=no_cache,
    )
    ensure_image(
        cfg.agent_image,
        cld_root / "imgs/claude-agent/Dockerfile.claude-agent",
        cld_root / "imgs/claude-agent",
        extra_paths=agent_extra_paths(cld_root),
        parent_image=(
            cfg.base_image,
            cld_root / "imgs/claude-base/Dockerfile.claude-base",
            cld_root,
            base_extra_paths(cld_root),
        ),
        force=True, no_cache=no_cache,
    )


def _parse_description(path: Path) -> str:
    lines = path.read_text().splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("description:"):
            return line[len("description:"):].strip()
    return ""


@app.command()
def prompts():
    """List available prompt templates with descriptions."""
    cfg = Config.from_env()
    setup_logging(cfg)
    log.info("prompts list")
    prompts_dir = Path(__file__).resolve().parent.parent / "prompts"
    if not prompts_dir.exists():
        typer.echo("No prompts directory found.", err=True)
        raise typer.Exit(1)

    items = []
    for path in sorted(prompts_dir.rglob("*.md")):
        rel = path.relative_to(prompts_dir).with_suffix("")
        desc = _parse_description(path)
        items.append((str(rel), desc))

    if not items:
        typer.echo("No prompts found.")
        return

    width = max(len(name) for name, _ in items)
    for name, desc in items:
        typer.echo(f"  {name:<{width}}  {desc}")


chain_app = typer.Typer(help="Multi-agent chain orchestrator.")
app.add_typer(chain_app, name="chain")


@chain_app.command("run")
@_handle_errors
def chain_run(
    chain_file: str = typer.Argument(..., help="Path to chain YAML file or @<name>"),
    task_file: Optional[str] = typer.Argument(None, help="Initial task file"),
    prompt: str = typer.Option("", "-p", "--prompt", help="Inline initial prompt"),
    name: str = typer.Option("", "-n", "--name", help="Chain session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Override default model"),
    revision: str = typer.Option("", "-r", "--revision", help="Starting revision"),
):
    """Run a multi-agent chain end to end."""
    chain_path = _resolve_chain_path(chain_file)
    if not chain_path.is_file():
        typer.echo(f"Error: chain file not found: {chain_file}", err=True)
        raise typer.Exit(1)
    task_path = Path(task_file) if task_file else None
    if task_path and not task_path.is_file():
        typer.echo(f"Error: task file not found: {task_file}", err=True)
        raise typer.Exit(1)
    if not task_path and not prompt:
        typer.echo("Error: provide a task file, --prompt, or both", err=True)
        raise typer.Exit(1)

    cfg = Config.from_env()
    setup_logging(cfg)
    log.info(
        "chain run: file=%s, name=%s, model=%s",
        chain_file,
        name or "<auto>",
        model or "<default>",
    )
    initial = task_path.read_text() if task_path else None
    result = run_chain(
        cfg, chain_path,
        initial_task=initial, inline_prompt=prompt or None,
        revision=revision, name_suffix=name,
    )
    print_chain_report(result, get_backend())
    raise typer.Exit(0 if result.success else 1)


@chain_app.command("validate")
@_handle_errors
def chain_validate(
    chain_file: str = typer.Argument(..., help="Path to chain YAML file or @<name>"),
):
    """Parse and validate a chain file. Exits 0 on success, 1 on any error."""
    chain_path = _resolve_chain_path(chain_file)
    if not chain_path.is_file():
        typer.echo(f"Error: chain file not found: {chain_file}", err=True)
        raise typer.Exit(1)
    cfg = Config.from_env()
    setup_logging(cfg)
    log.info("chain validate: %s", chain_file)
    repo_root = find_repo_root()
    cld_root = Path(__file__).resolve().parent.parent
    chain = load_chain(chain_path)
    validate_chain(chain, repo_root, cld_root)
    typer.echo(f"OK: '{chain.name}' has {len(chain.steps)} top-level step(s)")


@chain_app.command("list")
@_handle_errors
def chain_list():
    """List available chain definitions."""
    cfg = Config.from_env()
    setup_logging(cfg)
    log.info("chain list")
    repo_root = find_repo_root()
    cld_root = Path(__file__).resolve().parent.parent

    seen: set[str] = set()
    items: list[tuple[str, str, str]] = []  # (source, name, description)

    for source, base in (("workspace", repo_root), ("builtin", cld_root)):
        chains_dir = base / "chains"
        if not chains_dir.is_dir():
            continue
        for path in sorted(chains_dir.glob("*.y*ml")):
            try:
                chain = load_chain(path)
            except ValueError as e:
                items.append((source, path.name, f"[invalid: {e}]"))
                continue
            if chain.name in seen:
                continue
            seen.add(chain.name)
            items.append((source, chain.name, chain.description or ""))

    if not items:
        typer.echo("No chains found.")
        return
    name_w = max(len(it[1]) for it in items)
    src_w = max(len(it[0]) for it in items)
    for src, name, desc in items:
        typer.echo(f"  [{src:<{src_w}}]  {name:<{name_w}}  {desc}")


@chain_app.command("dry-run")
@_handle_errors
def chain_dry_run(
    chain_file: str = typer.Argument(..., help="Path to chain YAML file or @<name>"),
):
    """Print the execution plan without launching agents."""
    chain_path = _resolve_chain_path(chain_file)
    if not chain_path.is_file():
        typer.echo(f"Error: chain file not found: {chain_file}", err=True)
        raise typer.Exit(1)
    cfg = Config.from_env()
    setup_logging(cfg)
    log.info("chain dry-run: %s", chain_file)
    repo_root = find_repo_root()
    cld_root = Path(__file__).resolve().parent.parent
    chain = load_chain(chain_path)
    validate_chain(chain, repo_root, cld_root)

    typer.echo(f"Chain: {chain.name}")
    if chain.description:
        typer.echo(f"  {chain.description}")
    typer.echo(f"  Default model: {chain.defaults.model}")
    typer.echo()
    typer.echo(f"Plan ({len(chain.steps)} top-level item{'s' if len(chain.steps) != 1 else ''}):")
    for i, item in enumerate(chain.steps, start=1):
        if isinstance(item, ParallelGroup):
            typer.echo(f"  {i}. PARALLEL ({len(item.siblings)} sibling(s)):")
            for s in item.siblings:
                model = s.model or chain.defaults.model
                typer.echo(f"       - {s.name} (persona={s.persona}, model={model})")
        else:
            model = item.model or chain.defaults.model
            typer.echo(f"  {i}. {item.name} (persona={item.persona}, model={model})")


def _resolve_chain_path(arg: str) -> Path:
    """Resolve `@name` shortcut or treat as relative/absolute path."""
    if arg.startswith("@"):
        short = arg[1:]
        if not short.endswith((".yaml", ".yml")):
            short = f"{short}.yaml"
        repo_root = find_repo_root()
        cld_root = Path(__file__).resolve().parent.parent
        for base in (repo_root, cld_root):
            cand = base / "chains" / short
            if cand.is_file():
                return cand
        return repo_root / "chains" / short
    return Path(arg)


if __name__ == "__main__":
    app()
