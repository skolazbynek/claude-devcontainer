# Test Fix Implementation

## Item 1: Trunk auto-detection uses substring match

**Decision:** Implement.

**Fix:** `cld/cli.py:155-163` — Split `list_branches()` output into individual lines, strip whitespace and leading `* `, take the portion before `:` (for jj `name: hash` format) and before any space, then do exact set membership instead of `candidate in branches` substring search.

**Test:** `tests/test_cli.py` — passed.

---

## Item 2: `ensure_image` `parent_image` parameter is dead code

**Decision:** Implement.

**Fix:** Moved `DEVCONTAINER_IMAGE` constant from `cld/cli.py` to `cld/docker.py` (where image management lives). Updated `cld/cli.py` to import it from there. Updated `cld/agent.py` to import `DEVCONTAINER_IMAGE` and pass it as `parent_image` to `ensure_image()` so the devcontainer is auto-built before the agent image when running `cld agent`.

**Test:** `tests/test_agent.py` / `tests/test_docker.py` — passed.

---

## Item 3: Editor subprocess unhandled in `_prompt_user`

**Decision:** Implement.

**Fix:** `cld/loop.py:288-298` — Wrapped `subprocess.run([editor, tf_path])` in `try/except FileNotFoundError`. On error, the temp file is cleaned up, a readable message is printed, and the loop `continue`s so the user can try a different option.

**Test:** `tests/test_loop.py` — passed.

---

## Item 4: `review-diff` and `review-task` temp files accumulate without cleanup

**Decision:** Implement.

**Fix:** `cld/loop.py:309` — Added `"review-diff-*"` and `"review-task-*"` to the glob patterns in `_cleanup_temp_files` so files created by `launch_review` are removed alongside loop-impl/review/diff files.

**Test:** `tests/test_loop.py` passed. `tests/test_review_e2e.py::TestReviewLaunchIntegration::test_launch_review_creates_diff_and_task` failed with a pre-existing assertion bug: the test searches for `review-diff-*.patch` in `vcs.repo_root` but the files are placed in `vcs.repo_root / ".cld"`. This failure is unrelated to the cleanup fix.

---

## Item 5: `git squash` leaves HEAD on `into_rev`

**Decision:** Implement.

**Fix:** `cld/vcs/git.py:159-165` — Captured the original branch name via `git rev-parse --abbrev-ref HEAD` before checkout. After the amend commit, restored HEAD to the original branch if it was not a detached HEAD state.

**Test:** `tests/test_vcs_integration.py` — passed.

---

## Item 6: `check_status` commit hash populated despite failed status

**Decision:** Implement.

**Fix:** `cld/mcp/orchestrator.py:202` — Added `info.pop("commit", None)` when overriding status to `"failed"` due to missing `summary.json`, so callers no longer see a contradictory `commit` field alongside a failed status.

**Test:** `tests/test_orchestrator.py` — passed.
