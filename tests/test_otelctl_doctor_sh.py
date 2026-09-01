"""Tests for `otelctl.sh doctor` (otel/otelctl.sh), a bash+embedded-python
health check for the standalone OTel pipeline. Docker is not assumed to be
available (task-agent containers generally have no daemon socket), so these
tests only exercise logic that doesn't require a real collector container:

- doctor_check_env: the shell-env verdict table (TestDoctorCheckEnv), run
  directly via bash following the tests/test_broker_sh.py precedent.
- doctor_summary: the ok/warn/fail/skip -> summary line + verdict mapping
  (TestDoctorSummary), same technique.
- The synthetic round-trip and its isolated temp-dir replay fallback
  (TestDoctorRoundTrip), run as a real end-to-end `otelctl.sh doctor`
  invocation against a stand-in OTLP/HTTP receiver (plain http.server, no
  Docker) that replicates just the HTTP contract the real collector exposes
  -- this is the same technique used to manually verify doctor against the
  three states in the task brief, now automated.

Never point a one-shot aggregate.py pass at a real stats directory that a
live --watch might also be using (see docs/design-otel-doctor.md) -- every
CLD_OTEL_DIR here is a fresh tmp_path, never a real one.
"""

import json
import os
import re
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

OTELCTL_SH = Path(__file__).resolve().parent.parent / "otel" / "otelctl.sh"
AGGREGATE_PY = Path(__file__).resolve().parent.parent / "otel" / "aggregate.py"


def _extract_function(name: str) -> str:
    text = OTELCTL_SH.read_text()
    match = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}", text, re.MULTILINE | re.DOTALL)
    assert match, f"{name}() not found in {OTELCTL_SH}"
    return match.group(0)


def _run_function(name: str, body_snippet: str, env: dict) -> subprocess.CompletedProcess:
    script = "set -euo pipefail\n" + _extract_function(name) + "\n" + body_snippet
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


