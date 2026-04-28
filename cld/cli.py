"""CLI entry point for cld."""

import functools
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import typer

from cld.agent import launch_agent, launch_review, AGENT_IMAGE
from cld.docker import (
    build_container_args,
    build_session_name,
    cld_tmpdir,
    ensure_image,
    find_repo_root,
    load_dotenv,
    log_error,
    log_info,
    log_warn,
    mount_home_path,
    require_docker,
    to_host_path,
    BASE_IMAGE,
    CONTAINER_HOME,
    DEVCONTAINER_IMAGE,
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


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", callback=_version_callback, is_eager=True, help="Show version and exit"),
):
    pass


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
    launch_agent(
        task_file=task_path,
        inline_prompt=prompt or None,
        name=name,
        model=model,
        revision=revision,
    )


# Host paths to mount read-only (config files), devcontainer only.
_DIRECT_RO = [".gitconfig", ".bashrc"]

# Nvim host dirs mounted RO under /tmp/nvim-host/<sub>; devcontainer entrypoint
# copies them into $HOME so changes don't persist back to the host.
_NVIM_HOST_MOUNTS = {
    ".config/nvim": "config",
    ".local/share/nvim": "share",
    ".local/state/nvim": "state",
    ".cache/nvim": "cache",
}


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

    cld_root = Path(__file__).resolve().parent.parent
    ensure_image(
        DEVCONTAINER_IMAGE,
        cld_root / "imgs/claude-devcontainer/Dockerfile.claude-devcontainer",
        cld_root,
        parent_image=(
            BASE_IMAGE,
            cld_root / "imgs/claude-base/Dockerfile.claude-base",
            cld_root,
        ),
    )
    load_dotenv()

    repo_root = find_repo_root()
    session = build_session_name("cld", name)

    args = build_container_args(repo_root, session, interactive=True)
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

    for rel_path, sub in _NVIM_HOST_MOUNTS.items():
        local_path = Path.home() / rel_path
        if local_path.is_dir():
            host_path = to_host_path(str(local_path.resolve()))
            args += ["-v", f"{host_path}:/tmp/nvim-host/{sub}:ro"]
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
@_handle_errors
def review(
    feature_branch: str = typer.Argument(help="Feature branch to review"),
    trunk_branch: Optional[str] = typer.Argument(default=None, help="Trunk branch to diff against (auto-detected if omitted)"),
    name: str = typer.Option("", "-n", "--name", help="Session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Claude model"),
):
    """Launch a code review agent."""
    if trunk_branch is None:
        from cld.vcs import get_backend
        branches = get_backend().list_branches()
        branch_names = {
            line.strip().lstrip("* ").split(":")[0].split()[0]
            for line in branches.splitlines()
            if line.strip()
        }
        for candidate in ("main", "master", "trunk"):
            if candidate in branch_names:
                trunk_branch = candidate
                break
        if trunk_branch is None:
            raise RuntimeError("Could not auto-detect trunk branch; none of main/master/trunk found. Pass it explicitly.")
    launch_review(feature_branch, trunk_branch, name=name, model=model)


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

    if prompt:
        repo_root = find_repo_root()
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", prefix="loop-task-", delete=False, dir=cld_tmpdir(repo_root),
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
@_handle_errors
def build(no_cache: bool = typer.Option(False, "--no-cache", help="Force rebuild without cache")):
    """Build base, devcontainer, and agent images (base first)."""
    require_docker()
    cld_root = Path(__file__).resolve().parent.parent
    ensure_image(BASE_IMAGE, cld_root / "imgs/claude-base/Dockerfile.claude-base", cld_root, force=no_cache)
    ensure_image(DEVCONTAINER_IMAGE, cld_root / "imgs/claude-devcontainer/Dockerfile.claude-devcontainer", cld_root, force=no_cache)
    ensure_image(AGENT_IMAGE, cld_root / "imgs/claude-agent/Dockerfile.claude-agent", cld_root / "imgs/claude-agent", force=no_cache)


if __name__ == "__main__":
    app()
