# Code Review

### Trunk auto-detection uses substring match

Location: `cld/cli.py:168-176`

`get_backend().list_branches()` returns the raw multiline output of `git branch -a` or `jj bookmark list`. The check `if candidate in branches` is a substring search over the entire string, not a per-line exact match. A branch named `agent_maintain_auth` would cause `"main"` to match and `trunk_branch` would be set to `"main"` — a branch that doesn't exist — causing `fork_point()` to fail with a confusing error downstream.

---

### `ensure_image` `parent_image` parameter is dead code

Location: `cld/docker.py:74-103`, `cld/agent.py:83`, `cld/cli.py:219-222`

The new `parent_image` parameter was added to `ensure_image` to auto-build the devcontainer before the agent image (fixing PRODUCT_REVIEW §1.2). However, no caller passes `parent_image`: `launch_agent` calls `ensure_image(AGENT_IMAGE, ...)` without it, and `cld build` calls `ensure_image` twice separately. The auto-build feature does not actually work for `cld agent` runs.

---

### Editor subprocess unhandled in `_prompt_user`

Location: `cld/loop.py:1331-1339`

When the user selects `[e]dit`, `subprocess.run([editor, tf_path])` is called with `editor = os.environ.get("EDITOR", "vi")`. If the binary does not exist, this raises `FileNotFoundError`. The loop's outer `try/except` only catches `KeyboardInterrupt`, so the exception propagates uncaught, aborting the loop without running `_cleanup_temp_files`.

---

### `review-diff` and `review-task` temp files accumulate without cleanup

Location: `cld/agent.py:157-874`, `cld/loop.py:303-356`

`_cleanup_temp_files` removes `loop-impl-*`, `loop-review-*`, and `loop-diff-*` from `.cld/`. Files created by `launch_review` — `review-diff-{session}.patch` and `review-task-{session}-*.md` — are placed in `.cld/` but never removed by any cleanup path. These accumulate across review runs.

---

### `git squash` leaves HEAD on `into_rev`

Location: `cld/vcs/git.py:157-160`

The new `squash` implementation does `git checkout into_rev` before cherry-picking, leaving git HEAD on `into_rev`'s branch after the call. Any VCS operation after `squash` (in the same process or entrypoint shell context) would operate on `into_rev` rather than the original working branch. Currently benign because `squash` is the last VCS operation in the entrypoint, but the method leaves an implicit side effect undocumented.

---

### `check_status` marks "failed" when branch has no `summary.json`, but commit hash is still populated

Location: `cld/mcp/orchestrator.py:196-204`

When `summary_raw` is empty, `info["status"]` is overridden from `"completed"` to `"failed"`, but `info["commit"]` set at line 187 remains. Callers see `status=failed` alongside a valid commit hash, implying the agent committed something despite the failure designation. The intent was to flag agents that exited before the commit step, but the commit field contradicts that.
