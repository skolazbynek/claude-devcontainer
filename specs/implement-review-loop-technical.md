# Technical Implementation: Automated Implement-Review Loop

Reference: [Product Spec](implement-review-loop.md)

---

## Implementation Plan

### Phase 1: Modify `launch_agent` for quiet operation

Add `quiet` parameter to suppress banner output when called from the loop.

### Phase 2: New module `cld/loop.py`

All loop logic in one file. No classes -- free functions matching the existing codebase style. Contains:
- Loop orchestration (`run_loop`)
- Agent polling (`_wait_for_agent`)
- Prompt composition (`_compose_iter_prompt`, `_compose_review_prompt`)
- Review parsing (`_parse_review_severity`)
- jj helpers (`_jj_run`, `_jj_file_show`, `_jj_resolve`)
- Change annotation (`_describe_impl_change`, `_describe_review_change`)
- Output formatting (`_print_phase`, `_print_exit_report`)
- Interactive approval (`_prompt_user`)
- Cleanup (`_cleanup_temp_files`)

### Phase 3: CLI integration

Add `loop` subcommand to `cld/cli.py`.

### Phase 4: Review prompt template

New `prompts/loop-review.md` -- self-contained review prompt for loop iterations. Follows the same review dimensions and severity classification as `code-review.md` but parameterized for loop use (reads a patch file, writes to `CODE_REVIEW_iter<N>.md`).

---

## Phase 1: Modify `launch_agent`

**File:** `cld/agent.py`

Add `quiet` parameter. When true, suppress all `log_info` and `print` calls. Return dict unchanged.

```python
def launch_agent(
    task_file: Path | None = None,
    inline_prompt: str | None = None,
    name: str = "",
    model: str = "",
    revision: str = "",
    session_name: str | None = None,
    quiet: bool = False,
) -> dict:
```

Wrap existing output blocks in `if not quiet:`:

```python
    if not quiet:
        log_info("Starting agent in background...")
        log_info(f"Task: {resolved_task}")
        log_info(f"Repository: {jj_root}")
        print()

    # ... docker run (unchanged) ...

    if not quiet:
        print(f"Container ID: {cid}")
        # ... rest of the banner ...

    return {"container_id": cid, "session_name": session, "jj_root": str(jj_root)}
```

`log_info(f"Session name: ...")` inside `build_container_args` still prints -- that's fine, it's operational context.

---

## Phase 2: `cld/loop.py`

### Module structure

```python
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
```

### jj helpers

Local to loop.py. Same patterns as `orchestrator.py` -- extract when a third consumer appears.

```python
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
```

### Agent polling

```python
_POLL_INTERVAL = 30
_AGENT_TIMEOUT = 1800


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
```

### Change annotation

No child bookmarks survive the loop. Each agent creates a temporary bookmark (the entrypoint requires it), but the loop deletes it immediately after reading output. Instead, `jj describe` annotates each change so `jj log` tells the full story.

```python
def _get_change_description(revset: str, jj_root: Path) -> str:
    """Read the current description of a jj change."""
    result = _jj_run(["log", "-r", revset, "--no-graph", "-T", "description"], jj_root)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _describe_impl_change(
    session_name: str, iteration: int, task_file: Path,
    review_content: str | None, jj_root: Path,
) -> None:
    """Annotate an implementer's committed change with loop context."""
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
    """Annotate a reviewer's committed change with findings summary."""
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
```

### Review severity parsing

Counts `###` findings under `## Critical`, `## Major`, `## Minor` sections. Relies on the review prompt's output format where each finding is an H3 under its severity H2.

```python
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
```

### Prompt composition

**Implementer prompt (iteration 2+):**

Wraps the original task content with review findings appended. Writes to a temp file in `jj_root` (must be host-mountable).

```python
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
```

**Review prompt:**

Generates the cumulative diff, saves as a patch file, renders `prompts/loop-review.md` with template substitution.

```python
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
```

### Output formatting

```python
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
```

### Interactive approval

```python
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
```

### Temp file cleanup

```python
def _cleanup_temp_files(jj_root: Path) -> None:
    for pattern in (".cld-loop-impl-*", ".cld-loop-review-*", ".cld-loop-diff-*"):
        for f in jj_root.glob(pattern):
            f.unlink(missing_ok=True)
```

### Main loop: `run_loop`

