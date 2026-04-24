"""Agent, review, and headless launch logic."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from string import Template

from cld.docker import (
    build_container_args,
    build_session_name,
    ensure_image,
    find_repo_root,
    load_dotenv,
    log_error,
    log_info,
    require_docker,
    _to_host_path,
    WORKSPACE_BASE,
)
from cld.vcs import get_backend

AGENT_IMAGE = "claude-agent:latest"


def _build_task_file(
    task_file: Path | None, inline_prompt: str | None, tmpdir: Path | None = None,
) -> Path:
    """Combine a task file and/or inline prompt into a single markdown file.

    If both are given, the inline prompt is appended. Returns the resolved path
    to the final task file. *tmpdir* controls where temp files are created
    (must be host-translatable for bind mounts).
    """
    if task_file and inline_prompt:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", prefix=".cld-task-", delete=False, dir=tmpdir,
        )
        tmp.write(task_file.read_text())
        tmp.write(f"\n\n## Additional Instructions\n\n{inline_prompt}\n")
        tmp.close()
        return Path(tmp.name)
    if inline_prompt:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", prefix=".cld-task-", delete=False, dir=tmpdir,
        )
        tmp.write(inline_prompt)
        tmp.close()
        return Path(tmp.name)
    if task_file:
        return task_file.resolve()
    log_error("No task file or prompt provided")
    sys.exit(1)


def launch_agent(
    task_file: Path | None = None,
    inline_prompt: str | None = None,
    name: str = "",
    model: str = "",
    revision: str = "",
    session_name: str | None = None,
    quiet: bool = False,
) -> dict:
    """Launch an autonomous Claude agent in a Docker container.

    Validates the environment, builds container arguments, mounts the task file,
    and starts a detached container. Returns a dict with container_id,
    session_name, and repo_root.
    """
    require_docker()
    load_dotenv()
    repo_root = find_repo_root()
    vcs = get_backend()

    cld_root = Path(__file__).resolve().parent.parent
    ensure_image(
        AGENT_IMAGE,
        cld_root / "imgs/claude-agent/Dockerfile.claude-agent",
        cld_root / "imgs/claude-agent",
    )

    session = session_name or build_session_name("agent", name)
    resolved_task = _build_task_file(task_file, inline_prompt, tmpdir=repo_root)
    host_task = _to_host_path(str(resolved_task))

    args = ["--name", session]
    args += build_container_args(repo_root, session)
    args += [
        "-e", "INSTRUCTION_FILE=/config/task.md",
        "-v", f"{host_task}:/config/task.md:ro",
    ]
    if model:
        args += ["-e", f"AGENT_MODEL={model}"]
    if revision:
        args += ["-e", f"AGENT_REVISION={revision}"]

    if not quiet:
        log_info("Starting agent in background...")
        log_info(f"Task: {resolved_task}")
        log_info(f"Repository: {repo_root}")
        print()

    container_id = subprocess.run(
        ["docker", "run", "--detach"] + args + [AGENT_IMAGE],
        capture_output=True, text=True,
    )

    if container_id.returncode != 0:
        log_error(f"Failed to start container: {container_id.stderr.strip()}")
        sys.exit(1)

    cid = container_id.stdout.strip()

    if not quiet:
        vcs_name = vcs.name
        print(f"Container ID: {cid}")
        print()
        print("========================================")
        print("Agent started successfully")
        print("========================================")
        print()
        print(f"Check if running:\n  docker ps --filter id={cid}")
        print(f"\nFollow progress (logs):\n  tail -f {repo_root}/agent-output-{session}/agent.log")
        print(f"\nWait for completion:\n  docker wait {cid}")
        if vcs_name == "jj":
            print(f"\nAfter completion, view results:\n  jj log -r {session}\n  jj diff -r {session}")
            print(f"  cat {repo_root}/agent-output-{session}/summary.json")
            print(f"\nMerge changes:\n  jj squash --from {session}")
        else:
            print(f"\nAfter completion, view results:\n  git log {session}\n  git diff {session}~1..{session}")
            print(f"  cat {repo_root}/agent-output-{session}/summary.json")
            print(f"\nMerge changes:\n  git merge {session}")
        print()

    return {"container_id": cid, "session_name": session, "repo_root": str(repo_root)}


def launch_review(
    feature_branch: str,
    trunk_branch: str,
    name: str = "",
    model: str = "",
) -> dict:
    """Generate a diff between two branches and launch a code review agent.

    Uses the VCS backend to compute the fork point and produce a unified diff,
    then fills in a review template and delegates to ``launch_agent``.
    """
    vcs = get_backend()
    repo_root = vcs.repo_root
    cld_root = Path(__file__).resolve().parent.parent

    session = build_session_name("review", name)

    # Generate diff from fork point to feature branch
    diff_file = repo_root / f"review-diff-{session}.patch"
    log_info(f"Generating diff: fork_point({feature_branch}, {trunk_branch}) -> {feature_branch}")

    fork = vcs.fork_point(feature_branch, trunk_branch)
    diff_content = vcs.diff_between(fork, feature_branch)

    if diff_content.startswith("Error:"):
        log_error(f"Failed to generate diff: {diff_content}")
        sys.exit(1)
    if not diff_content.strip():
        log_error("Generated diff is empty")
        sys.exit(1)

    diff_file.write_text(diff_content)
    log_info(f"Diff saved to: {diff_file}")

    # Create task from template
    template_path = cld_root / "imgs/claude-agent-review/review-template.md"
    if not template_path.is_file():
        log_error(f"Template not found: {template_path}")
        sys.exit(1)

    task_content = Template(template_path.read_text()).safe_substitute(
        TRUNK_BRANCH=trunk_branch,
        FEATURE_BRANCH=feature_branch,
        DIFF_FILE_PATH=f"{WORKSPACE_BASE}/origin/review-diff-{session}.patch",
    )

    task_file = Path(tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix=f"review-task-{session}-", delete=False,
        dir=repo_root,
    ).name)
    task_file.write_text(task_content)
    log_info(f"Task file created: {task_file}")
    print()

    return launch_agent(
        task_file=task_file,
        model=model,
        session_name=session,
    )


def run_headless(args: list[str]) -> None:
    """Run Claude in headless mode with edit permissions. Replaces current process."""
    os.execvp("claude", ["claude", "-p", "--permission-mode", "acceptEdits", *args])
