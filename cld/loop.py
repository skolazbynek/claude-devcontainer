"""Automated implement-review loop."""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from string import Template

from cld.agent import launch_agent
from cld.docker import (
    build_session_name,
    find_jj_root,
    log_error,
    log_info,
    log_warn,
)

_POLL_INTERVAL = 30
_AGENT_TIMEOUT = 1800


# --- jj helpers ---


def _jj_run(args: list[str], jj_root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["jj"] + args, capture_output=True, text=True, cwd=str(jj_root),
    )


def _jj_file_show(revset: str, filepath: str, jj_root: Path) -> str | None:
    result = _jj_run(["file", "show", "-r", revset, filepath], jj_root)
    if result.returncode != 0:
        return None
    return result.stdout


def _jj_resolve(revset: str, jj_root: Path) -> str:
    """Resolve a revset to a concrete commit ID."""
    result = _jj_run(
        ["log", "-r", revset, "--no-graph", "-T", "commit_id", "-l", "1"], jj_root,
    )
    if result.returncode != 0:
        log_error(f"Failed to resolve revision '{revset}': {result.stderr.strip()}")
        sys.exit(1)
    return result.stdout.strip()


# --- Agent polling ---


def _wait_for_agent(session_name: str, jj_root: Path) -> dict:
    """Block until agent container exits, then return its summary."""
    start = time.monotonic()
    while time.monotonic() - start < _AGENT_TIMEOUT:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name=^{session_name}$", "--format", "{{.Status}}"],
            capture_output=True, text=True,
        )
        if not result.stdout.strip():
            break
        time.sleep(_POLL_INTERVAL)
    else:
        subprocess.run(["docker", "stop", session_name], capture_output=True, text=True)
        return {"status": "timeout", "session_name": session_name}

    summary_raw = _jj_file_show(
        session_name, f"agent-output-{session_name}/summary.json", jj_root,
    )
    if not summary_raw:
        return {"status": "unknown", "error": "No summary.json found"}
    try:
        return json.loads(summary_raw)
    except json.JSONDecodeError:
        return {"status": "unknown", "error": "Invalid summary.json"}


# --- Review severity parsing ---


def _parse_review_severity(content: str) -> dict:
    counts = {"critical": 0, "major": 0, "minor": 0}
    current = None
    for line in content.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("## critical"):
            current = "critical"
        elif stripped.startswith("## major"):
            current = "major"
        elif stripped.startswith("## minor"):
            current = "minor"
        elif stripped.startswith("## "):
            current = None
        elif stripped.startswith("### ") and current:
            counts[current] += 1
    return counts


# --- Change annotation ---


def _get_change_description(revset: str, jj_root: Path) -> str:
    result = _jj_run(["log", "-r", revset, "--no-graph", "-T", "description"], jj_root)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _describe_impl_change(
    session_name: str, iteration: int, task_file: Path,
    review_content: str | None, jj_root: Path,
) -> None:
    original_msg = _get_change_description(session_name, jj_root)

    parts = [f"[loop impl {iteration}] {original_msg}"]

    if iteration == 1:
        first_line = task_file.read_text().strip().splitlines()[0]
        parts.append(f"\nTask: {first_line}")
    elif review_content:
        severity = _parse_review_severity(review_content)
        parts.append(
            f"\nAddressing iteration {iteration - 1} review: "
            f"{severity['critical']} critical, {severity['major']} major"
        )

    _jj_run(["describe", "-r", session_name, "-m", "\n".join(parts)], jj_root)


def _describe_review_change(
    session_name: str, iteration: int, severity: dict, jj_root: Path,
) -> None:
    is_clean = severity["critical"] == 0 and severity["major"] == 0
    status = "clean" if is_clean else "needs fixes"

    msg = (
        f"[loop review {iteration}] "
        f"{severity['critical']} critical, {severity['major']} major, "
        f"{severity['minor']} minor -- {status}"
    )

    _jj_run(["describe", "-r", session_name, "-m", msg], jj_root)


def _delete_bookmark(bookmark: str, jj_root: Path) -> None:
    _jj_run(["bookmark", "delete", bookmark], jj_root)


