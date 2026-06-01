# Unified Anchor-Change UX

## 1. Goal & invariant

Every `cld` subcommand (`agent`, `devcontainer`, `review`, `loop`, `chain run`)
shares one notion of an **anchor change**:

> The anchor change is the immutable root from which every change produced by
> the command must descend. On invocation, the command creates a single empty
> **editable root** as a direct child of the anchor; all host-side setup
> (scratch files, composed task inputs, diff patches) and all in-container
> work happen on the editable root or on changes the command explicitly
> creates above it. The anchor is never written to.

Default anchor: the current change (`@` in jj, `HEAD` in git).
Override: `-r` / `--revision`, accepted by **every** subcommand.

Tree shape the contract guarantees:

```
anchor                (immutable; user's @ or -r, hash-pinned)
  └─ editable_root    (empty child created by the command; host writes here)
        └─ … command-specific structure …
```

What lives above `editable_root` is the command's choice (single agent
commit, per-iteration stack for `loop`, per-step stack for `chain`,
free-form edits for `devcontainer`). The shared contract enforces only the
boundary between the anchor and `editable_root` and the requirement that
all host writes happen inside the workspace directory.

## 2. Current state (per command)

| Command       | `-r` accepted | Default rev                          | Anchor hash pinned? | Empty editable root? | Scratch file location |
|---------------|---------------|--------------------------------------|---------------------|----------------------|-----------------------|
| `agent`       | yes           | empty → jj's internal default (`@-`) | no                  | jj: yes (by `workspace add` side-effect); git: no | host `<repo>/.cld/` |
| `devcontainer`| yes           | empty → jj's internal default (`@-`) | no                  | jj: yes; git: no     | n/a |
| `review`      | **no**        | n/a (inherits from `agent`)          | no                  | as `agent`           | host `<repo>/.cld/` |
| `loop`        | yes           | `@` / `HEAD`                         | resolved to hash    | no — branch points directly at anchor hash | host `<repo>/.cld/` |
| `chain run`   | yes           | `@` / `HEAD`                         | resolved to hash    | no                   | host `<repo>/.cld/` |

CLI help text on `agent`/`devcontainer` declares "default: `@-` for jj,
`HEAD` for git", which contradicts the task statement. `loop`/`chain`
already match the target default.

## 3. Gaps & bugs

1. **Default-rev policy is inconsistent.** `agent`/`devcontainer` advertise
   and forward "last committed" (`@-`); `loop`/`chain` use current (`@`);
   `review` has no knob. Target: `@` everywhere.

2. **Anchor is not hash-pinned at command entry.** Between resolution and
   `jj workspace add` / branch creation, `@` can shift (concurrent shell,
   jj auto-snapshot moving `@` after a file edit). All commands must
   resolve the anchor to a concrete commit ID **once**, up front, and use
   that hash thereafter.

3. **No explicit empty editable root on top of anchor.** jj's
   `workspace add` happens to create such a child as a side-effect; git's
   `worktree add -b NAME PATH REV` does not (HEAD == anchor on entry; the
   first agent commit retroactively becomes the empty child). Both
   backends must end in the same shape.

4. **Anchor immutability isn't enforced or surfaced.** Nothing prevents an
   agent (or interactive devcontainer user) from `jj edit <anchor>` or
   `jj squash --into <anchor>`. Exit reports never name the anchor, so a
   user can't easily diff `anchor..tip`.

5. **Host scratch files live outside the anchor tree.** Host code writes
   `.cld/loop-impl-iter*.md`, `.cld/loop-diff-*.patch`,
   `.cld/loop-review-iter*.md`, `.cld/chain-<chain>-<step>.md`,
   `.cld/persona-<chain>-<step>.md`, `.cld/review-diff-*.patch`,
   `.cld/review-task-*.md` into the **caller's** working-copy directory via
   `cld_tmpdir(repo_root)`. `.cld/` is gitignored, so jj doesn't snapshot
   them into host's `@`, but they are nonetheless host-side artifacts
   produced by the command outside the anchor tree:
     - Two simultaneous `cld` runs in the same repo collide on filenames.
     - Cleanup in `_cleanup_temp_files` is wildcard-based and
       indiscriminate — interrupts one command, wipes another's files.
     - The files have no anchor-rooted location, so the workspace they
       belong to cannot be reconstructed without host access.

