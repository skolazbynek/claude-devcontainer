# Launching sibling containers from inside master

## Goal

`cld <anything>` invoked inside a **master** container behaves identically to
`cld <anything>` on the host, targeting the repo the user `cd`'d into. Every
launch produces a **peer** container (sibling of master), not a nested one.
This covers ephemeral `cld`, `cld agent`, `cld master`, `cld run`, and
`cld chain run`. `restart` / `shutdown` / `status` / `logs` follow the same
rule because they operate on peers through the shared Docker daemon.

## UX invariant

One rule, applied everywhere:

> **`cld <cmd>` inside master == `cld <cmd>` on the host, for the repo at
> `$PWD`.**

Consequences:

- Target selection is **cwd-based only**. No `--repo` flag, no aliasing.
- `docker ps` and `docker inspect` return the same picture from host or master
  (single daemon via `/var/run/docker.sock`), so `status`, `logs`,
  `shutdown --all` transparently see every peer regardless of origin.
- Messenger MCP already works this way; nothing changes there.
- The `agent-start` skill remains a thin wrapper around `cd <target> && cld agent`.

## Concrete flows

```bash
# Master's own repo (RW-mounted at /workspace/origin; the only bind mount
# of a target repo master ever has)
cd /workspace/current      # or /workspace/origin
cld agent                  # start / attach; idempotent per repo
cld agent restart          # tear down + relaunch peer
cld agent shutdown         # docker rm; peer forgets its own bookmark
cld run @architect         # one-shot detached peer
cld chain run mychain.yaml
cld                        # ephemeral interactive peer

# A named sibling repo (from master_targets)
cd /home/zet/projects/foo  # empty placeholder directory; no mount, ls is empty
cld agent                  # peer agent launched with -v /home/zet/projects/foo
                           #   :/workspace/origin:rw against the host filesystem
```

Discovery of what's reachable from this master:

```bash
cld master repos
# /home/zet/projects/cld   own
# /home/zet/projects/foo   target
# /home/zet/projects/bar   target
```

`cld master repos` reads the master's `master_targets` config plus the
`CLD_HOST_PROJECT_DIR` env var for own repo. No docker-inspect, no jj reads,
no side effects.

## Design rules

1. **One peer-side code path for anchor work.** Both anchor resolution
   (`resolve_anchor`) and anchor staging (`stage_anchor_with_scratch`) run
   inside the peer container's entrypoint, uniformly for master's own repo
   and for sibling targets. Master's cld does neither. Host cld is
   unchanged; only cld-inside-master delegates.
2. **Master has no filesystem view of any sibling repo, and never writes to
   any target repo.** Sibling repos are not bind-mounted into master at all
   (see "target registration" below). Master's `/workspace/origin` is RW for
   its own workspace's needs but never receives a target-repo write from cld
   during a peer launch or shutdown. The isolation is physical for siblings
   and behavioral for own repo. Any target-repo write is done by the peer.
3. **Target registration is name-only, not mount-based.** `master_targets`
   lists host paths master is allowed to launch peers against. The
   directories exist as empty placeholders inside master (created by the
   entrypoint) so `cd` and cwd-walk work; they contain no repo content.
4. **`cld master repos` is discovery-only.** Reads config, no state, no
   side effects.
5. **Host cld semantics are the reference.** Any drift between host cld and
   cld-inside-master is a bug in the master-side path.

## Delegated anchor work (the crux)

Today, `resolve_anchor` and `stage_anchor_with_scratch` both run **on the
host** before `docker run`: they read jj history, write `.cld-run/session`
to the host working copy, run `jj split --onto A`, then pass the resulting
`AGENT_ANCHOR_HASH=B` to the container. This does not work from inside
master: no bind mount of the sibling exists in master, and master's view of
its own repo lives in the wrong jj workspace for the split.

Under this design, both operations move **into** the peer container's
entrypoint:

```
master:
  target_host_path = resolve_target(cwd)          # config lookup + own repo
  revision_hint    = -r flag or ""                # unresolved; peer resolves
  scratch          = {"session": ...}             # in memory
  docker run
      -e AGENT_REVISION_HINT=<revision_hint>
      -e AGENT_SCRATCH=<base64-json>              # or via stdin / tmpfile
      -v <target_host_path>:/workspace/origin:rw

peer container entrypoint (before workspace creation):
  A       = resolve_anchor(vcs, AGENT_REVISION_HINT)
  scratch = decode(env AGENT_SCRATCH)
  B       = stage_anchor_with_scratch(vcs, A, SESSION_NAME, scratch)
  export AGENT_ANCHOR_HASH=$B
  # rest of the existing entrypoint unchanged
```

Why this works everywhere:

- **Master's own repo**: master passes only the host path (from
  `CLD_HOST_PROJECT_DIR`) and the revision string. The peer's own
  `/workspace/origin` is RW; resolution and staging succeed there.
- **Siblings**: identical. The sibling repo is mounted RW into the peer
  container even though it is not visible in master at all.
- **Workspace mismatch bug is gone**: the peer stages while its
  `vcs.repo_root == vcs.workspace_path == /workspace/origin`, so the
  `jj status` snapshot and the `jj split ... .cld-run` operate on the same
  workspace where `.cld-run/` was written.