class TestDoctorCheckEnv:
    """doctor_check_env's shell-env verdict table -- run for real via bash,
    not reimplemented in Python, so a change to the real logic breaks this
    test rather than a parallel copy of it."""

    def _run_env(self, env_overrides: dict) -> subprocess.CompletedProcess:
        env = {"PATH": os.environ["PATH"], "PORT": "4318"}
        env.update(env_overrides)
        return _run_function("doctor_check_env", "doctor_report() { echo \"$1|$2|$3|${4:-}\"; }\ndoctor_check_env", env)

    def test_nothing_set_is_advisory_warn_only(self):
        result = self._run_env({})
        assert result.returncode == 0, result.stderr
        lines = result.stdout.strip().splitlines()
        assert len(lines) == 1
        assert lines[0].startswith("warn|shell env|no telemetry variables set")

    def test_fully_correct_env_is_all_ok(self):
        result = self._run_env(
            {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_METRICS_EXPORTER": "otlp",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318",
                "OTEL_RESOURCE_ATTRIBUTES": "service.name=my-session",
            }
        )
        assert result.returncode == 0, result.stderr
        states = [line.split("|", 1)[0] for line in result.stdout.strip().splitlines()]
        assert "fail" not in states
        assert states.count("ok") >= 5

    def test_missing_enable_telemetry_is_fail(self):
        result = self._run_env(
            {
                "OTEL_METRICS_EXPORTER": "otlp",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318",
            }
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("fail|shell env|CLAUDE_CODE_ENABLE_TELEMETRY is not set") for l in lines)

    def test_wrong_enable_telemetry_value_is_warn_not_fail(self):
        result = self._run_env(
            {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "true",
                "OTEL_METRICS_EXPORTER": "otlp",
            }
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith('warn|shell env|CLAUDE_CODE_ENABLE_TELEMETRY=true') for l in lines)

    def test_exporter_missing_otlp_is_fail(self):
        result = self._run_env(
            {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_METRICS_EXPORTER": "console"}
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("fail|shell env|OTEL_METRICS_EXPORTER=console does not include otlp") for l in lines)

    def test_exporter_comma_list_including_otlp_is_ok(self):
        result = self._run_env(
            {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_METRICS_EXPORTER": "console,otlp"}
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("ok|shell env|OTEL_METRICS_EXPORTER includes otlp") for l in lines)

    def test_metrics_protocol_overrides_general_protocol(self):
        result = self._run_env(
            {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
                "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL": "http/json",
            }
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("ok|shell env|protocol http/json") for l in lines)

    def test_grpc_protocol_is_fail(self):
        result = self._run_env({"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"})
        lines = result.stdout.strip().splitlines()
        assert any("OTEL_EXPORTER_OTLP_PROTOCOL=grpc" in l and l.startswith("fail") for l in lines)

    def test_endpoint_wrong_port_is_fail(self):
        result = self._run_env(
            {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:9999"}
        )
        lines = result.stdout.strip().splitlines()
        assert any("port 9999 != collector port 4318" in l for l in lines)

    def test_endpoint_https_is_fail(self):
        result = self._run_env(
            {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_EXPORTER_OTLP_ENDPOINT": "https://localhost:4318"}
        )
        lines = result.stdout.strip().splitlines()
        assert any('scheme "https"' in l for l in lines)

    def test_endpoint_host_docker_internal_is_ok_never_resolution_tested(self):
        """Trap 1 from the architect's amendment: host.docker.internal is the
        *correct* endpoint for a containerised session and does not resolve
        on the host doctor itself runs on -- it must be accepted outright,
        never sent through a resolution probe that would report it broken.
        PYTHON_OK is deliberately left unset here: if this ever regressed to
        probing resolution, it would hit the no-python skip branch instead
        of ok, so this test would catch that too."""
        result = self._run_env(
            {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://host.docker.internal:4318"}
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("ok|shell env|endpoint http://host.docker.internal:4318") for l in lines)

    def test_endpoint_literal_ip_is_ok_never_resolution_tested(self):
        result = self._run_env(
            {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://172.17.0.1:4318"}
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("ok|shell env|endpoint http://172.17.0.1:4318") for l in lines)

    def test_endpoint_unrecognized_host_without_python_is_skip_not_fail(self):
        """Trap 2: no getent, and a missing python3 must not be inferred as
        an unresolvable host -- that would false-fail every environment
        without python3 for a hostname doctor simply can't check."""
        result = self._run_env(
            {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://example.com:4318", "PYTHON_OK": "0"}
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith('skip|shell env|endpoint host "example.com" resolution') for l in lines)
        assert not any('does not resolve' in l for l in lines)

    def test_endpoint_unresolvable_host_with_python_is_fail(self):
        result = self._run_env(
            {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://totally-not-a-real-host-xyz123.invalid:4318",
                "PYTHON_OK": "1",
                "PYTHON": "python3",
            }
        )
        lines = result.stdout.strip().splitlines()
        assert any('host "totally-not-a-real-host-xyz123.invalid" does not resolve' in l and l.startswith("fail") for l in lines)

    def test_endpoint_resolvable_but_unrecognized_host_is_fail(self):
        """A hostname that resolves fine but isn't loopback/host.docker.internal/
        a literal IP is still a fail -- most likely a copy-pasted endpoint
        pointing at a different machine's collector."""
        result = self._run_env(
            {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://example.com:4318",
                "PYTHON_OK": "1",
                "PYTHON": "python3",
            }
        )
        lines = result.stdout.strip().splitlines()
        assert any(
            'host "example.com" is neither loopback, host.docker.internal, nor a literal IP' in l and l.startswith("fail")
            for l in lines
        )

    def test_resource_attributes_with_space_is_fail(self):
        result = self._run_env(
            {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_RESOURCE_ATTRIBUTES": "service.name=my session"}
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("fail|shell env|OTEL_RESOURCE_ATTRIBUTES contains a space") for l in lines)

    def test_cumulative_temporality_is_fail(self):
        result = self._run_env(
            {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "Cumulative",
            }
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("fail|shell env|OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative") for l in lines)

    def test_include_session_id_false_is_warn(self):
        result = self._run_env(
            {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_METRICS_INCLUDE_SESSION_ID": "false"}
        )
        lines = result.stdout.strip().splitlines()
        assert any("collapses into unknown-session.json" in l for l in lines)


class TestDoctorCheckCollectorMountsDrift:
    """doctor_check_collector's config-edited-since-start sub-check
    (otel/otelctl.sh doctor_check_collector, the mounts branch). Docker
    itself is stubbed on PATH (tests/test_broker_sh.py precedent), so this
    exercises real bash + real `date`/`stat` -- only the docker daemon is
    faked, not the platform-portability logic under test.

    Regression coverage for the architect-flagged GNU-only `date -d` /
    `stat -c %Y` bug: on a platform where neither can be parsed, the drift
    sub-check must report `skip`, never `ok` -- an `ok` here would silently
    claim "config not edited since start" on a platform that was never
    actually checked.
    """

    @pytest.fixture
    def fakedocker(self, tmp_path):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "docker").write_text("""#!/usr/bin/env bash
if [ "$1" = "inspect" ]; then
    shift
    fmt=""
    while [ $# -gt 0 ]; do
        case "$1" in -f) fmt="$2"; shift 2 ;; *) shift ;; esac
    done
    case "$fmt" in
        '{{.State.Status}}') echo "${FAKE_STATUS:-running}" ;;
        '{{.State.ExitCode}}') echo "0" ;;
        '{{.State.Error}}') echo "" ;;
        '{{.State.RestartCount}}') echo "${FAKE_RESTART_COUNT:-0}" ;;
        '{{.Config.Image}}') echo "otel-image:test" ;;
        '{{.State.StartedAt}}') echo "${FAKE_STARTED_AT:-2024-01-01T00:00:00.000000000Z}" ;;
        '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}') echo "${FAKE_DATA_SRC:-}" ;;
        '{{range .Mounts}}{{if eq .Destination "/etc/otelcol-contrib/config.yaml"}}{{.Source}}{{end}}{{end}}') echo "${FAKE_CFG_SRC:-}" ;;
    esac
    exit 0
fi
exit 1
""")
        (bindir / "docker").chmod(0o755)
        return bindir

    def _run(self, fakedocker, tmp_path, env_overrides: dict, extra_path: str = "", cfg_mtime: float = None):
        here = tmp_path / "here"
        here.mkdir()
        state = tmp_path / "state"
        cfg = here / "otel-collector-config.yaml"
        cfg.write_text("receivers: {}\n")
        if cfg_mtime is not None:
            os.utime(cfg, (cfg_mtime, cfg_mtime))

        env = {
            "PATH": f"{extra_path}{':' if extra_path else ''}{fakedocker}:{os.environ['PATH']}",
            "CONTAINER_NAME": "cld-otel-collector",
            "DATA_DIR": str(state),
            "HERE": str(here),
            "FAKE_DATA_SRC": f"{state}/data",
            "FAKE_CFG_SRC": str(cfg),
        }
        env.update(env_overrides)
        return _run_function(
            "doctor_check_collector",
            'doctor_report() { echo "$1|$2|$3|${4:-}"; }\ndoctor_check_collector',
            env,
        )

    def test_config_untouched_since_start_is_ok(self, fakedocker, tmp_path):
        """cfg mtime predates started_at -- not edited since start."""
        started = time.time()
        result = self._run(
            fakedocker,
            tmp_path,
            {"FAKE_STARTED_AT": time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime(started))},
            cfg_mtime=started - 3600,
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("ok|mounts|/data ->") for l in lines), result.stdout

    def test_config_edited_after_start_is_warn(self, fakedocker, tmp_path):
        """cfg mtime postdates started_at -- edited since start."""
        started = time.time() - 3600
        result = self._run(
            fakedocker,
            tmp_path,
            {"FAKE_STARTED_AT": time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime(started))},
            cfg_mtime=time.time(),
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("warn|mounts|") and "edited since collector started" in l for l in lines), result.stdout

    def test_unparseable_start_time_is_skip_not_ok(self, fakedocker, tmp_path):
        """Neither GNU `date -d` nor the BSD `date -j -u -f` fallback can
        parse this -- started_epoch stays empty, and the drift sub-check
        must not silently claim ok."""
        result = self._run(fakedocker, tmp_path, {"FAKE_STARTED_AT": "not-a-timestamp"})
        lines = result.stdout.strip().splitlines()
        assert any(
            l.startswith("skip|mounts|config-edited-since-start check|") and "could not be parsed" in l for l in lines
        ), result.stdout
        assert not any(l.startswith("ok|mounts|") for l in lines)

    def test_unreadable_mtime_is_skip_not_ok(self, fakedocker, tmp_path):
        """A `stat` that supports neither GNU `-c %Y` nor BSD `-f %m` must
        not be silently treated as 'not edited' -- pin the skip path
        distinct from the unparseable-start-time path above."""
        started = time.time() - 3600
        badstat_dir = tmp_path / "badstat-bin"
        badstat_dir.mkdir()
        (badstat_dir / "stat").write_text("#!/usr/bin/env bash\nexit 1\n")
        (badstat_dir / "stat").chmod(0o755)
        result = self._run(
            fakedocker,
            tmp_path,
            {"FAKE_STARTED_AT": time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime(started))},
            extra_path=str(badstat_dir),
        )
        lines = result.stdout.strip().splitlines()
        assert any(
            l.startswith("skip|mounts|config-edited-since-start check|") and "could not read" in l for l in lines
        ), result.stdout
        assert not any(l.startswith("ok|mounts|") for l in lines)


class TestDoctorSummary:
    """doctor_summary's ok/warn/fail/skip counts -> summary line + verdict.
    Regression-pins the comma-spacing bug (IFS=', ' only honors the first
    char of IFS when joining ${arr[*]}) and the shell-env-only-failure
    verdict branch."""

    def _run(self, counts: dict, fail_labels=()) -> subprocess.CompletedProcess:
        env = {"PATH": os.environ["PATH"]}
        labels_decl = " ".join(f'"{l}"' for l in fail_labels)
        body = f"""
DOCTOR_OK_COUNT={counts.get('ok', 0)}
DOCTOR_WARN_COUNT={counts.get('warn', 0)}
DOCTOR_FAIL_COUNT={counts.get('fail', 0)}
DOCTOR_SKIP_COUNT={counts.get('skip', 0)}
DOCTOR_FIRST_FAIL_DETAIL="first fail detail"
DOCTOR_FAIL_LABELS_ARR=({labels_decl})
doctor_summary
"""
        return _run_function("doctor_summary", body, env)

    def test_all_ok_no_failures_flowing(self):
        result = self._run({"ok": 4})
        assert result.returncode == 0, result.stderr
        assert result.stdout == "4 ok -- telemetry is flowing\n"

    def test_counts_join_with_comma_space(self):
        result = self._run({"ok": 4, "warn": 3, "fail": 5, "skip": 3}, fail_labels=["docker"] * 5)
        first_line = result.stdout.splitlines()[0]
        assert first_line == "4 ok, 3 warnings, 5 failures, 3 skipped -- telemetry is NOT being collected"

    def test_singular_warning_and_failure(self):
        result = self._run({"ok": 1, "warn": 1, "fail": 1}, fail_labels=["docker"])
        first_line = result.stdout.splitlines()[0]
        assert "1 warning" in first_line
        assert "1 failure" in first_line
        assert "1 warnings" not in first_line
        assert "1 failures" not in first_line

    def test_next_line_only_when_failures_present(self):
        result_ok = self._run({"ok": 4})
        assert "next:" not in result_ok.stdout
        result_fail = self._run({"ok": 4, "fail": 1}, fail_labels=["docker"])
        assert result_fail.stdout.splitlines()[-1] == "next: first fail detail"

    def test_all_failures_shell_env_softens_verdict(self):
        result = self._run({"ok": 4, "fail": 2}, fail_labels=["shell env", "shell env"])
        assert "the pipeline is healthy, but this shell will not export to it" in result.stdout

    def test_mixed_failures_including_non_shell_env_is_strict(self):
        result = self._run({"ok": 4, "fail": 2}, fail_labels=["shell env", "docker"])
        assert "telemetry is NOT being collected" in result.stdout


# --- round trip + isolated replay fallback, no Docker ---------------------


class _FakeCollectorHandler(BaseHTTPRequestHandler):
    """Replicates just the HTTP contract doctor's port/round-trip checks
    depend on: 400 on a malformed body to /v1/metrics, 404 elsewhere, and on
    a well-formed export request, 200 + the raw body appended to
    raw-metrics.jsonl exactly like the real collector's `file` exporter
    (append: true) does."""

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        if self.path != "/v1/metrics":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return
        with open(self.server.raw_path, "a") as f:
            f.write(body.decode("utf-8") + "\n")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_collector(tmp_path):
    port = _free_port()
    raw_path = tmp_path / "data" / "raw-metrics.jsonl"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.touch()
    server = ThreadingHTTPServer(("127.0.0.1", port), _FakeCollectorHandler)
    server.raw_path = str(raw_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port, raw_path
    server.shutdown()
    thread.join(timeout=5)


def _run_doctor(cld_otel_dir: Path, port: int, extra_env=None, timeout_secs=8):
    env = dict(os.environ)
    env.update(
        {
            "CLD_OTEL_DIR": str(cld_otel_dir),
            "CLD_OTEL_PORT": str(port),
            "PATH": os.environ["PATH"],
        }
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(OTELCTL_SH), "doctor", "--timeout", str(timeout_secs)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


class TestDoctorRoundTrip:
    def test_no_aggregator_falls_back_to_isolated_replay_and_warns(self, fake_collector, tmp_path):
        """Check 5: with the receiver reachable but no aggregate.py --watch
        running, doctor must not fail outright -- it runs an isolated
        temp-dir replay of aggregate.py (never touching the real stats dir)
        to prove the metric *would* aggregate correctly, and reports a warn
        telling the user to start the aggregator, not a fail."""
        port, raw_path = fake_collector
        result = _run_doctor(tmp_path, port)

        assert "[ ok ] port" in result.stdout
        round_trip_lines = [l for l in result.stdout.splitlines() if "round trip" in l]
        assert any(l.startswith("[warn]") for l in round_trip_lines), result.stdout
        assert any("no aggregator running" in l for l in result.stdout.splitlines())

        # The real stats dir must never have been touched by a one-shot pass.
        assert not (tmp_path / "stats").exists() or not list((tmp_path / "stats").rglob("*.json"))

    def test_live_aggregator_gets_a_real_green_round_trip(self, fake_collector, tmp_path):
        """With a real aggregate.py --watch process running against the same
        CLD_OTEL_DIR, the synthetic metric must land in and then be cleaned
        from a real stats file -- the happy path, not the replay fallback."""
        port, raw_path = fake_collector
        agg_proc = subprocess.Popen(
            ["python3", str(AGGREGATE_PY), "--watch"],
            env={**os.environ, "CLD_OTEL_DIR": str(tmp_path)},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            time.sleep(0.5)
            result = _run_doctor(tmp_path, port)
        finally:
            agg_proc.terminate()
            try:
                agg_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                agg_proc.kill()

        round_trip_lines = [l for l in result.stdout.splitlines() if "round trip" in l]
        assert any(l.startswith("[ ok ]") for l in round_trip_lines), result.stdout
        assert any("stats file" in l for l in round_trip_lines)
        # Cleaned up afterwards -- no leftover synthetic stats file.
        assert not (tmp_path / "stats").exists() or not list((tmp_path / "stats").rglob("*doctorcheck*"))