6. **`loop`/`chain` ignore the secondary-workspace hint.** When invoked
   from a secondary jj workspace, `find_repo_context` returns a non-empty
   `workspace_revision`; `agent`/`devcontainer` use it as the default rev,
   but `loop`/`chain` hard-code `"@" / "HEAD"`. Effect: in a secondary
   workspace, `cld loop` anchors at the **main** workspace's `@`.

7. **No branch-collision check.** `loop` and `chain` call
   `vcs.create_branch` on `loop_<name>` / `chain_<name>` unconditionally.
   A second run with the same `-n` fails partway with half-set-up state.

8. **Legacy non-preinit path in agent entrypoint.**
   `entrypoint-claude-agent.sh` carries a fallback that re-implements
   anchor resolution in shell when `WORKSPACE_PREINITIALIZED` is not set.
   Source of silent drift.

9. **`review` has no `-r`.** No way to pick the workspace baseline.

10. **Anchor not propagated to the container.** `AGENT_REVISION` is
    forwarded but only `devcontainer` and the agent's legacy fallback
    use it. With immutability part of the contract, the container needs
    the anchor hash to guard against rewrites.

## 4. Design

### 4.1 Shared abstraction (`cld/vcs/anchor.py`)

```
resolve_anchor(vcs, revision: str) -> str
    # revision == "" -> resolves "@" (jj) or "HEAD" (git),
    #   respecting vcs.workspace_revision when in a secondary workspace.
    # Returns a concrete commit hash. Never returns a symbolic name.

create_editable_root(vcs, anchor_hash: str,
                     workspace_path: Path, branch: str) -> None
    # Creates a workspace at workspace_path whose @ is an empty descendant
    # of anchor_hash, with branch/bookmark pointing at it.
    #
    # jj:  jj workspace add --name <branch> -r <anchor_hash> <workspace_path>
    #      jj bookmark create <branch> -r @          (run inside the new ws)
    # git: git worktree add -b <branch> <workspace_path> <anchor_hash>
    #      git -C <workspace_path> commit --allow-empty -m "cld: editable root"
    #
    # Postcondition (both backends): workspace_path's @ is empty,
    # branch points at @, and @'s only parent is anchor_hash.

assert_descendant(vcs, anchor_hash: str, candidate: str) -> None
    # Raises if candidate does not have anchor_hash in its ancestry.
    # Cheap: jj log -r 'ancestors(<c>) & <anchor>' / git merge-base --is-ancestor.
```

These three helpers form the entire shared contract. There is no separate
"seed" or "inputs" concept; whatever structure a command wants above
`editable_root` is built with the existing `VcsBackend` methods.

### 4.2 Command-side opening sequence

```
1. anchor   = resolve_anchor(vcs, args.revision)
2. ws_path  = <repo>/.cld/workspaces/<session>
3. branch   = command-specific (agent_<session>, loop_<name>, ...)
              -- refuse if it already exists, unless --force
4. create_editable_root(vcs, anchor, ws_path, branch)
5. (optional) host writes scratch files into ws_path's working copy
              (see §4.4) -- no host-side writes outside ws_path
6. launch the container, mounting ws_path at /workspace/current
7. on each subsequent advance of the persistent branch:
       assert_descendant(anchor, new_tip)
8. exit report prints the anchor hash and uses anchor..tip for inspect
   commands (§4.6)
```

### 4.3 Per-command changes