# --- Prompt composition ---


def _compose_iter_prompt(
    task_file: Path, review_content: str | None, iteration: int, jj_root: Path,
) -> Path:
    if iteration == 1 or not review_content:
        return task_file

    combined = (
        f"{task_file.read_text()}\n\n"
        f"# Review Findings (Iteration {iteration - 1})\n\n"
        f"The following issues were found in the previous implementation. "
        f"Address all Critical and Major findings. Minor findings are optional.\n\n"
        f"{review_content}\n"
    )
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix=f".cld-loop-impl-iter{iteration}-",
        delete=False, dir=jj_root,
    )
    tmp.write(combined)
    tmp.close()
    return Path(tmp.name)


def _compose_review_prompt(
    start_commit: str, loop_bookmark: str, iteration: int, jj_root: Path,
) -> Path:
    diff_result = subprocess.run(
        ["jj", "diff", "--from", start_commit, "--to", loop_bookmark, "--git"],
        capture_output=True, text=True, cwd=str(jj_root),
    )
    if diff_result.returncode != 0:
        log_error(f"Failed to generate diff: {diff_result.stderr.strip()}")
        sys.exit(1)
    if not diff_result.stdout.strip():
        log_error("Generated diff is empty -- nothing to review")
        sys.exit(1)

    diff_file = jj_root / f".cld-loop-diff-iter{iteration}.patch"
    diff_file.write_text(diff_result.stdout)

    cld_root = Path(__file__).resolve().parent.parent
    template_path = cld_root / "prompts/loop-review.md"
    template = Template(template_path.read_text())

    content = template.safe_substitute(
        DIFF_FILE_PATH=f"/workspace/origin/{diff_file.name}",
        OUTPUT_FILE=f"CODE_REVIEW_iter{iteration}.md",
        ITERATION=str(iteration),
    )

    task = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix=f".cld-loop-review-iter{iteration}-",
        delete=False, dir=jj_root,
    )
    task.write(content)
    task.close()
    return Path(task.name)


# --- Output formatting ---


def _format_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


def _print_phase(iteration: int, max_iter: int, phase: str, session: str) -> None:
    log_info(f"[{iteration}/{max_iter}] {phase} ({session})")


def _print_iteration_result(iteration: int, max_iter: int, severity: dict) -> None:
    parts = []
    for level in ("critical", "major", "minor"):
        count = severity[level]
        if count:
            parts.append(f"{count} {level}")
    summary = ", ".join(parts) if parts else "no findings"
    is_clean = severity["critical"] == 0 and severity["major"] == 0
    action = "clean, stopping" if is_clean else "continuing"
    log_info(f"[{iteration}/{max_iter}] result: {summary} -> {action}")


def _print_exit_report(
    loop_bookmark: str, iteration: int, max_iter: int, reason: str,
) -> None:
    print()
    print("=" * 48)
    print(f"Loop completed: {iteration}/{max_iter} iterations ({reason})")
    print("=" * 48)
    print()
    print(f"Bookmark:  {loop_bookmark}")
    print(f"History:   jj log -r '{loop_bookmark}::@'")
    print(f"Diff:      jj diff -r '{loop_bookmark}'")
    if iteration > 0:
        print(f"Review:    jj file show -r '{loop_bookmark}' CODE_REVIEW_iter{iteration}.md")
    print(f"Merge:     jj squash --from '{loop_bookmark}'")
    print()


# --- Interactive approval ---


def _prompt_user(severity: dict, review_content: str) -> str:
    print()
    print(f"  Critical: {severity['critical']}  Major: {severity['major']}  Minor: {severity['minor']}")
    print()
    while True:
        choice = input("  [c]ontinue  [s]top  [v]iew full review: ").strip().lower()
        if choice in ("c", "continue"):
            return "continue"
        if choice in ("s", "stop"):
            return "stop"
        if choice in ("v", "view"):
            print()
            print(review_content)
            print()


# --- Cleanup ---


def _cleanup_temp_files(jj_root: Path) -> None:
    for pattern in (".cld-loop-impl-*", ".cld-loop-review-*", ".cld-loop-diff-*"):
        for f in jj_root.glob(pattern):
            f.unlink(missing_ok=True)


