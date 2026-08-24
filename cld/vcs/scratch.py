"""Anchor scratch staging (peer-side).

A scratch commit B is created *inside the peer container's ephemeral workspace
at* ``/workspace/current`` — after ``jj workspace add -r <A> /workspace/current``.
The container writes ``.cld-run/*`` into that workspace and runs ``jj commit``,
which produces B as a child of A. Change A in the origin store is never
rewritten, and the user's origin working copy is never touched — even in the
common case where the user's ``@`` is A itself.

B only carries the scratch payload (task brief, session marker) — it is not
the enforced anchor. ``AGENT_ANCHOR_HASH`` is A itself, so any pre-existing
descendant of A, not just of B, is inside the container's editable tree.

The host (or a master delegating to a peer) passes:
- ``AGENT_REVISION_HINT``: the revision to anchor on (resolved commit hash from
  the host's jj view, or an unresolved revset string when the delegating master
  has no RW view of the target repo).
- ``AGENT_SCRATCH``: a base64 envelope of the scratch payload.

``stage_from_env()`` is the peer-side entry point. It's invoked from inside the
already-created workspace (cwd = ``/workspace/current``), writes the payload,
commits, and prints B's commit id to stdout; the entrypoint uses A (already
resolved before staging) as ``AGENT_ANCHOR_HASH``, not B.
"""

import base64
import json
import os
import sys
from pathlib import Path

from cld.log import get_logger
from cld.vcs.jj import JjBackend

log = get_logger(__name__)


SCRATCH_DIR = ".cld-run"


def stage_in_workspace(
    workspace_path: Path,
    session: str,
    scratch_files: dict[str, bytes],
    mode: str = "isolated",
) -> str:
    """Materialize scratch and commit B inside an already-created jj workspace.

    Args:
        workspace_path: path to a jj secondary workspace (typically ``/workspace/current``).
            Its ``@`` must be an empty child of A when this is called.
        session: session name; embedded in B's description.
        scratch_files: mapping of ``relative-path-under-.cld-run/`` -> bytes.
        mode: ``"isolated"`` (default) or ``"shared"`` -- embedded in B's
            description so a restart/reattach can recover which anchor
            semantics this session was launched with (see
            docs/design-anchor-modes.md). Isolated: the caller uses B itself
            as ``AGENT_ANCHOR_HASH``. Shared: the caller uses A (B's parent).

    Returns:
        Commit id of B, the scratch commit whose only diff vs. A is
        ``.cld-run/*``.
    """
    scratch_dir = workspace_path / SCRATCH_DIR
    scratch_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in scratch_files.items():
        dest = scratch_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)

    vcs = JjBackend(workspace_path, workspace_path=workspace_path)
    desc = f"cld anchor: {session} mode={mode}"
    result = vcs.run(["commit", "-m", desc, SCRATCH_DIR])
    if result.returncode != 0:
        raise RuntimeError(
            f"jj commit failed (rc={result.returncode}): "
            f"{(result.stderr or '').strip()}"
        )

    show = vcs.run(["log", "-r", "@-", "--no-graph", "-T", "commit_id", "-n", "1"])
    if show.returncode != 0 or not show.stdout.strip():
        raise RuntimeError("could not read anchor commit id from @-")
    b_hash = show.stdout.strip()
    log.info("staged anchor: %s (session=%s)", b_hash[:12], session)
    return b_hash


def encode_scratch_envelope(scratch: dict[str, bytes]) -> str:
    """Serialize `scratch` to a base64 string safe to carry through `docker run -e`."""
    inner = {k: base64.b64encode(v).decode("ascii") for k, v in scratch.items()}
    return base64.b64encode(json.dumps(inner).encode("utf-8")).decode("ascii")


def decode_scratch_envelope(payload: str) -> dict[str, bytes]:
    """Inverse of `encode_scratch_envelope`. Raises RuntimeError on malformed input."""
    try:
        inner = json.loads(base64.b64decode(payload).decode("utf-8"))
        return {k: base64.b64decode(v) for k, v in inner.items()}
    except (ValueError, TypeError) as e:
        raise RuntimeError(f"AGENT_SCRATCH could not be decoded: {e}") from e


def stage_from_env() -> str:
    """Peer-side entrypoint: stage anchor B in the current workspace.

    Called by the container entrypoint after ``jj workspace add -r <A>
    /workspace/current``, with cwd = the workspace (or ``WORKSPACE_CURRENT``
    exported to it).

    Env vars:
    - ``SESSION_NAME``  -- required.
    - ``AGENT_SCRATCH`` -- required, base64 envelope from ``encode_scratch_envelope``.
    - ``AGENT_ANCHOR_MODE`` -- optional, ``"isolated"`` (default) or ``"shared"``;
      embedded in B's description for restart/reattach recovery.
    - ``WORKSPACE_CURRENT`` -- optional; defaults to cwd.

    Returns B's commit id.
    """
    session = os.environ.get("SESSION_NAME") or ""
    if not session:
        raise RuntimeError("stage_from_env: SESSION_NAME is required")
    scratch_b64 = os.environ.get("AGENT_SCRATCH") or ""
    if not scratch_b64:
        raise RuntimeError("stage_from_env: AGENT_SCRATCH is required")
    mode = os.environ.get("AGENT_ANCHOR_MODE") or "isolated"
    workspace = Path(os.environ.get("WORKSPACE_CURRENT") or os.getcwd())
    scratch = decode_scratch_envelope(scratch_b64)
    return stage_in_workspace(workspace, session, scratch, mode)


if __name__ == "__main__":
    try:
        print(stage_from_env())
    except Exception as e:  # noqa: BLE001 -- surface any staging error to the shell caller
        print(f"stage_from_env failed: {e}", file=sys.stderr)
        sys.exit(1)
