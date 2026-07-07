"""One-shot Claude agent (`cld run`) launch logic."""

import subprocess
import sys
from pathlib import Path

from cld.config import Config
from cld.docker import (
    base_extra_paths,
    build_container_args,
    build_session_name,
    ensure_image,
    find_repo_context,
    require_docker,
    run_extra_paths,
    to_host_path,
)
from cld.log import get_logger
from cld.vcs import get_backend
from cld.vcs.anchor import resolve_anchor
from cld.vcs.scratch import stage_anchor_with_scratch

log = get_logger(__name__)


def launch_run(
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
    anchor_hash: str | None = None,
) -> dict:
    """Launch a one-shot autonomous Claude agent in a Docker container.

    If ``anchor_hash`` is provided the caller has already staged the anchor
    commit (via ``stage_anchor_with_scratch``); otherwise this function does
    the staging itself. ``AGENT_ANCHOR_HASH`` (== B) is propagated into the
    container so the in-container entrypoint can create the workspace on top
    of it and the descendant guard can enforce immutability.
    """
    require_docker()
    if not task_file and not inline_prompt:
        log.error("No task file or prompt provided")
        sys.exit(1)

    repo_root, _workspace_rev = find_repo_context()
    vcs = get_backend()

    cld_root = Path(__file__).resolve().parent.parent
    ensure_image(
        cfg.run_image,
        cld_root / "imgs/claude-run/Dockerfile.claude-run",
        cld_root / "imgs/claude-run",
        extra_paths=run_extra_paths(cld_root),
        parent_image=(
            cfg.base_image,
            cld_root / "imgs/claude-base/Dockerfile.claude-base",
            cld_root,
            base_extra_paths(cld_root),
        ),
        quiet=quiet,
    )

    session = session_name or build_session_name("run", name)

    # Host stages an anchor commit B (child of the resolved anchor A) containing
    # only `.cld-run/*`. B's hash is the AGENT_ANCHOR_HASH the container reads;
    # the in-container entrypoint creates the workspace on top of B and the
    # descendant guard enforces that all further commits descend from it.
    if anchor_hash is None:
        base_anchor = resolve_anchor(vcs, revision)
        anchor_hash = stage_anchor_with_scratch(
            vcs, base_anchor, session,
            {"session": f"{session}\n".encode()},
        )

    args = ["--name", session]
    args += build_container_args(repo_root, session, cfg)
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
        ["docker", "run", "--detach"] + args + [cfg.run_image],
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
        print(f"\nWait for completion:\n  docker wait {cid}")
        if vcs_name == "jj":
            print(f"\nAfter completion, view results:\n  jj log -r '{anchor_hash}..{session}'\n  jj diff --from {anchor_hash} --to {session}")
            print(f"\nMerge changes:\n  jj squash --from {session}")
        else:
            print(f"\nAfter completion, view results:\n  git log {anchor_hash}..{session}\n  git diff {anchor_hash}..{session}")
            print(f"\nMerge changes:\n  git merge {session}")
        print()

    return {
        "container_id": cid,
        "session_name": session,
        "repo_root": str(repo_root),
        "anchor_hash": anchor_hash,
    }