**`cld agent`**
- CLI help: default-rev wording becomes "current change (`@` / `HEAD`)".
- Host: replace `effective_revision = revision or workspace_rev` with
  `resolve_anchor`. Drop the bespoke `_create_agent_workspace`; use
  `create_editable_root`.
- Container: keep the `WORKSPACE_PREINITIALIZED` path only. **Delete**
  the legacy fallback in `entrypoint-claude-agent.sh`.
- After the container exits, run `assert_descendant(anchor, "@-")` on the
  agent's branch before any host-side post-processing. Failure goes into
  `summary.json` as `"anchor_violation"`.

**`cld devcontainer`**
- CLI help wording fix as above.
- Host pre-creates the workspace via `create_editable_root` (same as
  `agent` does), passing `WORKSPACE_PREINITIALIZED=1`. The container
  entrypoint loses its workspace-creation branch entirely; both
  entrypoints take the same pre-initialised path.
- On exit, print the anchor hash next to the workspace branch.

**`cld review`**
- Add `-r` / `--revision` option (default `@`/`HEAD`).
- Resolve anchor and forward to `launch_agent` via the existing
  `revision=` param. Diff between feature/trunk is independent of the
  anchor — the anchor only sets the review agent's workspace baseline.

**`cld loop`**
- Replace `start_commit = vcs.resolve_revision(revision or default_rev)`
  with `resolve_anchor` (honors secondary-workspace hint).
- Replace `vcs.create_branch(loop_branch, start_commit)` with
  `create_editable_root(vcs, anchor, ws_path, loop_branch)`. Per-iteration
  agents stack their changes above `editable_root`; loop chooses whether
  to commit per-iteration inputs before each impl agent or to leave them
  in the working change.
- Before each `vcs.set_branch(loop_branch, impl_session)`, call
  `assert_descendant(anchor, impl_session)`. On failure, abort the
  iteration and leave the impl branch for inspection.
- Exit report: replace `jj log -r '{loop_branch}::@'` with
  `jj log -r '{anchor}..{loop_branch}'`. Print anchor hash.

**`cld chain run`**
- Same anchor resolution and `create_editable_root` call as `loop`.
- `initialise_chain_branch` becomes a thin wrapper around
  `create_editable_root`.
- `advance_chain_branch` runs `assert_descendant(anchor, session)`
  before `set_branch`.

### 4.4 Scratch-file isolation

The contract requires that **no host-side write occurs outside the
workspace directory**. Host scratch files (composed task inputs, diff
patches, persona stagings) move from:

```
<repo_root>/.cld/<file>           (current; host's main working-copy dir)
```

to:

```
<workspace_path>/.cld-run/<file>  (inside the editable_root workspace)
```

`<workspace_path>` is `<repo_root>/.cld/workspaces/<session>/` — the
per-session workspace created in step 4 of §4.2. `.cld-run/` is a new,
**not-gitignored** top-level directory, so the files are VCS-trackable
descendants of the anchor.

Mechanics:

