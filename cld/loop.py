"""Automated implement-review loop."""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from string import Template

from cld.agent import agent_workspace_path, launch_agent
from cld.agent_runtime import format_duration, read_agent_cost, wait_for_agent
from cld.config import Config
from cld.docker import build_session_name
from cld.log import get_logger
from cld.vcs import VcsBackend, get_backend
from cld.vcs.anchor import assert_descendant, create_editable_root, resolve_anchor

log = get_logger(__name__)


# --- Review severity parsing ---


def _parse_review_severity(content: str) -> dict:
    """Parse a markdown review file and count findings by severity level.

    Looks for ``## critical``, ``## major``, ``## minor`` headers and counts
    ``### `` sub-entries under each.
    """
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


def _describe_impl_change(
    session_name: str, iteration: int,
    task_file: Path | None, inline_prompt: str | None,
    review_content: str | None, vcs: VcsBackend,
) -> None:
    """Annotate an implementation change with loop metadata.

    Prepends ``[loop impl N]`` to the commit message and appends context
    about the task (iteration 1) or addressed review findings (iteration 2+).
    """
    original_msg = vcs.get_description(session_name)

    parts = [f"[loop impl {iteration}] {original_msg}"]

    if iteration == 1:
        task_text = _load_task_text(task_file, inline_prompt)
        first_line = task_text.strip().splitlines()[0] if task_text.strip() else ""
        parts.append(f"\nTask: {first_line}")
    elif review_content:
        severity = _parse_review_severity(review_content)
        parts.append(
            f"\nAddressing iteration {iteration - 1} review: "
            f"{severity['critical']} critical, {severity['major']} major"
        )

    vcs.describe(session_name, "\n".join(parts))


def _describe_review_change(
    session_name: str, iteration: int, severity: dict, vcs: VcsBackend,
) -> None:
    """Annotate a review change with severity counts and pass/fail status."""
    is_clean = severity["critical"] == 0 and severity["major"] == 0
    status = "clean" if is_clean else "needs fixes"

    msg = (
        f"[loop review {iteration}] "
        f"{severity['critical']} critical, {severity['major']} major, "
        f"{severity['minor']} minor -- {status}"
    )

    vcs.describe(session_name, msg)


# --- Prompt composition ---


def _compose_iter_prompt(
    task_file: Path | None, inline_prompt: str | None,
    review_content: str | None, iteration: int, scratch_dir: Path,
) -> tuple[Path | None, str | None]:
    """Build the task prompt inputs for an implementation iteration.

    First iteration: forwards the user's inputs unchanged so the agent entrypoint
    combines them the same way ``cld agent`` does. Subsequent iterations:
    combines the original task with previous review findings into a staged file
    under the loop workspace's ``.cld-run/``.
    """
    if iteration == 1 or not review_content:
        return task_file, inline_prompt

    task_text = _load_task_text(task_file, inline_prompt)
    combined = (
        f"{task_text}\n\n"
        f"# Review Findings (Iteration {iteration - 1})\n\n"
        f"The following issues were found in the previous implementation. "
        f"Address all Critical and Major findings. Minor findings are optional.\n\n"
        f"{review_content}\n"
    )
    staged = scratch_dir / f"loop-impl-iter{iteration}.md"
    staged.write_text(combined)
    return staged, None


def _load_task_text(task_file: Path | None, inline_prompt: str | None) -> str:
    """Read task_file + inline_prompt back into a single string for host-side use."""
    if task_file and inline_prompt:
        return f"{task_file.read_text()}\n\n## Additional Instructions\n\n{inline_prompt}\n"
    if task_file:
        return task_file.read_text()
    return inline_prompt or ""


def _compose_review_prompt(
    start_commit: str, loop_branch: str, iteration: int,
    vcs: VcsBackend, scratch_dir: Path,
) -> Path:
    """Build the task prompt for a review iteration.

    Generates a diff from *start_commit* to the current loop branch tip,
    saves it as a patch file in the loop workspace's ``.cld-run/``, and
    fills in the review template.
    """
    diff_content = vcs.diff_between(start_commit, loop_branch)
    if diff_content.startswith("Error:"):
        log.error(f"Failed to generate diff: {diff_content}")
        sys.exit(1)
    if not diff_content.strip():
        log.error("Generated diff is empty -- nothing to review")
        sys.exit(1)

    diff_file = scratch_dir / f"loop-diff-iter{iteration}.patch"
    diff_file.write_text(diff_content)

    template_path = Path(__file__).resolve().parent / "prompts/loop-review.md"
    template = Template(template_path.read_text())

    content = template.safe_substitute(
        DIFF_FILE_PATH=f"/workspace/current/.cld-run/{diff_file.name}",
        OUTPUT_FILE=f"CODE_REVIEW_iter{iteration}.md",
        ITERATION=str(iteration),
    )

    task_file = scratch_dir / f"loop-review-iter{iteration}.md"
    task_file.write_text(content)
    return task_file


