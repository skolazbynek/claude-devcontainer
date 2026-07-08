"""Unit tests for cld.vcs.scratch peer-side staging.

Staging now happens inside the peer container's ephemeral workspace at
/workspace/current (a jj secondary workspace pointing at A). The tests use a
real jj secondary workspace under tmp_path to exercise the same code path.
"""

import subprocess
from pathlib import Path

import pytest

from cld.vcs.jj import JjBackend
from cld.vcs.scratch import (
    SCRATCH_DIR,
    decode_scratch_envelope,
    encode_scratch_envelope,
    stage_from_env,
    stage_in_workspace,
)


def _add_second_commit(path: Path) -> str:
    """Advance the seed repo by one commit; return the parent commit hash (A)."""
    (path / "a.txt").write_text("hello\n")
    subprocess.run(
        ["jj", "commit", "-m", "add a.txt"],
        cwd=path, check=True, capture_output=True,
    )
    result = subprocess.run(
        ["jj", "log", "-r", "@-", "--no-graph", "-T", "commit_id"],
        cwd=path, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _origin_wc_commit(origin_root: Path) -> str:
    """Return the commit id currently at the origin main workspace's @."""
    return subprocess.run(
        ["jj", "log", "-r", "@", "--no-graph", "-T", "commit_id"],
        cwd=origin_root, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _make_peer_workspace(origin: JjBackend, anchor: str, tmp_path: Path) -> Path:
    """Create a jj secondary workspace at *anchor* (mimics the container's `jj workspace add`)."""
    peer = tmp_path / "peer-workspace"
    subprocess.run(
        ["jj", "workspace", "add", "--name", "peer", "-r", anchor, str(peer)],
        cwd=origin.repo_root, check=True, capture_output=True,
    )
    return peer


class TestStageInWorkspace:
    def test_produces_child_of_anchor_with_scratch(self, jj_repo, tmp_path):
        anchor = _add_second_commit(jj_repo.repo_root)
        peer = _make_peer_workspace(jj_repo, anchor, tmp_path)

        b_hash = stage_in_workspace(
            peer, "sess_a",
            {"task.md": b"# task body\n", "sub/x.txt": b"y\n"},
        )

        parents = subprocess.run(
            ["jj", "log", "-r", b_hash, "--no-graph", "-T",
             'parents.map(|p| p.commit_id()).join(",")'],
            cwd=jj_repo.repo_root, check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert anchor in parents

        summary = subprocess.run(
            ["jj", "log", "-r", b_hash, "--no-graph", "--summary", "-T", '""'],
            cwd=jj_repo.repo_root, check=True, capture_output=True, text=True,
        ).stdout
        assert f"{SCRATCH_DIR}/task.md" in summary
        assert f"{SCRATCH_DIR}/sub/x.txt" in summary

    def test_origin_working_copy_untouched(self, jj_repo, tmp_path):
        """The critical invariant: origin's @ (main workspace WC) does not move."""
        anchor = _add_second_commit(jj_repo.repo_root)
        pre = _origin_wc_commit(jj_repo.repo_root)

        peer = _make_peer_workspace(jj_repo, anchor, tmp_path)
        stage_in_workspace(peer, "sess_b", {"session": b"sess_b\n"})

        post = _origin_wc_commit(jj_repo.repo_root)
        assert post == pre, "peer-side staging must not move origin's @"
        # And no .cld-run/ leaked into the origin working copy.
        assert not (jj_repo.repo_root / SCRATCH_DIR).exists()

    def test_at_is_a_case_does_not_rewrite_anchor(self, jj_repo, tmp_path):
        """The typical jj case: user's origin @ IS the anchor A. Staging must not rewrite A."""
        anchor = _origin_wc_commit(jj_repo.repo_root)  # @ IS A
        peer = _make_peer_workspace(jj_repo, anchor, tmp_path)

        b_hash = stage_in_workspace(peer, "sess_c", {"session": b"sess_c\n"})

        # A remains visible at its original commit hash (not rewritten, not hidden).
        vis = subprocess.run(
            ["jj", "log", "-r", "all()", "--no-graph", "-T",
             "commit_id ++ \"\\n\""],
            cwd=jj_repo.repo_root, check=True, capture_output=True, text=True,
        ).stdout
        assert anchor in vis
        # B is a distinct child of A.
        assert b_hash != anchor
        parents = subprocess.run(
            ["jj", "log", "-r", b_hash, "--no-graph", "-T",
             'parents.map(|p| p.commit_id()).join(",")'],
            cwd=jj_repo.repo_root, check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert anchor in parents


class TestScratchEnvelope:
    def test_roundtrip(self):
        scratch = {"session": b"hello world\n", "extra.md": b"\x00\x01\x02"}
        encoded = encode_scratch_envelope(scratch)
        assert isinstance(encoded, str)
        decoded = decode_scratch_envelope(encoded)
        assert decoded == scratch

    def test_decode_malformed_raises(self):
        with pytest.raises(RuntimeError):
            decode_scratch_envelope("not-base64!@#$")


class TestStageFromEnv:
    def test_stages_using_env(self, jj_repo, tmp_path, monkeypatch):
        anchor = _add_second_commit(jj_repo.repo_root)
        peer = _make_peer_workspace(jj_repo, anchor, tmp_path)

        monkeypatch.setenv("SESSION_NAME", "sess_env")
        monkeypatch.setenv("WORKSPACE_CURRENT", str(peer))
        monkeypatch.setenv(
            "AGENT_SCRATCH",
            encode_scratch_envelope({"session": b"sess_env\n"}),
        )
        b_hash = stage_from_env()
        assert b_hash

        show = subprocess.run(
            ["jj", "log", "-r", b_hash, "--no-graph", "--summary", "-T", '""'],
            cwd=jj_repo.repo_root, check=True, capture_output=True, text=True,
        )
        assert SCRATCH_DIR in show.stdout

    def test_missing_scratch_errors(self, jj_repo, monkeypatch):
        monkeypatch.setenv("SESSION_NAME", "sess_env")
        monkeypatch.delenv("AGENT_SCRATCH", raising=False)
        with pytest.raises(RuntimeError, match="AGENT_SCRATCH is required"):
            stage_from_env()

    def test_missing_session_errors(self, monkeypatch):
        monkeypatch.delenv("SESSION_NAME", raising=False)
        with pytest.raises(RuntimeError, match="SESSION_NAME is required"):
            stage_from_env()
