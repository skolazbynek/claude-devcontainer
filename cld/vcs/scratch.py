"""Anchor + scratch staging.

`stage_anchor_with_scratch` writes cld-scratch files (`.cld-run/*`) into the
working copy of a repo, then uses `jj split --onto <anchor>` to extract them
into a dedicated child commit ``B`` of the anchor. ``B``'s hash becomes the
container's `AGENT_ANCHOR_HASH` so the agent's workspace is layered on top of
`B` (and thus sees the scratch files as tracked history rooted in the anchor
tree). The user's original working-copy change stays where it was, minus the
`.cld-run/` files.

`.cld-run/` MUST NOT be gitignored: the split relies on jj snapshotting those
paths into a commit. This module raises a clear error if the repo's ignore
rules would exclude them.

This module runs both host-side (traditional inline flow) and peer-side
(delegated flow used when a `cld master` container launches a sibling: see
docs/design-master-sibling-launch.md). The peer-side entrypoint invokes
`python -m cld.vcs.scratch` which reads the delegated envelope from env and
prints the resulting anchor hash to stdout.
"""

import base64
import json
import os
import sys
from pathlib import Path

from cld.log import get_logger
from cld.vcs import VcsBackend

log = get_logger(__name__)


SCRATCH_DIR = ".cld-run"


