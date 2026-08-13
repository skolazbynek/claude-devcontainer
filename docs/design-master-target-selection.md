# Target selection from inside a master container

> **Status: design only.** Requested by `docs/next-steps-todo.md` § "Empty mounted
> directories". Nothing here is implemented; `docs/design-master-sibling-launch.md`
> still describes the shipped behaviour.

## The question

`cld master` materializes one **empty placeholder directory** per `master_targets`
entry inside the container. Are they needed? Is there another way, with today's
task-agent / broker machinery, to launch an agent against a different repo from
inside master?

## Answer: needed today, but the wrong shape

**Not redundant.** cwd is the *only* target selector that exists in-master:

- `resolve_master_target(cwd, cfg)` (`cld/docker.py`) is the single in-master
  resolver. It translates cwd back to a host path (`to_host_path`) and matches it
  against `MASTER_TARGETS`; a non-matching cwd is a hard error.
- `find_target_repo(cfg)` funnels every in-master verb through it:
  `_dispatch_agent_to_broker` (agent start/restart/shutdown/status/logs),
  `_dispatch_task_agent_to_broker` (task-agent start/status/logs/shutdown),
  `_cwd_repo_task_agent_name` → `_resolve_task_agent` (slug disambiguation), and
  `_task_agent_repo_root`'s fallback.
- The broker's `agent` / `task-agent` actions take `<target>` as an absolute host
  path, so *something* in the container has to produce one.

Delete the placeholders with no replacement and master loses all cross-repo
capability. So this is the TODO's second branch: a simpler design, documented,
**not implemented**.

### Four defects of the placeholder mechanism

1. **Placeholder path ≠ host path.** The entrypoint mirrors each target under the
   container `$HOME` (swapping the `$CLD_HOST_HOME` prefix), because an
   unprivileged container user can only `mkdir` under its own home. With host
   targets under `/home/zet/projects/`, inside master you must `cd
   ~/projects/foo`; `cd /home/zet/projects/foo` fails. `design-master-sibling-launch.md`
   § "Target registration" claims container-path == host-path. It cannot be.
2. **It imposes an unrelated config constraint.** `build_container_args` hard-exits
   when a `master_targets` entry is not under the host home dir — purely so a
   mirror path exists. A repo at `/srv/work/x` can never be a target, for a reason
   that has nothing to do with launching agents.
3. **Discovery is broken exactly where it is needed.** `cld master repos` reads
   `cfg.master_targets`, i.e. host TOML that does not exist inside the container;
   the host-set `MASTER_TARGETS` env var is the real table. Verified in a live
   master: `repos` printed only the `own` row while `MASTER_TARGETS` carried seven
   entries. The documented way to find a `cd` destination does not show it.
4. **A placeholder is indistinguishable from a broken target.** `ls` is empty,
   `jj log` fails, and any typo'd `mkdir` under `~/projects/` produces something
   that looks exactly like a registered target but resolves to nothing.

The invariant the placeholders serve — *"`cld <cmd>` inside master == `cld <cmd>`
on the host, for the repo at `$PWD`"* — is also already dead: since the docker
socket was removed, bare `cld`, `cld run`, `cld master` and `cld chain run` are all
refused in-master. Only `agent` and `task-agent` remain, and both are
broker-delegated. cwd-parity buys nothing that survives.

## Alternatives considered

| | Approach | Verdict |
|---|---|---|
| **A** | Status quo: cwd + `$HOME`-mirrored placeholders | Defects 1–4; keeps a dead invariant alive. |
| **B** | **Explicit `--repo <name\|path>`, no placeholders** | **Recommended.** Removes the mkdir, the home-dir constraint and the cwd translation; discovery becomes the flag's own value list. |
| **C** | RO bind-mount every target into master | Restores browsing and cwd honesty, but breaks design rule 2 (master has no filesystem view of siblings) and re-opens the exposure the socket removal closed. Rejected. |
| **D** | Registered aliases only (`--repo foo`, never a path) | B minus the escape hatch for basename collisions. B's path form costs one branch; keep it. |
| **E** | Make placeholders honest: `--tmpfs <host-path>` per target so container-path == host-path anywhere | Fixes defect 1 and 2, not 3 or 4, and adds N tmpfs mounts plus a root-created mountpoint per target — all to preserve cwd-parity, which no longer exists. Rejected. |

