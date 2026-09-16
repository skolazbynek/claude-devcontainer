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


_VCS_LIB = Path(__file__).resolve().parent.parent / "imgs/claude-devcontainer/vcs-lib.sh"


class TestRecoverAnchorSessionGlob:
    """cld_recover_anchor (vcs-lib.sh) globs for its own session's scratch
    description; the glob must not match a sibling session whose name extends
    this one (cld_ticket_x-1 vs cld_ticket_x-12)."""

    def _recover(self, repo_root: Path, bookmark: str, session: str) -> str:
        result = subprocess.run(
            ["bash", "-c",
             f'source "{_VCS_LIB}" && cd "{repo_root}" && '
             f'cld_recover_anchor "{bookmark}" "{session}"'],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def _staged_sibling(self, jj_repo, tmp_path) -> str:
        """Scratch commit of session cld_ticket_x-12, with a work commit on
        top carrying bookmark cld_ticket_x-1 -- the sibling's scratch is an
        ancestor of the probed bookmark. Returns the scratch commit id."""
        anchor = _add_second_commit(jj_repo.repo_root)
        peer = _make_peer_workspace(jj_repo, anchor, tmp_path)
        scratch = stage_in_workspace(peer, "cld_ticket_x-12", {"session": b"cld_ticket_x-12\n"})
        (peer / "work.txt").write_text("work\n")
        subprocess.run(
            ["jj", "commit", "-m", "work"], cwd=peer, check=True, capture_output=True,
        )
        subprocess.run(
            ["jj", "bookmark", "create", "cld_ticket_x-1", "-r", "@-"],
            cwd=peer, check=True, capture_output=True,
        )
        return scratch

    def test_own_session_matches(self, jj_repo, tmp_path):
        """Positive control: the exact session still recovers its scratch."""
        scratch = self._staged_sibling(jj_repo, tmp_path)
        assert self._recover(jj_repo.repo_root, "cld_ticket_x-1", "cld_ticket_x-12") == scratch

    def test_sibling_prefix_session_does_not_match(self, jj_repo, tmp_path):
        """Session x-1 has no scratch of its own; x-12's must not stand in."""
        self._staged_sibling(jj_repo, tmp_path)
        assert self._recover(jj_repo.repo_root, "cld_ticket_x-1", "cld_ticket_x-1") == ""


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


class TestStageFromEnvDefaultPayload:
    """`--default-payload` (ticket kind): no AGENT_SCRATCH, the session-marker
    payload is synthesized in-container (design-ticket-containers.md 4.2)."""

    def test_stages_without_agent_scratch(self, jj_repo, tmp_path, monkeypatch):
        anchor = _add_second_commit(jj_repo.repo_root)
        peer = _make_peer_workspace(jj_repo, anchor, tmp_path)

        monkeypatch.setenv("SESSION_NAME", "cld_ticket_lide-2600")
        monkeypatch.setenv("WORKSPACE_CURRENT", str(peer))
        monkeypatch.delenv("AGENT_SCRATCH", raising=False)
        b_hash = stage_from_env(default_payload=True)

        # B's shape is unchanged: session marker under .cld-run/, the
        # `cld anchor: <session> mode=<mode>` description.
        show = subprocess.run(
            ["jj", "log", "-r", b_hash, "--no-graph", "--summary", "-T", "description"],
            cwd=jj_repo.repo_root, check=True, capture_output=True, text=True,
        )
        assert f"{SCRATCH_DIR}/session" in show.stdout
        assert "cld anchor: cld_ticket_lide-2600 mode=isolated" in show.stdout

    def test_session_marker_content_is_session_name(self, jj_repo, tmp_path, monkeypatch):
        anchor = _add_second_commit(jj_repo.repo_root)
        peer = _make_peer_workspace(jj_repo, anchor, tmp_path)

        monkeypatch.setenv("SESSION_NAME", "cld_ticket_t1")
        monkeypatch.setenv("WORKSPACE_CURRENT", str(peer))
        monkeypatch.delenv("AGENT_SCRATCH", raising=False)
        stage_from_env(default_payload=True)

        assert (peer / SCRATCH_DIR / "session").read_text() == "cld_ticket_t1\n"

    def test_mode_env_still_lands_in_description(self, jj_repo, tmp_path, monkeypatch):
        anchor = _add_second_commit(jj_repo.repo_root)
        peer = _make_peer_workspace(jj_repo, anchor, tmp_path)

        monkeypatch.setenv("SESSION_NAME", "cld_ticket_t2")
        monkeypatch.setenv("WORKSPACE_CURRENT", str(peer))
        monkeypatch.setenv("AGENT_ANCHOR_MODE", "shared")
        b_hash = stage_from_env(default_payload=True)

        desc = subprocess.run(
            ["jj", "log", "-r", b_hash, "--no-graph", "-T", "description"],
            cwd=jj_repo.repo_root, check=True, capture_output=True, text=True,
        ).stdout
        assert "mode=shared" in desc

    def test_env_path_still_requires_agent_scratch(self, jj_repo, monkeypatch):
        """The v1 wire keeps its hard requirement when the flag is off."""
        monkeypatch.setenv("SESSION_NAME", "sess_env")
        monkeypatch.delenv("AGENT_SCRATCH", raising=False)
        with pytest.raises(RuntimeError, match="AGENT_SCRATCH is required"):
            stage_from_env(default_payload=False)