Host cld can keep its current inline flow (no regression, no scratch env
plumbing on host). Only cld-inside-master takes the delegated path,
selected by presence of `CLD_HOST_PROJECT_DIR` / `MASTER_MODE`.

## Target registration and discovery

`master_extra_mounts_ro` is **replaced** by `master_targets`. Migration is
a rename plus a semantic shift: the values are still expanded host paths,
but they are no longer bind-mounted into master. They are registered as
launchable targets and materialized as empty placeholder directories.

```toml
master_targets = ["~/projects/foo", "~/work/bar"]
```

- **Master entrypoint** creates each path as an empty directory at boot
  (`mkdir -p`). `~` expands against `CLD_HOST_HOME` so container-path ==
  host-path. Nothing is bind-mounted; the placeholder is purely to make
  `cd <path>` succeed inside master's shell.
- **Cld target resolution inside master** consults the list rather than
  walking for `.jj/`. Cwd (or ancestor) matching a `master_targets` entry
  or matching `CLD_HOST_PROJECT_DIR` (own repo) selects that target's host
  path. Anything else is an explicit error with a hint to add the path to
  `master_targets`.
- **`cld master repos`** (new) reads config, prints `<path>  <role>` per
  line where role is `own` or `target`. Errors when run outside a master
  container.
- **Peer envelope**: launched peer containers gain `AGENT_REVISION_HINT`
  (unresolved) and `AGENT_SCRATCH` (base64 JSON) env vars. No new env vars
  on host cld's launch path.

Losing browsing: master can no longer `ls`, `cat`, or `jj log` inside a
sibling repo (previously an incidental benefit of the RO mount). If that
matters, launch an ephemeral peer (`cld` bare) against the target for a
throwaway inspection shell. This design does not add an optional
browse-only mount; if the need proves real, it becomes a separate
orthogonal config knob and does not alter target registration.

## Shutdown / bookmark cleanup

The peer container is the entity that *creates* the bookmark on launch (its
entrypoint runs `jj bookmark set <session>`). Symmetrically, the peer is
also the entity that *forgets* it on shutdown. Master never writes to the
target's jj store at any point in the shutdown flow -- master's shutdown
code path is just `docker stop && docker rm`, regardless of whether the
target is own repo or a sibling.

Mechanism -- peer self-cleanup on SIGTERM:

```
master:
  docker stop <peer>           # sends SIGTERM to PID 1
  docker rm   <peer>            # after the peer exits

peer container (PID 1 traps SIGTERM):
  jj bookmark forget "$SESSION_NAME"   # writes to /workspace/origin (RW)
  exit 0
```

Where the trap lives per peer type:

- **Agent** (`cld agent`): the supervisor daemon already handles SIGTERM to
  finish the current message. Extend its shutdown to call `jj bookmark
  forget "$SESSION_NAME"` as its last act before exiting.
- **Master** (`cld master`): the entrypoint's PID 1 shell traps SIGTERM,
  runs the forget, exits. The interactive attach sessions are `docker exec`
  processes, unaffected.
- **`cld run`** (`--rm`, one-shot): out of scope for this section. Its
  bookmark is the deliverable and is intentionally retained.

Why this is preferred over master-side forgetting:

- Master's RO view of every target repo is preserved absolutely across all
  code paths (Rule 2). No selective RW mounts, no `docker exec` reach-ins,
  no throwaway sidecar containers.
- One shutdown code path in master -- no branching between own repo and
  sibling. Matches the "one code path" ambition of Rule 1.
- Symmetric with launch: whoever creates the bookmark forgets it.

Failure mode: if the peer dies dirty (SIGKILL, OOM, host reboot before the
trap ran), the bookmark survives. Handled as best-effort with a WARN and
the manual `jj bookmark forget` command in the log, matching the current
`_forget_session_bookmark` fallback wording. A sweeper command (out of
scope) may later reconcile stale bookmarks against `docker ps` output.

Host cld today does the forget directly on its own machine
(`_forget_session_bookmark`). Migrating host cld to the same peer
self-cleanup mechanism is the natural follow-up (drops the helper, removes
a code path). Not required by this design but recommended for consistency
with Rule 5.

## Out of scope

- No bind mounts of sibling repos into master, RO or otherwise. The
  optional browse-only mount is a possible future addition, not part of
  this design.
- No repo aliasing or name-based `--repo` flag.
- No changes to messenger, mailbox, or chain internals.
- No changes to host cld's inline anchor work.
- No support for repos not listed in `master_targets` (the user must
  restart master with the path added — same UX as any config change).

## Consequences worth noting

- Master's own repo and sibling repos are treated identically at every
  step: launch, restart, shutdown, status. No branching by target kind in
  master's code paths.
- If the peer entrypoint fails during staging, the peer container exits
  non-zero. `cld run` (`--rm`) leaves nothing behind; `cld agent` / `cld master`
  leave a stopped container the user can `logs` and remove. No half-staged
  state on any target repo (staging is atomic per `jj op restore`).
- Anchor-related failures now surface through the peer's logs rather than
  synchronously to the caller. Callers wait for the peer's readiness
  sentinel (or `--rm` exit); a staging failure means the sentinel never
  appears and the existing 60 s readiness timeout fires.