# --- Output formatting ---


def _print_phase(iteration: int, max_iter: int, phase: str, session: str) -> None:
    log.info(f"[{iteration}/{max_iter}] {phase} ({session})")


def _print_iteration_result(iteration: int, max_iter: int, severity: dict) -> None:
    """Log a one-line summary of review findings and the resulting action."""
    parts = []
    for level in ("critical", "major", "minor"):
        count = severity[level]
        if count:
            parts.append(f"{count} {level}")
    summary = ", ".join(parts) if parts else "no findings"
    is_clean = severity["critical"] == 0 and severity["major"] == 0
    action = "clean, stopping" if is_clean else "continuing"
    log.info(f"[{iteration}/{max_iter}] result: {summary} -> {action}")


def _print_exit_report(
    loop_branch: str, iteration: int, max_iter: int, reason: str,
    vcs: VcsBackend, anchor: str, total_cost_usd: float = 0.0,
) -> None:
    """Print the final summary with VCS-appropriate commands for the user."""
    vcs_name = vcs.name
    print()
    print("=" * 48)
    print(f"Loop completed: {iteration}/{max_iter} iterations ({reason})")
    print("=" * 48)
    print()
    print(f"Anchor:    {anchor[:12]}")
    print(f"Branch:    {loop_branch}")
    if vcs_name == "jj":
        print(f"History:   jj log -r '{anchor}..{loop_branch}'")
        print(f"Diff:      jj diff --from {anchor} --to {loop_branch}")
        if iteration > 0:
            print(f"Review:    jj file show -r '{loop_branch}' CODE_REVIEW_iter{iteration}.md")
        print(f"Merge:     jj squash --from '{loop_branch}'")
    else:
        print(f"History:   git log {anchor}..{loop_branch}")
        print(f"Diff:      git diff {anchor}..{loop_branch}")
        if iteration > 0:
            print(f"Review:    git show {loop_branch}:CODE_REVIEW_iter{iteration}.md")
        print(f"Merge:     git merge {loop_branch}")
    if total_cost_usd > 0:
        n = iteration if iteration > 0 else max_iter
        label = "iteration" if n == 1 else "iterations"
        print()
        print(f"Total cost: ${total_cost_usd:.4f} over {n} {label}")
    print()


# --- Interactive approval ---


def _prompt_user(severity: dict, review_content: str) -> tuple[str, str]:
    """Prompt the user to continue, stop, view review findings, or edit them before feeding back."""
    print()
    print(f"  Critical: {severity['critical']}  Major: {severity['major']}  Minor: {severity['minor']}")
    print()
    while True:
        choice = input("  [c]ontinue / [s]top / [v]iew findings / [e]dit findings / [q]uit: ").strip().lower()
        if choice in ("c", "continue"):
            return "continue", review_content
        if choice in ("s", "stop", "q", "quit"):
            return "stop", review_content
        if choice in ("v", "view"):
            print()
            print(review_content)
            print()
        if choice in ("e", "edit"):
            with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as tf:
                tf.write(review_content)
                tf_path = tf.name
            editor = os.environ.get("EDITOR", "vi")
            try:
                subprocess.run([editor, tf_path])
            except FileNotFoundError:
                Path(tf_path).unlink(missing_ok=True)
                print(f"  Editor not found: {editor}")
                print()
                continue
            tf = Path(tf_path)
            review_content = tf.read_text()
            tf.unlink(missing_ok=True)
            print("  Findings updated.")
            print()


# --- Cleanup ---


# --- Main loop ---


