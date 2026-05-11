"""CLI entry point for cld."""

import functools
import os
import subprocess
from pathlib import Path
from typing import Optional

import typer

from cld.agent import launch_agent, launch_review
from cld.config import Config
from cld.docker import (
    agent_extra_paths,
    base_extra_paths,
    build_container_args,
    build_session_name,
    devcontainer_extra_paths,
    ensure_image,
    find_repo_context,
    log_error,
    log_info,
    log_warn,
    require_docker,
    stage_home_ro,
)
from cld.loop import run_loop

app = typer.Typer()


def _handle_errors(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (RuntimeError, subprocess.CalledProcessError, FileNotFoundError) as e:
            log_error(str(e))
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
        ctx.invoke(devcontainer, name="", model="", revision="", extra_args=None)


@app.command()
@_handle_errors
def agent(
    task_file: Optional[str] = typer.Argument(None, help="Path to task markdown file"),
    name: str = typer.Option("", "-n", "--name", help="Session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Claude model (e.g. opus, sonnet)"),
    revision: str = typer.Option("", "-r", "--revision", help="Revision to base workspace on (default: last committed change -- @- for jj, HEAD for git)"),
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
    name: str = typer.Option("", "-n", "--name", help="Session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Claude model (e.g. opus, sonnet)"),
    revision: str = typer.Option("", "-r", "--revision", help="Revision to base workspace on (default: last committed change -- @- for jj, HEAD for git)"),
    extra_args: Optional[list[str]] = typer.Argument(None, help="Extra args passed to container"),
):
    """Launch an interactive Claude devcontainer."""
    require_docker()
    cfg = Config.from_env()

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

    repo_root, workspace_rev = find_repo_context()
    session = build_session_name("cld", name)

    args = build_container_args(repo_root, session, cfg, interactive=True)
    if model:
        args += ["-e", f"AGENT_MODEL={model}"]
    effective_revision = revision or workspace_rev
    if effective_revision:
        args += ["-e", f"AGENT_REVISION={effective_revision}"]

    skipped = []
    for rel in cfg.home_mounts_devcontainer:
        mnt = stage_home_ro(rel, cfg)
        if mnt:
            args += mnt
        else:
            skipped.append(rel)

    if skipped:
        log_warn(f"Optional host paths not found (skipped): {', '.join(skipped)}")

    args += [cfg.devcontainer_image]
    if extra_args:
        args += extra_args

    log_info("Starting Claude Code in container...")
    print()

    os.execvp("docker", ["docker", "run"] + args)


@app.command()
@_handle_errors
def review(
    feature_branch: str = typer.Argument(help="Feature branch to review"),
    trunk_branch: Optional[str] = typer.Argument(default=None, help="Trunk branch to diff against (auto-detected if omitted)"),
    name: str = typer.Option("", "-n", "--name", help="Session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Claude model"),
):
    """Launch a code review agent."""
    cfg = Config.from_env()
    if trunk_branch is None:
        from cld.vcs import get_backend
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
    launch_review(cfg, feature_branch, trunk_branch, name=name, model=model)


@app.command()
@_handle_errors
def loop(
    task_file: Optional[str] = typer.Argument(None, help="Path to task markdown file"),
    name: str = typer.Option("", "-n", "--name", help="Loop session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Model for implementer agent"),
    review_model: str = typer.Option("", "--review-model", help="Model for reviewer agent"),
    revision: str = typer.Option("", "-r", "--revision", help="Revision to base workspace on (default: last committed change -- @- for jj, HEAD for git)"),
    max_iterations: int = typer.Option(3, "--max-iterations", help="Maximum iteration count"),
    prompt: str = typer.Option("", "-p", "--prompt", help="Inline prompt (alternative to task file)"),
    approve: bool = typer.Option(False, "--approve", help="Pause after each review for approval"),
):
    """Run an automated implement-review loop."""
    if not task_file and not prompt:
        typer.echo("Error: Provide a task file, --prompt, or both", err=True)
        raise typer.Exit(1)
    task_path = Path(task_file) if task_file else None
    if task_path and not task_path.is_file():
        typer.echo(f"Error: Task file not found: {task_file}", err=True)
        raise typer.Exit(1)

    cfg = Config.from_env()
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


if __name__ == "__main__":
    app()