def _assert_scratch_not_ignored(repo_root: Path) -> None:
    """Raise if `.cld-run/` is excluded by any ignore rule (jj or git)."""
    gitignore = repo_root / ".gitignore"
    if gitignore.is_file():
        for lineno, raw in enumerate(gitignore.read_text().splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            stripped = line.lstrip("/").rstrip("/")
            if stripped in (SCRATCH_DIR, SCRATCH_DIR + "/*", SCRATCH_DIR + "/**"):
                raise RuntimeError(
                    f"{gitignore}:{lineno}: '{raw}' excludes {SCRATCH_DIR}/ which "
                    "cld needs to snapshot into the anchor commit. Remove the rule."
                )


def stage_anchor_with_scratch(
    vcs: VcsBackend,
    anchor_hash: str,
    session: str,
    scratch_files: dict[str, bytes],
) -> str:
    """Write scratch files, `jj split` them onto `anchor_hash`, return B's hash.

    Args:
        vcs: JjBackend rooted at the origin repo (raises if not jj).
        anchor_hash: commit hash of the anchor (A). Must exist.
        session: session name (used in the anchor commit's description).
        scratch_files: mapping of ``relative-path-under-.cld-run/`` -> bytes.

    Returns:
        The commit hash of ``B``: an immediate child of ``anchor_hash`` whose
        only diff vs. ``anchor_hash`` is the scratch files.

    Raises:
        NotImplementedError: if `vcs.name != "jj"`.
        RuntimeError: on any staging failure (with best-effort undo).
    """
    if vcs.name != "jj":
        raise NotImplementedError(
            "stage_anchor_with_scratch: jj backend required (got %r)" % vcs.name
        )

    repo_root = vcs.repo_root
    _assert_scratch_not_ignored(repo_root)

    scratch_dir = repo_root / SCRATCH_DIR
    written: list[Path] = []
    pre_op_id = _current_op_id(vcs)

    try:
        scratch_dir.mkdir(parents=True, exist_ok=True)
        for rel, content in scratch_files.items():
            dest = scratch_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
            written.append(dest)

        # Force a snapshot so the split sees the new files.
        vcs.run(["status"])

        desc = f"cld anchor: {session}"
        result = vcs.run([
            "split", "--onto", anchor_hash, "-m", desc, SCRATCH_DIR,
        ])
        if result.returncode != 0:
            raise RuntimeError(
                f"jj split failed (rc={result.returncode}): "
                f"{(result.stderr or '').strip()}"
            )

        b_hash = _resolve_split_child(vcs, anchor_hash, desc)
        if not b_hash:
            raise RuntimeError(
                f"could not locate split-produced child of {anchor_hash[:12]} "
                f"with description {desc!r}"
            )

        _verify_scratch_in_commit(vcs, b_hash)
        log.info("staged anchor: %s -> %s (session=%s)", anchor_hash[:12], b_hash[:12], session)
        return b_hash

    except Exception:
        log.error(
            "stage_anchor_with_scratch failed; attempting undo (pre_op_id=%s). "
            "If undo also fails, recover manually with: jj op restore %s",
            pre_op_id or "<unknown>", pre_op_id or "<pre-op-id>",
        )
        _best_effort_undo(vcs, pre_op_id, written)
        raise


def _current_op_id(vcs: VcsBackend) -> str:
    """Return the current jj operation id (for post-failure recovery)."""
    result = vcs.run(["op", "log", "--no-graph", "-T", "id ++ \"\\n\"", "-n", "1"])
    if result.returncode != 0:
        return ""
    line = result.stdout.strip().splitlines()
    return line[0] if line else ""


def _resolve_split_child(vcs: VcsBackend, anchor_hash: str, desc: str) -> str:
    """Find the commit id of the split-created child of *anchor_hash*.

    Uses a revset filter on children with an exact-match description; falls
    back to parsing stdout only if that revset returns nothing.
    """
    revset = f'children({anchor_hash}) & description(substring:"{desc}")'
    result = vcs.run(["log", "-r", revset, "--no-graph", "-T", "commit_id", "-n", "1"])
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return ""


def _verify_scratch_in_commit(vcs: VcsBackend, commit: str) -> None:
    """Ensure `commit`'s diff vs. its parent includes `.cld-run/` entries."""
    result = vcs.run(["log", "-r", commit, "--no-graph", "--summary", "-T", '""'])
    if result.returncode != 0:
        raise RuntimeError(f"jj log --summary failed for {commit}")
    if SCRATCH_DIR not in result.stdout:
        raise RuntimeError(
            f"staged commit {commit[:12]} does not contain {SCRATCH_DIR}/ entries: "
            f"{result.stdout[:400]}"
        )


def _best_effort_undo(vcs: VcsBackend, pre_op_id: str, written: list[Path]) -> None:
    """Roll the repo back to *pre_op_id* and delete host scratch files."""
    if pre_op_id:
        r = vcs.run(["op", "restore", pre_op_id])
        if r.returncode != 0:
            log.warning(
                "jj op restore %s failed (rc=%d): %s",
                pre_op_id, r.returncode, (r.stderr or "").strip(),
            )
    for p in written:
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            pass
    # Prune empty scratch dir if we created it.
    scratch_dir = vcs.repo_root / SCRATCH_DIR
    if scratch_dir.is_dir():
        try:
            for sub in sorted(scratch_dir.rglob("*"), reverse=True):
                if sub.is_dir() and not any(sub.iterdir()):
                    sub.rmdir()
            if not any(scratch_dir.iterdir()):
                scratch_dir.rmdir()
        except OSError:
            pass


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
    """Peer-side entrypoint: read the delegated envelope and stage the anchor.

    Reads:
    - ``WORKSPACE_ORIGIN`` -- absolute path to the repo checkout inside the peer
      container (RW). ``get_backend`` picks this up automatically.
    - ``SESSION_NAME``     -- required.
    - ``AGENT_REVISION_HINT`` -- unresolved revision string (empty = default @).
    - ``AGENT_SCRATCH``    -- base64 envelope from `encode_scratch_envelope`.

    Returns the resulting anchor commit hash (B).
    """
    from cld.vcs import get_backend
    from cld.vcs.anchor import resolve_anchor

    session = os.environ.get("SESSION_NAME") or ""
    if not session:
        raise RuntimeError("stage_from_env: SESSION_NAME is required")
    scratch_b64 = os.environ.get("AGENT_SCRATCH") or ""
    if not scratch_b64:
        raise RuntimeError("stage_from_env: AGENT_SCRATCH is required")
    scratch = decode_scratch_envelope(scratch_b64)

    vcs = get_backend()
    base = resolve_anchor(vcs, os.environ.get("AGENT_REVISION_HINT") or "")
    return stage_anchor_with_scratch(vcs, base, session, scratch)


if __name__ == "__main__":
    try:
        print(stage_from_env())
    except Exception as e:  # noqa: BLE001 -- surface any staging error to the shell caller
        print(f"stage_from_env failed: {e}", file=sys.stderr)
        sys.exit(1)
