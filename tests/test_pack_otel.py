"""Tests for otel/pack.sh -- the standalone-install packer for otel/ (see
docs/design-otel-standalone-install.md). Runs the real script via
subprocess, following tests/test_broker_sh.py's convention: no bespoke shell
harness, just the script exercised the way a user would run it.

Marked integration: these hit the real filesystem and (for provenance) the
real jj repo this test suite lives in.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
OTEL_DIR = REPO_ROOT / "otel"
PACK_SH = OTEL_DIR / "pack.sh"

# otel/pack.sh's own non-recursive discovery excludes these; mirror it here
# so the "every file, verbatim" test compares against the same set pack.sh
# itself would pick up, without hard-coding the file list twice.
_EXCLUDED_PREFIXES = ("otel-standalone-",)
_EXCLUDED_SUFFIXES = (".tar.gz", ".tgz")
_EXCLUDED_NAMES = {"aggregate.log", "aggregate.pid", "PROVENANCE"}


def _expected_source_files():
    names = []
    for f in OTEL_DIR.iterdir():
        if not f.is_file():
            continue
        if f.name.startswith(".") or f.name in _EXCLUDED_NAMES:
            continue
        if f.name.startswith(_EXCLUDED_PREFIXES) or f.name.endswith(_EXCLUDED_SUFFIXES):
            continue
        names.append(f.name)
    return sorted(names)


def _run(args, cwd, env=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", *args],
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
    )


def _pack(tmp_path, extra_args=(), pack_sh=PACK_SH, quiet=True):
    out = tmp_path / "artifact.out"
    args = [str(pack_sh), "--out", str(out)]
    if quiet:
        args.append("--quiet")
    args.extend(extra_args)
    result = _run(args, cwd=tmp_path)
    assert result.returncode == 0, f"pack.sh failed: {result.stdout}\n{result.stderr}"
    return out, result


def _extract(artifact, args=(), cwd=None, env=None):
    full_args = [str(artifact), *args]
    return _run(full_args, cwd=cwd, env=env)


pytestmark = pytest.mark.integration


class TestPackIncludesEveryFileVerbatim:
    def test_tarball_matches_source_tree(self, tmp_path):
        tarball, _ = _pack(tmp_path, extra_args=["--tarball"])
        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()
        subprocess.run(
            ["tar", "xzf", str(tarball), "-C", str(extract_dir)], check=True
        )
        extracted_otel = extract_dir / "otel"

        expected = set(_expected_source_files())
        actual = {
            f.name for f in extracted_otel.iterdir() if f.is_file() and f.name != "PROVENANCE"
        }
        assert actual == expected

        for name in expected:
            assert (extracted_otel / name).read_bytes() == (OTEL_DIR / name).read_bytes(), name

    def test_provenance_present_and_not_in_expected_set(self, tmp_path):
        tarball, _ = _pack(tmp_path, extra_args=["--tarball"])
        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()
        subprocess.run(
            ["tar", "xzf", str(tarball), "-C", str(extract_dir)], check=True
        )
        assert (extract_dir / "otel" / "PROVENANCE").is_file()


class TestSelfExtractingRoundtrip:
    def test_extracts_expected_files_and_modes(self, tmp_path):
        artifact, _ = _pack(tmp_path)
        target = tmp_path / "otel-out"
        result = _extract(artifact, args=["--dir", str(target)], cwd=tmp_path)
        assert result.returncode == 0, result.stderr

        expected = set(_expected_source_files()) | {"PROVENANCE"}
        actual = {f.name for f in target.iterdir() if f.is_file()}
        assert actual == expected

        for exe in ("otelctl.sh", "aggregate.py", "pack.sh"):
            mode = (target / exe).stat().st_mode
            assert mode & stat.S_IXUSR, f"{exe} not executable"

    def test_provenance_names_a_source_rev(self, tmp_path):
        artifact, _ = _pack(tmp_path)
        target = tmp_path / "otel-out"
        _extract(artifact, args=["--dir", str(target)], cwd=tmp_path)
        provenance = (target / "PROVENANCE").read_text()
        assert "Source rev:" in provenance
        rev_line = [l for l in provenance.splitlines() if l.startswith("Source rev:")][0]
        assert rev_line.split(":", 1)[1].strip() != ""
        assert "unknown" not in rev_line


class TestCheckDetectsTampering:
    def test_check_ok_on_untouched_artifact(self, tmp_path):
        artifact, _ = _pack(tmp_path)
        result = _extract(artifact, args=["--check"], cwd=tmp_path)
        assert result.returncode == 0, result.stderr

    def test_check_and_extract_reject_tampered_payload(self, tmp_path):
        artifact, _ = _pack(tmp_path)
        data = artifact.read_bytes()
        marker = b"__OTEL_PAYLOAD_BELOW__\n"
        idx = data.index(marker) + len(marker)
        # Flip one base64 character well inside the payload region.
        pos = idx + 40
        original = data[pos]
        replacement = ord("A") if chr(original) != "A" else ord("B")
        tampered = bytearray(data)
        tampered[pos] = replacement
        artifact.write_bytes(bytes(tampered))

        check_result = _extract(artifact, args=["--check"], cwd=tmp_path)
        assert check_result.returncode == 4, check_result.stdout

        target = tmp_path / "otel-tampered"
        extract_result = _extract(artifact, args=["--dir", str(target)], cwd=tmp_path)
        assert extract_result.returncode == 4, extract_result.stdout
        assert not target.exists() or not any(target.iterdir())


class TestRefusesNonemptyTarget:
    def test_exits_3_without_force(self, tmp_path):
        artifact, _ = _pack(tmp_path)
        target = tmp_path / "otel-out"
        target.mkdir()
        (target / "keep.txt").write_text("pre-existing\n")

        result = _extract(artifact, args=["--dir", str(target)], cwd=tmp_path)
        assert result.returncode == 3
        assert (target / "keep.txt").exists()
        assert not (target / "otelctl.sh").exists()

    def test_succeeds_with_force(self, tmp_path):
        artifact, _ = _pack(tmp_path)
        target = tmp_path / "otel-out"
        target.mkdir()
        (target / "keep.txt").write_text("pre-existing\n")

        result = _extract(artifact, args=["--dir", str(target), "--force"], cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert (target / "otelctl.sh").exists()


class TestListExtractsNothing:
    def test_list_prints_manifest_and_creates_no_files(self, tmp_path):
        artifact, _ = _pack(tmp_path)
        result = _extract(artifact, args=["--list"], cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert "Source rev:" in result.stdout
        assert "otelctl.sh" in result.stdout
        assert not (tmp_path / "otel").exists()


class TestArtifactHasNoCldPaths:
    def test_no_dangling_relative_cld_path(self, tmp_path):
        artifact, _ = _pack(tmp_path)
        target = tmp_path / "otel-out"
        _extract(artifact, args=["--dir", str(target)], cwd=tmp_path)

        for f in target.iterdir():
            if not f.is_file():
                continue
            text = f.read_text(errors="ignore")
            assert "../cld" not in text, f"{f.name} still contains a path-shaped ../cld reference"


class TestRepackFromExtractedCopy:
    def test_repack_carries_forward_original_rev(self, tmp_path):
        first_artifact, _ = _pack(tmp_path)
        extracted = tmp_path / "otel-extracted"
        _extract(first_artifact, args=["--dir", str(extracted)], cwd=tmp_path)
        original_provenance = (extracted / "PROVENANCE").read_text()
        original_rev_line = [
            l for l in original_provenance.splitlines() if l.startswith("Source rev:")
        ][0]
        original_rev = original_rev_line.split(":", 1)[1].strip()

        second_out_dir = tmp_path / "repack-out"
        second_out_dir.mkdir()
        second_artifact, result = _pack(
            second_out_dir, pack_sh=extracted / "pack.sh"
        )
        assert result.returncode == 0, result.stderr

        reextracted = tmp_path / "otel-reextracted"
        _extract(second_artifact, args=["--dir", str(reextracted)], cwd=tmp_path)
        new_provenance = (reextracted / "PROVENANCE").read_text()
        assert original_rev in new_provenance
        assert "re-packed from a standalone copy" in new_provenance
