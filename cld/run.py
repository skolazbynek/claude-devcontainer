"""One-shot Claude agent (`cld run`) launch logic."""

import subprocess
import sys
from pathlib import Path

from cld.config import Config
from cld.docker import (
    anchor_env_args,
    base_extra_paths,
    build_container_args,
    build_session_name,
    ensure_image,
    find_target_repo,
    in_master_container,
    require_docker,
    run_extra_paths,
    to_host_path,
)
from cld.log import get_logger
from cld.vcs import get_backend

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

    repo_root = find_target_repo(cfg)

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

    # Anchor: on the host (traditional flow), stage commit B inline and pass
    # AGENT_ANCHOR_HASH. Inside master (delegated flow), pass AGENT_REVISION_HINT
    # + AGENT_SCRATCH and let the peer stage locally.
    # `anchor_hash` may be pre-staged by a caller (chain runner); if so, use it
    # directly. Chain-inside-master is blocked upstream, so we don't handle it.
    args = ["--name", session]
    args += build_container_args(repo_root, session, cfg)
    if anchor_hash is not None:
        args += ["-e", f"AGENT_ANCHOR_HASH={anchor_hash}"]
    else:
        args += anchor_env_args(cfg, session, revision)
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
        if anchor_hash:
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
        print(f"Container ID: {cid}")
        print()
        print("========================================")
        print("Agent started successfully")
        print("========================================")
        print()
        if anchor_hash:
            print(f"Anchor:       {anchor_hash[:12]}")
        print(f"Check if running:\n  docker ps --filter id={cid}")
        print(f"\nWait for completion:\n  docker wait {cid}")
        # The anchor hash is only known synchronously in the host flow. In the
        # delegated flow the peer stages the anchor itself; the resulting hash
        # appears in the peer's summary.json (anchor_hash) once it completes.
        if anchor_hash and not in_master_container():
            vcs_name = get_backend().name
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