def run_loop(
    cfg: Config,
    task_file: Path | None,
    *,
    inline_prompt: str | None = None,
    name: str = "",
    model: str = "",
    review_model: str = "",
    revision: str = "",
    max_iterations: int = 3,
    approve: bool = False,
) -> None:
    """Run the automated implement-review loop.

    Each iteration launches an implementation agent, waits for it, then launches
    a review agent. If the review is clean (no critical/major findings), the loop
    stops. Otherwise, review feedback is fed into the next implementation iteration.
    A VCS branch tracks the accumulated changes across iterations.
    """
    vcs = get_backend()
    repo_root = vcs.repo_root
    loop_branch = build_session_name("loop", name)
    anchor = resolve_anchor(vcs, revision)
    loop_ws = agent_workspace_path(repo_root, loop_branch)
    create_editable_root(vcs, anchor, loop_ws, loop_branch)
    loop_vcs = type(vcs)(repo_root=repo_root, workspace_path=loop_ws)
    scratch_dir = loop_ws / ".cld-run"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    start_commit = anchor

    log.info(f"Loop '{loop_branch}' started at anchor {anchor[:12]}")

    review_content: str | None = None
    final_reason = "max iterations reached"
    final_iteration = 0
    total_cost_usd = 0.0
    current_cid: str | None = None

    try:
        for iteration in range(1, max_iterations + 1):
            final_iteration = iteration

            # --- IMPLEMENT ---
            impl_task_file, impl_inline = _compose_iter_prompt(
                task_file, inline_prompt, review_content, iteration, scratch_dir,
            )
            if impl_task_file is not None and iteration > 1:
                # Commit the composed input on loop_branch so it appears in the
                # per-iteration agent's workspace at /workspace/current/.cld-run/.
                loop_vcs.commit(f"[loop impl {iteration}] inputs")
                loop_vcs.set_branch(loop_branch, "@-" if vcs.name == "jj" else "HEAD")
            impl_session = f"{loop_branch}_impl{iteration}"

            _print_phase(iteration, max_iterations, "implementing...", impl_session)
            phase_start = time.monotonic()

            impl_result = launch_agent(
                cfg,
                task_file=impl_task_file,
                inline_prompt=impl_inline,
                model=model,
                revision=loop_branch,
                session_name=impl_session,
                quiet=True,
            )
            current_cid = impl_result["container_id"]

            impl_summary = wait_for_agent(impl_result["session_name"], vcs, cfg)
            impl_cost = read_agent_cost(impl_result["session_name"], vcs)
            if impl_cost is not None:
                total_cost_usd += impl_cost
            duration = time.monotonic() - phase_start
            log.info(f"[{iteration}/{max_iterations}] implementing... done ({format_duration(duration)})")

            impl_status = impl_summary.get("status", "unknown")
            if impl_status == "no_changes":
                log_info(f"[{iteration}/{max_iterations}] No changes made — task already complete")
                final_reason = "no changes (task already complete)"
                break
            if impl_status != "success":
                log.error(f"Implementer {impl_status}: {impl_summary.get('error', '')}")
                final_reason = f"implementer {impl_status} (iteration {iteration})"
                if iteration == 1:
                    vcs.delete_branch(loop_branch)
                break

            _describe_impl_change(impl_session, iteration, task_file, inline_prompt, review_content, vcs)
            assert_descendant(vcs, anchor, impl_session)
            vcs.set_branch(loop_branch, impl_session)
            vcs.delete_branch(impl_session)

            # --- REVIEW ---
            review_task = _compose_review_prompt(start_commit, loop_branch, iteration, vcs, scratch_dir)
            # Commit the review scratch (diff + task) on loop_branch so it appears
            # in the review agent's workspace at /workspace/current/.cld-run/.
            loop_vcs.commit(f"[loop review {iteration}] inputs")
            loop_vcs.set_branch(loop_branch, "@-" if vcs.name == "jj" else "HEAD")
            review_session = f"{loop_branch}_review{iteration}"

            _print_phase(iteration, max_iterations, "reviewing...", review_session)
            phase_start = time.monotonic()

            review_result = launch_agent(
                cfg,
                task_file=review_task,
                model=review_model,
                revision=loop_branch,
                session_name=review_session,
                quiet=True,
            )
            current_cid = review_result["container_id"]

            review_summary = wait_for_agent(review_result["session_name"], vcs, cfg)
            review_cost = read_agent_cost(review_result["session_name"], vcs)
            if review_cost is not None:
                total_cost_usd += review_cost
            duration = time.monotonic() - phase_start
            log.info(f"[{iteration}/{max_iterations}] reviewing... done ({format_duration(duration)})")

            # --- EVALUATE ---
            review_content = vcs.file_show(
                review_session, f"CODE_REVIEW_iter{iteration}.md",
            )

            if not review_content:
                log.warning("Reviewer produced no review file")
                if review_summary.get("status") == "success":
                    vcs.set_branch(loop_branch, review_session)
                vcs.delete_branch(review_session)
                final_reason = f"no review output (iteration {iteration})"
                break

            severity = _parse_review_severity(review_content)

            _describe_review_change(review_session, iteration, severity, vcs)
            if review_summary.get("status") == "success":
                assert_descendant(vcs, anchor, review_session)
                vcs.set_branch(loop_branch, review_session)
            vcs.delete_branch(review_session)

            _print_iteration_result(iteration, max_iterations, severity)

            is_clean = severity["critical"] == 0 and severity["major"] == 0
            if approve and not is_clean and iteration < max_iterations:
                action, review_content = _prompt_user(severity, review_content)
                if action == "stop":
                    final_reason = "user stopped"
                    break

            if is_clean:
                final_reason = "clean review"
                break

    except KeyboardInterrupt:
        print()
        log.warning("Interrupted")
        final_reason = "interrupted"
        if current_cid:
            log_warn(f"Stopping container {current_cid[:12]}...")
            subprocess.run(["docker", "stop", "--time=10", current_cid], capture_output=True)

    _print_exit_report(loop_branch, final_iteration, max_iterations, final_reason, vcs, anchor, total_cost_usd)
