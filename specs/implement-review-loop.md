# Spec: Automated Implement-Review Loop

## Overview

A new `cld loop` command that automates the implement-review cycle using independent Docker agents. Given a task, it launches an implementer agent, then a reviewer agent, and iterates until the review is clean or a maximum iteration count is reached -- without human intervention.

---

## Use Cases

### UC1: Hands-off task implementation

A developer provides a task prompt (file or inline) and walks away. The loop implements the task, reviews its own output, and iterates on review findings until the result meets quality thresholds. The developer returns to a single jj bookmark containing the final implementation plus a trail of review artifacts.

### UC2: Supervised iteration with approval gates

For high-stakes or unfamiliar tasks, the developer runs the loop in interactive mode (`--approve`). After each review, the loop pauses and displays findings. The developer chooses to continue, stop, or adjust the prompt before the next iteration.

### UC3: Background/detached execution

The developer launches the loop detached (`--detach`) and continues other work. The loop commits a `LOOP_SUMMARY.md` to the bookmark on completion. The developer checks back with `cld loop status <name>`.

---

## Behaviour

### Loop lifecycle

```
                    ┌─────────────────────────────────────────┐
                    │                                         │
   task prompt ──> IMPLEMENT ──> REVIEW ──> EVALUATE ──┬──> DONE
                    ^                                  │
                    │                                  │
                    └──── feedback (if not clean) ─────┘
```

1. **Init**: Create `loop_<name>` bookmark at the starting revision.
2. **Implement**: Launch implementer agent with the original task prompt (and review feedback from prior iteration, if any). Agent commits to its own `agent_<id>` bookmark branching from `loop_<name>`.
3. **Advance bookmark**: On implementer completion, move `loop_<name>` forward to the implementer's output revision.
4. **Review**: Launch reviewer agent against the current `loop_<name>` revision. Reviewer writes `CODE_REVIEW_iter<N>.md` to the repo root and commits it.
5. **Evaluate**: Parse reviewer output for severity findings. Apply termination rules.
6. **Iterate or stop**: If termination criteria met, finalize. Otherwise, compose the next iteration's prompt and go to step 2.

### Implementer agent

- Receives: original task prompt + (from iteration 2 onward) review findings from previous iteration as additional context.
- Starts from: `--revision loop_<name>` (the current tip of the loop bookmark).
- Uses the standard `cld agent` pipeline: isolated jj workspace, commits results, writes `summary.json`.

### Reviewer agent

- Receives: the `code-review.md` prompt template, parameterized with the revision range from the loop's starting point to current `loop_<name>` tip (cumulative diff of the full implementation).
- Reviews only code changes, not review artifacts.
- Output: `CODE_REVIEW_iter<N>.md` committed to the bookmark.

### Termination rules

The loop stops when **any** of the following are true:

1. **Clean review**: The reviewer's output contains no Critical and no Major findings.
2. **Max iterations reached**: Hardcoded limit (default: 3, configurable via `--max-iterations`).
3. **Agent failure**: An agent crashes or produces no changes. The bookmark remains at the last successful state.

Severity is determined by parsing the reviewer's markdown output for `## Critical` and `## Major` section headers with non-empty content beneath them.

### Bookmark management

A single `loop_<name>` bookmark is the user-facing artifact. It advances forward with each successful implementer iteration:

```
(start) ── agent_iter1 ── review_iter1 ── agent_iter2 ── review_iter2
                                                              ^
                                                        loop_<name>
```

Individual `agent_*` and `review_*` bookmarks are implementation details. They are cleaned up after the loop completes unless `--keep-bookmarks` is passed.

### Review history

Each iteration's review is committed as `CODE_REVIEW_iter<N>.md` (not overwriting previous iterations). This preserves the full review arc and lets the developer trace what was found and fixed across iterations.

### Prompt composition for iteration 2+

The implementer prompt for subsequent iterations is composed of:

```markdown
# Original Task

<contents of the user's task file>

# Review Findings (Iteration <N-1>)

The following issues were found in the previous implementation. Address all Critical
and Major findings. Minor findings are optional.

<contents of CODE_REVIEW_iter<N-1>.md>
```

---

## UX / Developer Experience

### Progress output

Compact, real-time status to stdout:

```
[1/3] implementing... (agent_k82f1)  3m12s
[1/3] reviewing...    (review_k82f1) 2m45s
[1/3] result: 1 critical, 2 major, 1 minor -> continuing
[2/3] implementing... (agent_r91a3)  4m01s
[2/3] reviewing...    (review_r91a3) 2m30s
[2/3] result: 0 critical, 0 major, 2 minor -> clean, stopping
```

### Exit report

On completion, print:

```
Loop completed: 2/3 iterations (clean review)
Bookmark: loop_feature-x
Inspect:  jj diff -r 'loop_feature-x'
Reviews:  jj file show -r 'loop_feature-x' CODE_REVIEW_iter2.md
Merge:    jj squash --from 'loop_feature-x'
```

Includes: why it stopped (clean / max iterations / failure), where the result is, and actionable next commands.

### Interactive mode (`--approve`)

After each review phase, pause and display:

```
Review findings (iteration 1):
  Critical: 1  Major: 2  Minor: 1

  [c]ontinue  [s]top  [e]dit prompt  [v]iew full review
```

### Detached mode (`--detach`)

Runs the loop in the background. Commits `LOOP_SUMMARY.md` to the bookmark on completion. Check status via `cld loop status <name>`.

### Failure resilience

If an agent crashes mid-loop, the `loop_<name>` bookmark still points at the last successful implementer output. The exit report indicates the failure and what was preserved.