- `cld_tmpdir(repo_root)` (host's `.cld/` writer) is removed. Replaced by
  callers receiving a `workspace_path` and writing under
  `<workspace_path>/.cld-run/`.
- Whether the command **commits** these files before launching the agent
  is the command's choice, not the shared contract:
    - `agent`, `review`: simplest path is to leave them in the working
      change; the agent's first commit absorbs them.
    - `loop`, `chain`: may prefer to commit per-iteration / per-step
      inputs as their own change so each step's history is clean.
  In every case the files reside in the editable-root workspace, so they
  are inherently rooted in the anchor tree.
- Container path references in templates (`review-template.md`,
  `loop-review.md`) switch from `/workspace/origin/.cld/<file>` to
  `/workspace/current/.cld-run/<file>`. The container reads its inputs
  from its own workspace, not via the bind-mounted host origin.
- Wildcard cleanup (`_cleanup_temp_files` and its glob list) is deleted
  outright. Scratch files live and die with the workspace; forgetting the
  workspace (`vcs.forget_workspace`) reclaims the directory.
- Concurrency: per-session workspaces are already separate directories,
  so cross-run collisions on scratch filenames become structurally
  impossible.

`CLAUDE.md` documents `.cld-run/` as the cld-reserved scratch directory
inside agent workspaces, never present in the host's main working copy.
No change to repo `.gitignore`.

### 4.5 Container-side guard

Add one helper to `vcs-lib.sh`:

```
vcs_assert_descendant ANCHOR REV   # exit 1 if REV does not descend from ANCHOR
```

`entrypoint-claude-agent.sh` reads the anchor hash from a new env var
`AGENT_ANCHOR_HASH` (set by the host alongside `WORKSPACE_PREINITIALIZED`)
and asserts:
- once before `vcs_commit`,
- once before `vcs_squash_into_parent`.

On failure, write `AGENT-FAILURE.md` with a machine-readable reason
(`anchor_violation: <observed parent> is not a descendant of <anchor>`)
and exit non-zero. This is the in-container half of the host-side
post-step check, catching damage that happens between commit and squash.

### 4.6 Output / reporting

Every exit report gains:

```
Anchor:    <12-char hash>   (resolved from "<symbolic input>", e.g. "@", "main", or user-supplied rev)
```

Inspect commands switch to the anchor-relative form:

- jj:  `jj log -r '<anchor>..<branch>'`, `jj diff --from <anchor> --to <branch>`
- git: `git log <anchor>..<branch>`, `git diff <anchor>..<branch>`

## 5. Backwards-compatibility notes

- `AGENT_REVISION` env var is preserved for now but only as a hash.
  `AGENT_ANCHOR_HASH` is added. Update CLAUDE.md's container-side table.
- Behavioural change for users who currently rely on
  `cld agent`/`devcontainer` defaulting to `@-`: they now default to
  `@`. Matches the task contract and what `loop`/`chain` already do.
  Note in CHANGELOG / release notes.
- Removing the agent entrypoint's legacy non-preinit branch means direct
  `docker run claude-agent:latest ...` (bypassing `cld agent`) stops
  working unless the caller pre-creates the workspace. Acceptable: that
  path is not a supported entry point.
- `.cld-run/` becomes a reserved directory name inside agent workspaces.
  Repos that already use this name at the top level would conflict;
  scan repos at upgrade time (one-line grep) before shipping.

## 6. Implementation order (suggested)

1. Add `cld/vcs/anchor.py` with `resolve_anchor`,
   `create_editable_root`, `assert_descendant`; unit tests against both
   backends.
2. Migrate `cld agent` host path to use the helpers; delete legacy
   container fallback in `entrypoint-claude-agent.sh`.
3. Migrate `cld devcontainer` host path; collapse the container's
   workspace-creation branch.
4. Add `-r` to `cld review`; route through `resolve_anchor`.
5. Migrate `cld loop` (anchor + editable_root + post-step
   `assert_descendant`); add anchor printing.
6. Migrate `cld chain run`; same shape.
7. Move scratch-file writers (`launch_review`, `_compose_review_prompt`,
   `_compose_iter_prompt`, `compose_task`,
   `_stage_persona_without_frontmatter`) from `<repo>/.cld/` to
   `<workspace_path>/.cld-run/`. Update template paths from
   `/workspace/origin/.cld/...` to `/workspace/current/.cld-run/...`.
   Delete `cld_tmpdir` and `_cleanup_temp_files`.
8. Container-side `vcs_assert_descendant` + entrypoint guards;
   propagate `AGENT_ANCHOR_HASH`.
9. CLI help text + CLAUDE.md update (default-rev wording, env-var
   table, `.cld-run/` description).
10. End-to-end tests: `cld <each command> -r <hash>` then verify
    `assert_descendant(<hash>, <session_branch>)` holds; deliberate-
    mutation test in a sandbox confirming both the host-side check and
    the container-side guard fire.