## Recommended design (B): explicit target selection

### Grammar

```
cld task-agent start … [--repo <ref>]
cld agent           … [--repo <ref>]
cld task-agent status|logs|transcript|shutdown … [--repo <ref>]
```

- **omitted** → master's own repo (`CLD_HOST_PROJECT_DIR`). The common case stays
  flag-free, which is why this is not a UX regression for single-repo use.
- **`<basename>`** → the unique registered target (own repo included) whose
  basename matches. A collision is an error listing both full paths.
- **`<absolute host path>`** (`~` expanded against `CLD_HOST_HOME`) → must equal own
  repo or a registered target exactly. The escape hatch for a collision.
- **anything else** → error naming `cld repos` as the discovery surface.
- **cwd is not consulted in-master at all.** On the host, cwd-walk stays the rule;
  `--repo` is a container-CLI option only, so host semantics do not change.

### Resolver

One function replaces `resolve_master_target`:

```python
resolve_target_repo(ref: str, cfg: Config) -> str   # -> absolute host path
```

reading `MASTER_TARGETS` and `CLD_HOST_PROJECT_DIR` — host-set env vars, the only
trustworthy target table inside a container (in-container TOML is not the operator's
config; see defect 3). `find_target_repo(cfg)` keeps its host behaviour and gains an
optional ref.

### What gets deleted

- the `MASTER_TARGETS` materialization loop in
  `imgs/claude-devcontainer/entrypoint-claude-devcontainer.sh`;
- the "not under your home directory" refusal (and its `home`-prefix arithmetic) in
  `build_container_args`;
- cwd→host translation *for target resolution* (`to_host_path` stays for persona,
  task-file and mailbox paths);
- the container-path == host-path claim in `design-master-sibling-launch.md`.

### What stays, unchanged

- `MASTER_TARGETS` env (now purely a resolution table) and the host-set
  `org.cld.targets` label. **The security boundary does not move**: the broker still
  resolves `<target>` against the label via `validate_target`, so an explicitly
  named repo is refused exactly like a cwd-derived one was.
- The broker wire format: `<action> <session> <base64-argv>` with `<target>` an
  absolute host path. `--repo` is resolved container-side before dispatch.
- `master_targets` as the config key, so no user config migration.

### Discovery

`cld master repos` (`cld repos` after the container-CLI split) reads
`MASTER_TARGETS` instead of in-container TOML, and prints the basename beside the
path so its output *is* the list of legal `--repo` values:

```
foo   /home/zet/projects/foo   target
cld   /home/zet/projects/cld   own
```

### Consequences

- Targets may live anywhere on the host, not just under `$HOME`.
- `cd ~/projects/foo` stops working (the directory is gone) — replaced by
  `--repo foo`. Since nothing but agent launching was possible there, nothing else
  is lost.
- Slug disambiguation (`_resolve_task_agent`) stops depending on cwd in-master and
  takes `--repo` instead; on the host it stays cwd-based.
- `cld repos` becomes load-bearing rather than decorative, and correct.

### Tests the implementation would need

- Resolver unit table: own-repo default, basename hit, basename collision (error
  lists both), absolute-path hit, `~` expansion, unknown ref (error names `repos`).
- `test_docker`: `MASTER_TARGETS` env + `org.cld.targets` label still set for a
  master; no home-dir refusal for a target outside `$HOME`.
- Broker-path: an unregistered `--repo` value is refused by `validate_target`
  (the boundary check is unchanged, so this is a regression guard).

## Out of scope

- Browsing a sibling repo from master (still: launch an ephemeral peer against it).
- Fan-out of one command across several repos.
- Any change to how the broker validates targets, or to the host cwd-walk.
