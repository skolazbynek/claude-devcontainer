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
    find_jj_root,
    load_dotenv,
    log_error,
    log_info,
    require_docker,
    _to_host_path,
    WORKSPACE_BASE,
)

AGENT_IMAGE = "claude-agent:latest"


def _build_task_file(
    task_file: Path | None, inline_prompt: str | None, tmpdir: Path | None = None,
) -> Path:
    """Build a task file from file, inline prompt, or both. Returns path to temp file.

    tmpdir: directory for temp files -- must be host-translatable for bind mounts.
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
) -> dict:
    """Launch an autonomous Claude agent in a Docker container.

    Returns dict with container_id, session_name, jj_root.
    """
    require_docker()
    load_dotenv()
    jj_root = find_jj_root()

    cld_root = Path(__file__).resolve().parent.parent
    ensure_image(
        AGENT_IMAGE,
        cld_root / "imgs/claude-agent/Dockerfile.claude-agent",
        cld_root / "imgs/claude-agent",
    )

    session = session_name or build_session_name("agent", name)
    resolved_task = _build_task_file(task_file, inline_prompt, tmpdir=jj_root)
    host_task = _to_host_path(str(resolved_task))

    args = ["--name", session]
    args += build_container_args(jj_root, session)
    args += [
        "-e", "INSTRUCTION_FILE=/config/task.md",
        "-v", f"{host_task}:/config/task.md:ro",
    ]
    if model:
        args += ["-e", f"AGENT_MODEL={model}"]
    if revision:
        args += ["-e", f"AGENT_REVISION={revision}"]

    log_info("Starting agent in background...")
    log_info(f"Task: {resolved_task}")
    log_info(f"Repository: {jj_root}")
    print()

    container_id = subprocess.run(
        ["docker", "run", "--detach"] + args + [AGENT_IMAGE],
        capture_output=True, text=True,
    )

    if container_id.returncode != 0:
        log_error(f"Failed to start container: {container_id.stderr.strip()}")
        sys.exit(1)

    cid = container_id.stdout.strip()
    print(f"Container ID: {cid}")
    print()
    print("========================================")
    print("Agent started successfully")
    print("========================================")
    print()
    print(f"Check if running:\n  docker ps --filter id={cid}")
    print(f"\nFollow progress (logs):\n  tail -f {jj_root}/agent-output-{session}/agent.log")
    print(f"\nWait for completion:\n  docker wait {cid}")
    print(f"\nAfter completion, view results:\n  jj log -r {session}\n  jj diff -r {session}")
    print(f"  cat {jj_root}/agent-output-{session}/summary.json")
    print(f"\nMerge changes:\n  jj squash --from {session}")
    print()

    return {"container_id": cid, "session_name": session, "jj_root": str(jj_root)}


def launch_review(
    feature_branch: str,
    trunk_branch: str,
    name: str = "",
    model: str = "",
) -> dict:
    """Generate a diff and launch a review agent."""
    jj_root = find_jj_root()
    cld_root = Path(__file__).resolve().parent.parent

    session = build_session_name("review", name)

    # Generate diff
    diff_file = jj_root / f"review-diff-{session}.patch"
    log_info(f"Generating diff: fork_point({feature_branch} | {trunk_branch}) -> {feature_branch}")

    result = subprocess.run(
        [
            "jj", "diff",
            "--from", f"fork_point({feature_branch} | {trunk_branch})",
            "--to", feature_branch,
            "--git",
        ],
        capture_output=True, text=True, cwd=str(jj_root),
    )
    if result.returncode != 0:
        log_error(f"Failed to generate diff: {result.stderr.strip()}")
        sys.exit(1)
    if not result.stdout.strip():
        log_error("Generated diff is empty")
        sys.exit(1)

    diff_file.write_text(result.stdout)
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
        dir=jj_root,
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
    """Run Claude in headless mode. Replaces current process."""
    os.execvp("claude", ["claude", "-p", "--permission-mode", "acceptEdits", *args])
