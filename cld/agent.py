"""Agent and review launch logic."""

import subprocess
import sys
import tempfile
from pathlib import Path
from string import Template

from cld.config import Config
from cld.docker import (
    agent_extra_paths,
    base_extra_paths,
    build_container_args,
    build_session_name,
    cld_tmpdir,
    ensure_image,
    find_repo_context,
    require_docker,
    to_host_path,
    WORKSPACE_BASE,
)
from cld.log import get_logger
from cld.vcs import get_backend
from cld.vcs.jj import JjBackend

log = get_logger(__name__)


def _create_agent_workspace(vcs, session: str, revision: str, repo_root: Path) -> Path:
    """Create the agent's workspace/worktree on the host before container start.

    Returns the host path to the workspace working directory.
    """
    workspace_host_path = repo_root / ".cld" / "workspaces" / session
    workspace_host_path.parent.mkdir(parents=True, exist_ok=True)
    if workspace_host_path.exists():
        log.error(f"Workspace path already exists: {workspace_host_path}")
        sys.exit(1)

    log.debug("Creating %s workspace %s at %s (rev=%s)",
              vcs.name, session, workspace_host_path, revision or "<default>")
    out = vcs.create_workspace(session, str(workspace_host_path), revision)
    log.debug("create_workspace output: %s", out.strip() if out else "")

    if vcs.name == "jj":
        # `jj workspace add` does not create a bookmark; place one at the new
        # workspace's @ by running the command from inside that workspace.
        ws_vcs = JjBackend(repo_root=vcs.repo_root, workspace_path=workspace_host_path)
        ws_vcs.create_branch(session)

        # jj stores an absolute host-side path in <ws>/.jj/repo pointing back
        # to the main repo's .jj/. The agent container bind-mounts the main
        # repo at /workspace/origin, so we rewrite the pointer to the in-
        # container path. (Run host-side jj operations on the workspace before
        # this, since the pointer becomes invalid on the host afterward.)
        repo_pointer = workspace_host_path / ".jj" / "repo"
        repo_pointer.write_text(f"{WORKSPACE_BASE}/origin/.jj/repo")
    else:
        # git: `git worktree add -b` already created the branch. The worktree's
        # .git file contains `gitdir: <abs-host-path>` to the main repo's
        # .git/worktrees/<name> dir; rewrite to the in-container path.
        dotgit = workspace_host_path / ".git"
        if dotgit.is_file():
            content = dotgit.read_text().strip()
            if content.startswith("gitdir:"):
                abs_target = Path(content.split(":", 1)[1].strip())
                try:
                    rel_to_repo = abs_target.relative_to(vcs.repo_root)
                except ValueError:
                    rel_to_repo = None
                if rel_to_repo is not None:
                    dotgit.write_text(f"gitdir: {WORKSPACE_BASE}/origin/{rel_to_repo}\n")

    return workspace_host_path


def launch_agent(
    cfg: Config,
    task_file: Path | None = None,
    inline_prompt: str | None = None,
    name: str = "",
    model: str = "",
    revision: str = "",
    session_name: str | None = None,
    quiet: bool = False,
    *,
    system_prompt_file: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict:
    """Launch an autonomous Claude agent in a Docker container.

    Validates the environment, builds container arguments, mounts the task file,
    and starts a detached container. Returns a dict with container_id,
    session_name, and repo_root.
    """
    require_docker()
    if not task_file and not inline_prompt:
        log.error("No task file or prompt provided")
        sys.exit(1)

    repo_root, workspace_rev = find_repo_context()
    vcs = get_backend()

    cld_root = Path(__file__).resolve().parent.parent
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
        quiet=quiet,
    )

    session = session_name or build_session_name("agent", name)
    effective_revision = revision or workspace_rev

    workspace_host_path = _create_agent_workspace(vcs, session, effective_revision, repo_root)

    args = ["--name", session]
    args += build_container_args(repo_root, session, cfg)
    host_ws = to_host_path(str(workspace_host_path), cfg)
    args += ["-v", f"{host_ws}:{WORKSPACE_BASE}/current"]
    args += ["-e", "WORKSPACE_PREINITIALIZED=1"]
    if task_file:
        host_task = to_host_path(str(task_file.resolve()), cfg)
        args += ["-v", f"{host_task}:/config/task.md:ro"]
    if inline_prompt:
        args += ["-e", f"AGENT_INLINE_PROMPT={inline_prompt}"]
    if model:
        args += ["-e", f"AGENT_MODEL={model}"]
    if system_prompt_file:
        host_prompt = to_host_path(str(system_prompt_file.resolve()), cfg)
        args += ["-v", f"{host_prompt}:/config/persona.md:ro"]
        args += ["-e", "AGENT_SYSTEM_PROMPT_FILE=/config/persona.md"]
    if extra_env:
        for k, v in extra_env.items():
            args += ["-e", f"{k}={v}"]

    if not quiet:
        log.info("Starting agent in background...")
        if task_file:
            log.info(f"Task file: {task_file}")
        if inline_prompt:
            log.info("Inline prompt: provided")
        log.info(f"Repository: {repo_root}")
        print()

    container_id = subprocess.run(
        ["docker", "run", "--detach"] + args + [cfg.agent_image],
        capture_output=True, text=True,
    )

    if container_id.returncode != 0:
        log.error(f"Failed to start container: {container_id.stderr.strip()}")
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
        print(f"\nFollow progress (logs):\n  tail -f {workspace_host_path}/agent-output-{session}/agent.log")
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
    cfg: Config,
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
    tmp = cld_tmpdir(repo_root)
    diff_file = tmp / f"review-diff-{session}.patch"
    log.info(f"Generating diff: fork_point({feature_branch}, {trunk_branch}) -> {feature_branch}")

    fork = vcs.fork_point(feature_branch, trunk_branch)
    diff_content = vcs.diff_between(fork, feature_branch)

    if diff_content.startswith("Error:"):
        log.error(f"Failed to generate diff: {diff_content}")
        sys.exit(1)
    if not diff_content.strip():
        log.error("Generated diff is empty")
        sys.exit(1)

    diff_file.write_text(diff_content)
    log.info(f"Diff saved to: {diff_file}")

    # Create task from template
    template_path = cld_root / "imgs/claude-agent-review/review-template.md"
    if not template_path.is_file():
        log.error(f"Template not found: {template_path}")
        sys.exit(1)

    task_content = Template(template_path.read_text()).safe_substitute(
        TRUNK_BRANCH=trunk_branch,
        FEATURE_BRANCH=feature_branch,
        DIFF_FILE_PATH=f"{WORKSPACE_BASE}/origin/.cld/{diff_file.name}",
    )

    task_file = Path(tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix=f"review-task-{session}-", delete=False,
        dir=tmp,
    ).name)
    task_file.write_text(task_content)
    log.info(f"Task file created: {task_file}")
    print()

    return launch_agent(
        cfg,
        task_file=task_file,
        model=model,
        session_name=session,
    )