---

## CLI Interface

```
cld loop [OPTIONS] [TASK_FILE]
```

| Option | Default | Description |
|---|---|---|
| `TASK_FILE` | required | Path to task prompt markdown file |
| `-p, --prompt` | | Inline prompt (alternative to task file) |
| `-n, --name` | auto | Loop session name suffix |
| `-m, --model` | sonnet | Model for implementer agent |
| `--review-model` | sonnet | Model for reviewer agent |
| `-r, --revision` | `@` | Starting jj revision |
| `--max-iterations` | 3 | Hard iteration cap |
| `--approve` | false | Pause after each review for manual approval |
| `--detach` | false | Run loop in background |
| `--keep-bookmarks` | false | Keep individual agent/review bookmarks after completion |

---

## Technical Implementation

### New components

| File | Purpose |
|---|---|
| `cld/loop.py` | Loop orchestration logic: lifecycle, polling, prompt composition, termination evaluation, bookmark management |
| `cld/cli.py` | New `loop` subcommand definition |

### Extracted/refactored from existing code

| Component | Source | Change |
|---|---|---|
| `wait_for_agent(session_name, timeout, poll_interval) -> summary` | `cld/mcp/orchestrator.py` `check_status` logic | Extract the docker-ps + jj-bookmark-fallback polling into a reusable library function in `cld/agent.py` or `cld/docker.py` |
| `parse_review_severity(content: str) -> dict` | New | Parse `CODE_REVIEW_iter*.md` for Critical/Major/Minor section presence and finding counts |
| Review diff generation | `cld/agent.py` `launch_review()` | Reuse the `jj diff --from ... --to ... --git` logic for generating the reviewer's input diff |

### Reused as-is

| Component | Usage |
|---|---|
| `launch_agent()` | Launches both implementer and reviewer agents |
| `build_session_name()` | Naming with `loop_` prefix |
| `find_jj_root()` | Workspace detection |
| `_jj_file_show()` | Reading committed review output and summaries |
| `ensure_image()` | Docker image availability |
| Agent entrypoint | Standard workspace isolation, commit, summary generation |

### Loop orchestration pseudocode

```python
def run_loop(task_file, name, model, review_model, revision, max_iterations, approve):
    jj_root = find_jj_root()
    loop_bookmark = build_session_name("loop", name)

    # Create loop bookmark at starting revision
    jj("bookmark", "create", loop_bookmark, "-r", revision)

    review_content = None

    for iteration in range(1, max_iterations + 1):
        # --- Implement ---
        impl_prompt = compose_impl_prompt(task_file, review_content, iteration)
        impl_result = launch_agent(task_file=impl_prompt, revision=loop_bookmark)
        impl_summary = wait_for_agent(impl_result["session_name"])

        if impl_summary["status"] != "success":
            report_failure(iteration, impl_summary)
            break

        # Advance loop bookmark
        jj("bookmark", "set", loop_bookmark, "-r", impl_result["session_name"])

        # --- Review ---
        review_prompt = compose_review_prompt(loop_bookmark, revision, iteration)
        review_result = launch_agent(task_file=review_prompt, revision=loop_bookmark)
        review_summary = wait_for_agent(review_result["session_name"])

        # Advance bookmark past review commit
        jj("bookmark", "set", loop_bookmark, "-r", review_result["session_name"])

        # --- Evaluate ---
        review_content = jj_file_show(loop_bookmark, f"CODE_REVIEW_iter{iteration}.md")
        severity = parse_review_severity(review_content)

        print_iteration_result(iteration, max_iterations, severity)

        if approve:
            action = prompt_user(severity)
            if action == "stop":
                break

        if severity["critical"] == 0 and severity["major"] == 0:
            print_exit_report(loop_bookmark, iteration, "clean review")
            break
    else:
        print_exit_report(loop_bookmark, max_iterations, "max iterations reached")

    # Cleanup temporary bookmarks
    cleanup_agent_bookmarks(...)
```

### Polling mechanism

```python
def wait_for_agent(session_name, timeout=1800, poll_interval=30) -> dict:
    """Poll agent status until completion or timeout."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        status = check_agent_status(session_name)
        if status["status"] != "running":
            return status
        time.sleep(poll_interval)
    stop_agent(session_name)
    return {"status": "timeout", "session_name": session_name}
```

### Review severity parsing

```python
def parse_review_severity(content: str) -> dict:
    """Parse CODE_REVIEW markdown for finding counts per severity."""
    counts = {"critical": 0, "major": 0, "minor": 0}
    current_severity = None

    for line in content.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("## critical"):
            current_severity = "critical"
        elif stripped.startswith("## major"):
            current_severity = "major"
        elif stripped.startswith("## minor"):
            current_severity = "minor"
        elif stripped.startswith("## "):
            current_severity = None
        elif stripped.startswith("### ") and current_severity:
            counts[current_severity] += 1

    return counts
```

The parser counts H3 headings (`###`) under each severity H2 section, since each finding is an H3 per the review template's output format.

---

## Open Questions

1. **Reviewer scope**: Currently specified as cumulative diff (start to current tip). Worth adding a `--incremental-review` flag for iteration-only diffs in the future?
2. **Cost visibility**: Should the exit report include token usage estimates? Requires parsing `result.json` from each agent.
3. **Prompt customization**: Should the user be able to supply a custom review prompt instead of the default `code-review.md`? Likely yes, via `--review-prompt <file>`.
4. **Orchestrator MCP integration**: Should `cld loop` be exposed as an orchestrator MCP tool so a team-leader agent can trigger loops? Deferred -- useful but adds complexity.