```python
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

            # Annotate and clean up implementer's bookmark
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
                # Still clean up the reviewer bookmark
                if review_summary.get("status") == "success":
                    _jj_run(["bookmark", "set", loop_bookmark, "-r", review_session], jj_root)
                _delete_bookmark(review_session, jj_root)
                final_reason = f"no review output (iteration {iteration})"
                break

            severity = _parse_review_severity(review_content)

            # Annotate and clean up reviewer's bookmark
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
```

---

## Phase 3: CLI integration

**File:** `cld/cli.py`

Add import:

```python
from cld.loop import run_loop
```

Add command (follows existing `agent` and `review` patterns):

```python
@app.command()
def loop(
    task_file: Optional[str] = typer.Argument(None, help="Path to task markdown file"),
    name: str = typer.Option("", "-n", "--name", help="Loop session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Model for implementer agent"),
    review_model: str = typer.Option("", "--review-model", help="Model for reviewer agent"),
    revision: str = typer.Option("", "-r", "--revision", help="Starting jj revision"),
    max_iterations: int = typer.Option(3, "--max-iterations", help="Maximum iteration count"),
    prompt: str = typer.Option("", "-p", "--prompt", help="Inline prompt (alternative to task file)"),
    approve: bool = typer.Option(False, "--approve", help="Pause after each review for approval"),
):
    """Run an automated implement-review loop."""
    if not task_file and not prompt:
        typer.echo("Error: Provide a task file, --prompt, or both", err=True)
        raise typer.Exit(1)
    task_path = Path(task_file) if task_file else None
    if task_path and not task_path.is_file():
        typer.echo(f"Error: Task file not found: {task_file}", err=True)
        raise typer.Exit(1)

    if prompt and not task_path:
        jj_root = find_jj_root()
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", prefix=".cld-loop-task-", delete=False, dir=jj_root,
        )
        tmp.write(prompt)
        tmp.close()
        task_path = Path(tmp.name)
    elif prompt and task_path:
        jj_root = find_jj_root()
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", prefix=".cld-loop-task-", delete=False, dir=jj_root,
        )
        tmp.write(task_path.read_text())
        tmp.write(f"\n\n## Additional Instructions\n\n{prompt}\n")
        tmp.close()
        task_path = Path(tmp.name)

    run_loop(
        task_path,
        name=name,
        model=model,
        review_model=review_model,
        revision=revision,
        max_iterations=max_iterations,
        approve=approve,
    )
```

Additional import at the top of `cli.py`:

```python
import tempfile
```

---

## Phase 4: Review prompt template

**File:** `prompts/loop-review.md` (new)

Self-contained review prompt for loop iterations. Same review philosophy and severity classification as `code-review.md` but parameterized for loop use.

Template variables: `${DIFF_FILE_PATH}`, `${OUTPUT_FILE}`, `${ITERATION}`.

```markdown
---
description: Review prompt for implement-review loop iterations
---

# Task

You are a senior engineer performing iteration ${ITERATION} of an automated code review loop.

# Setup

1. Read the diff file at `${DIFF_FILE_PATH}` to obtain the full set of changes.
2. For every changed file, explore its surrounding context: callers, callees, base classes,
   relevant interfaces, and third-party dependencies. Do not review a change in isolation.
3. Ignore any `CODE_REVIEW_iter*.md` files and `agent-output-*` directories -- these are
   loop artifacts, not code changes.

# Review dimensions

Evaluate every change through three lenses, in this order:

## 1. Logic and bugs

- Incorrect conditions, off-by-one errors, wrong operator, inverted boolean logic.
- Unhandled return values, missing `None`/`null` checks, silenced exceptions that hide failures.
- Race conditions, deadlocks, incorrect ordering of operations.
- State mutations with unintended side effects across call boundaries.
- Misuse of APIs or framework contracts (wrong argument types, ignored return semantics).
- Pay special attention to `None`/`null` handling: unchecked nullable returns, missing guards
  before attribute access.

## 2. Security

- Injection vectors: SQL, command, template, path traversal.
- Authentication and authorization gaps: missing permission checks, privilege escalation paths.
- Secrets or credentials exposed in code, logs, or error messages.
- Unsafe deserialization, unvalidated redirects, SSRF.

## 3. Robustness and extensibility

- Tight coupling that makes future changes disproportionately expensive.
- Violation of existing patterns and abstractions already established in the codebase.
- Resource leaks: unclosed handles, missing cleanup in error paths, unbounded growth.
- Fragile assumptions about data shape, ordering, or external system behavior.

# Severity classification

Classify each finding as one of:

- **Critical** -- Will cause data loss, security breach, or system failure under normal
  operation. Must be fixed before merge.
- **Major** -- Likely to cause bugs, degraded behavior, or maintenance burden in realistic
  scenarios. Should be fixed before merge.
- **Minor** -- Marginal improvement. Acceptable to merge as-is, but worth noting.

# Constraints

- Do not review style, formatting, naming, or documentation quality.
- Do not suggest refactors, alternative designs, or improvements beyond the three review
  dimensions.
- Suggest a fix only as general description, don't write specific code.
- If a pattern looks intentional and consistent with the rest of the codebase, do not flag it.
- If you find nothing meaningful, say so. Do not fabricate findings to fill space.

# Output

Write all findings to `${OUTPUT_FILE}` in the repository root.

Structure:

    # Code Review: Iteration ${ITERATION}

    ## Summary

    <Two to three sentences: overall assessment, number of findings by severity.>

    ## Critical

    ### <Short finding title>

    **Dimension:** <Logic | Security | Robustness>
    **Location:** `<file>:<lines>`

    <Concise description. Three to five sentences maximum.>

    **Fix:** <One-line fix description.>

    ## Major

    ### ...

    ## Minor

    ### ...

- Omit empty severity sections.
- Be concise. Less is more.
```

