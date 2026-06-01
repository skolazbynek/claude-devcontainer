"""Agent and review launch logic."""

import subprocess
import sys
from pathlib import Path
from string import Template

from cld.config import Config
from cld.docker import (
    agent_extra_paths,
    base_extra_paths,
    build_container_args,
    build_session_name,
    ensure_image,
    find_repo_context,
    require_docker,
    to_host_path,
    WORKSPACE_BASE,
)
from cld.log import get_logger
from cld.vcs import get_backend
from cld.vcs.anchor import assert_descendant, create_editable_root, resolve_anchor

log = get_logger(__name__)


def agent_workspace_path(repo_root: Path, session: str) -> Path:
    """Host path where an agent's per-session workspace lives."""
    return repo_root / ".cld" / "workspaces" / session


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
    workspace_path: Path | None = None,
    anchor_hash: str | None = None,
) -> dict:
    """Launch an autonomous Claude agent in a Docker container.

    If ``workspace_path`` and ``anchor_hash`` are provided the caller has
    already created the editable_root workspace; otherwise this function
    creates one. Either way, ``AGENT_ANCHOR_HASH`` is propagated into the
    container so the in-container guard can enforce immutability.
    """
    require_docker()
    if not task_file and not inline_prompt:
        log.error("No task file or prompt provided")
        sys.exit(1)

    repo_root, _workspace_rev = find_repo_context()
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

    if workspace_path is None:
        anchor_hash = resolve_anchor(vcs, revision)
        workspace_path = agent_workspace_path(repo_root, session)
        create_editable_root(vcs, anchor_hash, workspace_path, session)
    elif anchor_hash is None:
        raise RuntimeError("workspace_path given without anchor_hash")

    args = ["--name", session]
    args += build_container_args(repo_root, session, cfg)
    host_ws = to_host_path(str(workspace_path), cfg)
    args += ["-v", f"{host_ws}:{WORKSPACE_BASE}/current"]
    args += ["-e", "WORKSPACE_PREINITIALIZED=1"]
    args += ["-e", f"AGENT_ANCHOR_HASH={anchor_hash}"]
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
        log.info(f"Anchor: {anchor_hash[:12]}")
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
        print(f"Anchor:       {anchor_hash[:12]}")
        print(f"Check if running:\n  docker ps --filter id={cid}")
        print(f"\nFollow progress (logs):\n  tail -f {workspace_path}/agent-output-{session}/agent.log")
        print(f"\nWait for completion:\n  docker wait {cid}")
        if vcs_name == "jj":
            print(f"\nAfter completion, view results:\n  jj log -r '{anchor_hash}..{session}'\n  jj diff --from {anchor_hash} --to {session}")
            print(f"  cat {repo_root}/agent-output-{session}/summary.json")
            print(f"\nMerge changes:\n  jj squash --from {session}")
        else:
            print(f"\nAfter completion, view results:\n  git log {anchor_hash}..{session}\n  git diff {anchor_hash}..{session}")
            print(f"  cat {repo_root}/agent-output-{session}/summary.json")
            print(f"\nMerge changes:\n  git merge {session}")
        print()

    return {
        "container_id": cid,
        "session_name": session,
        "repo_root": str(repo_root),
        "workspace_path": str(workspace_path),
        "anchor_hash": anchor_hash,
    }


def launch_review(
    cfg: Config,
    feature_branch: str,
    trunk_branch: str,
    name: str = "",
    model: str = "",
    revision: str = "",
) -> dict:
    """Generate a diff between two branches and launch a code review agent."""
    vcs = get_backend()
    repo_root = vcs.repo_root
    cld_root = Path(__file__).resolve().parent.parent

    session = build_session_name("review", name)

    anchor = resolve_anchor(vcs, revision)
    workspace_path = agent_workspace_path(repo_root, session)
    create_editable_root(vcs, anchor, workspace_path, session)

    scratch = workspace_path / ".cld-run"
    scratch.mkdir(parents=True, exist_ok=True)

    diff_file = scratch / f"review-diff-{session}.patch"
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

    template_path = cld_root / "imgs/claude-agent-review/review-template.md"
    if not template_path.is_file():
        log.error(f"Template not found: {template_path}")
        sys.exit(1)
    task_content = Template(template_path.read_text()).safe_substitute(
        TRUNK_BRANCH=trunk_branch,
        FEATURE_BRANCH=feature_branch,
        DIFF_FILE_PATH=f"{WORKSPACE_BASE}/current/.cld-run/{diff_file.name}",
    )
    task_file = scratch / f"review-task-{session}.md"
    task_file.write_text(task_content)
    log.info(f"Task file created: {task_file}")
    print()

    return launch_agent(
        cfg,
        task_file=task_file,
        model=model,
        session_name=session,
        workspace_path=workspace_path,
        anchor_hash=anchor,
    )
