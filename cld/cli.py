"""CLI entry point for cld."""

import os
import tempfile
from pathlib import Path
from typing import Optional

import typer

from cld.agent import launch_agent, launch_review, run_headless, AGENT_IMAGE
from cld.docker import (
    build_container_args,
    build_session_name,
    ensure_image,
    find_jj_root,
    load_dotenv,
    log_info,
    log_warn,
    mount_home_path,
    require_docker,
    CONTAINER_HOME,
)
from cld.loop import run_loop

app = typer.Typer(add_completion=False)


@app.command()
def agent(
    task_file: Optional[str] = typer.Argument(None, help="Path to task markdown file"),
    name: str = typer.Option("", "-n", "--name", help="Session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Claude model (e.g. opus, sonnet)"),
    revision: str = typer.Option("", "-r", "--revision", help="jj revset to base workspace on"),
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
    launch_agent(
        task_file=task_path,
        inline_prompt=prompt or None,
        name=name,
        model=model,
        revision=revision,
    )


DEVCONTAINER_IMAGE = "claude-devcontainer:latest"

# Host paths to mount read-only (config files)
_DIRECT_RO = [".gitconfig", ".bashrc"]
_DIRECT_RW = [".config/nvim", ".cache/nvim", ".local/share/nvim", ".local/state/nvim"]


@app.command()
def devcontainer(
    name: str = typer.Option("", "-n", "--name", help="Session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Claude model (e.g. opus, sonnet)"),
    revision: str = typer.Option("", "-r", "--revision", help="jj revset to base workspace on"),
    extra_args: Optional[list[str]] = typer.Argument(None, help="Extra args passed to container"),
):
    """Launch an interactive Claude devcontainer."""
    require_docker()

    cld_root = Path(__file__).resolve().parent.parent
    ensure_image(
        DEVCONTAINER_IMAGE,
        cld_root / "imgs/claude-devcontainer/Dockerfile.claude-devcontainer",
        cld_root,
    )
    load_dotenv()

    jj_root = find_jj_root()
    session = build_session_name("cld", name)

    args = build_container_args(jj_root, session, interactive=True)
    if model:
        args += ["-e", f"AGENT_MODEL={model}"]
    if revision:
        args += ["-e", f"AGENT_REVISION={revision}"]

    skipped = []
    for rel_path in _DIRECT_RO:
        mnt = mount_home_path(rel_path, f"{CONTAINER_HOME}/{rel_path}:ro")
        if mnt:
            args += mnt
        else:
            skipped.append(rel_path)

    for rel_path in _DIRECT_RW:
        mnt = mount_home_path(rel_path, f"{CONTAINER_HOME}/{rel_path}:rw")
        if mnt:
            args += mnt
        else:
            skipped.append(rel_path)

    if skipped:
        log_warn(f"Optional host paths not found (skipped): {', '.join(skipped)}")

    args += [DEVCONTAINER_IMAGE]
    if extra_args:
        args += extra_args

    log_info("Starting Claude Code in container...")
    print()

    os.execvp("docker", ["docker", "run"] + args)


@app.command()
def review(
    feature_branch: str = typer.Argument(help="Feature branch to review"),
    trunk_branch: str = typer.Argument(help="Trunk branch to diff against"),
    name: str = typer.Option("", "-n", "--name", help="Session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Claude model"),
):
    """Launch a code review agent."""
    launch_review(feature_branch, trunk_branch, name=name, model=model)


@app.command()
def loop(
    task_file: Optional[str] = typer.Argument(None, help="Path to task markdown file"),
    name: str = typer.Option("", "-n", "--name", help="Loop session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Model for implementer agent"),
    review_model: str = typer.Option("", "--review-model", help="Model for reviewer agent"),
    revision: str = typer.Option("", "-r", "--revision", help="Starting jj revision"),
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

    if prompt:
        jj_root = find_jj_root()
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", prefix=".cld-loop-task-", delete=False, dir=jj_root,
        )
        if task_path:
            tmp.write(task_path.read_text())
            tmp.write(f"\n\n## Additional Instructions\n\n{prompt}\n")
        else:
            tmp.write(prompt)
        tmp.close()
        task_path = Path(tmp.name)

    run_loop(
        task_path,
        name=name,
        model=model,
        review_model=review_model,
        revision=revision,
        max_iterations=max_iterations,
        approve=approve,
    )


@app.command()
def headless(ctx: typer.Context):
    """Run Claude in headless mode (passthrough to claude -p)."""
    run_headless(ctx.args)


if __name__ == "__main__":
    app()