---

## Edge Cases and Error Handling

### Agent produces no changes (`status: no_changes`)

Iteration 1: task may be unclear or already done. Loop stops, bookmark stays at start.

Iteration 2+: implementer couldn't address review findings. Loop stops, bookmark stays at last successful implementation.

### Reviewer fails or produces no review file

Loop stops. Implementation from that iteration is preserved on the bookmark.

### jj bookmark already exists

`jj bookmark create` fails. Loop exits with error. User must delete old bookmark or choose different name. No implicit overwrite.

### Keyboard interrupt

Caught in `run_loop`. Prints exit report with current state so user can inspect the bookmark.

### Temp files

`.cld-loop-impl-*`, `.cld-loop-review-*`, `.cld-loop-diff-*` written to `jj_root`. Cleaned up by `_cleanup_temp_files` at end of loop regardless of exit reason.

### Agent bookmark deletion after failed describe

If `jj describe` fails (unlikely), the bookmark delete still runs. The change exists in history with the agent's original description -- not ideal but not broken.

### Bookmark advancement on reviewer failure

If the reviewer's `status != "success"` but it did produce a `CODE_REVIEW_iter<N>.md`, the loop still reads and evaluates it but does NOT advance the loop bookmark past the review commit. The bookmark stays at the implementer's output.

---

## jj Change History Model

No child bookmarks persist. Only `loop_<name>` survives. The change history is self-documenting via `jj describe`:

```
jj log -r 'loop_feature-x'

@  qrst  loop_feature-x
|  [loop review 2] 0 critical, 0 major, 2 minor -- clean
o  mnop
|  [loop impl 2] Fixed null check in parse_id, addressed race condition
|  Addressing iteration 1 review: 1 critical, 2 major
o  ijkl
|  [loop review 1] 1 critical, 2 major, 1 minor -- needs fixes
o  efgh
|  [loop impl 1] Added user endpoint with validation
|  Task: implement user CRUD endpoint
o  abcd  (start)
```

Each change carries its role (impl/review), iteration number, and contextual details directly in its description.

---

## Build Order

| Step | Files | Depends on |
|---|---|---|
| 1 | `cld/agent.py` | Add `quiet` param to `launch_agent` |
| 2 | `prompts/loop-review.md` | New file, no code deps |
| 3 | `cld/loop.py` | New file, depends on step 1 |
| 4 | `cld/cli.py` | Add `loop` command + imports, depends on step 3 |
| 5 | Manual test | `cld loop prompts/some-task.md --max-iterations 2 --approve` |

---

## Deferred

- **`--detach` mode**: Requires daemonization, state persistence, `cld loop status` subcommand. Ship foreground-only first.
- **Orchestrator MCP integration**: Exposing `run_loop` as an MCP tool for the team-leader agent.
- **`--review-prompt` flag**: Custom review prompt override.
- **Cost/token reporting**: Parse `result.json` for token usage in exit report.