# --- Main loop ---


def run_loop(
    task_file: Path,
    *,
    name: str = "",
    model: str = "",
    review_model: str = "",
    revision: str = "",
    max_iterations: int = 3,
    approve: bool = False,
) -> None:
    jj_root = find_jj_root()
    loop_bookmark = build_session_name("loop", name)
    start_commit = _jj_resolve(revision or "@", jj_root)

    result = _jj_run(["bookmark", "create", loop_bookmark, "-r", start_commit], jj_root)
    if result.returncode != 0:
        log_error(f"Failed to create bookmark: {result.stderr.strip()}")
        sys.exit(1)

    log_info(f"Loop '{loop_bookmark}' started at {start_commit[:12]}")

    review_content: str | None = None
    final_reason = "max iterations reached"
    final_iteration = 0

    try:
        for iteration in range(1, max_iterations + 1):
            final_iteration = iteration

            # --- IMPLEMENT ---
            impl_task = _compose_iter_prompt(task_file, review_content, iteration, jj_root)
            impl_session = f"{loop_bookmark}_impl{iteration}"

            _print_phase(iteration, max_iterations, "implementing...", impl_session)
            phase_start = time.monotonic()

            impl_result = launch_agent(
                task_file=impl_task,
                model=model,
                revision=loop_bookmark,
                session_name=impl_session,
                quiet=True,
            )

            impl_summary = _wait_for_agent(impl_result["session_name"], jj_root)
            duration = time.monotonic() - phase_start
            log_info(f"[{iteration}/{max_iterations}] implementing... done ({_format_duration(duration)})")

            impl_status = impl_summary.get("status", "unknown")
            if impl_status != "success":
                log_error(f"Implementer {impl_status}: {impl_summary.get('error', '')}")
                final_reason = f"implementer {impl_status} (iteration {iteration})"
                break

            _describe_impl_change(impl_session, iteration, task_file, review_content, jj_root)
            _jj_run(["bookmark", "set", loop_bookmark, "-r", impl_session], jj_root)
            _delete_bookmark(impl_session, jj_root)

            # --- REVIEW ---
            review_task = _compose_review_prompt(start_commit, loop_bookmark, iteration, jj_root)
            review_session = f"{loop_bookmark}_review{iteration}"

            _print_phase(iteration, max_iterations, "reviewing...", review_session)
            phase_start = time.monotonic()

            review_result = launch_agent(
                task_file=review_task,
                model=review_model,
                revision=loop_bookmark,
                session_name=review_session,
                quiet=True,
            )

            review_summary = _wait_for_agent(review_result["session_name"], jj_root)
            duration = time.monotonic() - phase_start
            log_info(f"[{iteration}/{max_iterations}] reviewing... done ({_format_duration(duration)})")

            # --- EVALUATE ---
            review_content = _jj_file_show(
                review_session, f"CODE_REVIEW_iter{iteration}.md", jj_root,
            )

            if not review_content:
                log_warn("Reviewer produced no review file")
                if review_summary.get("status") == "success":
                    _jj_run(["bookmark", "set", loop_bookmark, "-r", review_session], jj_root)
                _delete_bookmark(review_session, jj_root)
                final_reason = f"no review output (iteration {iteration})"
                break

            severity = _parse_review_severity(review_content)

            _describe_review_change(review_session, iteration, severity, jj_root)
            if review_summary.get("status") == "success":
                _jj_run(["bookmark", "set", loop_bookmark, "-r", review_session], jj_root)
            _delete_bookmark(review_session, jj_root)

            _print_iteration_result(iteration, max_iterations, severity)

            if approve:
                action = _prompt_user(severity, review_content)
                if action == "stop":
                    final_reason = "user stopped"
                    break

            if severity["critical"] == 0 and severity["major"] == 0:
                final_reason = "clean review"
                break

    except KeyboardInterrupt:
        print()
        log_warn("Interrupted")
        final_reason = "interrupted"

    _print_exit_report(loop_bookmark, final_iteration, max_iterations, final_reason)
    _cleanup_temp_files(jj_root)
