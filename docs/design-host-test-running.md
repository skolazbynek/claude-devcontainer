# Host-side test running (`runtests` + SSH broker)

**Status:** Phases 1–3 implemented (`runtests/`, `host-broker/`, cld plumbing). Phase 4 (setup UX) deferred.
**Date:** 2026-07-29
**Related:** Option A (SSH forced command) brainstorm; supersedes the in-container
`.env`-injection approach for the *test-running* use case.

## 1. Problem

Running a target repo's test suite needs authentication secrets (MySQL / Redis /
etc.) that live in a gitignored `.env` on the host. Two hard requirements:

1. **Secrets must never be readable by `claude`** and the raw `.env` must not be
   mounted into the `cld master` container.
2. **Tests must run against `claude`'s in-progress change** (the master
   container's jj workspace), **without disturbing the host user's current VCS
   state** (`@`). The user keeps absolute control over what change their host
   working copy is on.

The chosen approach keeps secrets entirely on the host: the master container can
only *trigger* a fixed host-side command over SSH (Option A), and that command
launches an **isolated, ephemeral `runtests` container** that materializes the
change under test in its own jj workspace and runs pytest. `claude` sees only the
streamed test output.

## 2. Locked decisions

| # | Decision |
|---|---|
| 1 | The docker socket is **no longer mounted** into any cld container. **Master has no docker socket** — the broker (SSH) is `claude`'s only host channel, so secrets genuinely stay away from `claude`. In-container docker needs (peer enumeration, sibling `cld agent` launches) route through broker actions; see `cld/host_docker.py` and the `list-containers`/`agent` actions in `host-broker/host-broker.sh`. |
| 2 | The **raw `.env` may be mounted** into the `runtests` container (`claude` cannot reach that container). |
| 3 | `runtests` accepts an **arbitrary `REVISION`**; when run ad-hoc it **defaults to `@`**. In the brokered path the broker supplies the change. |
| 4 | The broker passes **the current change of the `cld master` container's jj workspace**. |
| 5 | The broker is **purely store-reading**: it resolves the master session bookmark's tip from the jj store. It does **not** `docker exec` into master to force a snapshot (accepts minor watchman-snapshot lag). |
| 6 | `runtests` is a **completely separate, self-contained project**: its own directory, minimal Debian base, its own Dockerfile/scripts, **no dependency on the cld app** (no `import cld`, no cld env-var names, not in the cld image hierarchy). Single job: run pytest at a revision. |
| 7 | `runtests` is **startable with arbitrary pytest arguments** (argv passthrough). |
| 8 | Name of the container/image/project: **`runtests`**. |

## 3. Key insight

Docker is **not** what protects the host's VCS state — **jj's multi-workspace
model is**. `jj workspace add --name <ws> -r <rev> <dir>` creates an *independent*
working copy (its own `@`, its own directory) sharing only `.jj/repo/store`. It
**never moves the default workspace's `@`, bookmarks, or working copy** — the same
guarantee `cld master`/`agent` already rely on.

- **jj secondary workspace** → VCS-view isolation (test the change without touching host `@`).
- **Docker** → environment + secret isolation (right deps/certs, no host pollution, `claude` sees only stdout).

## 4. Architecture

```
┌─ STANDALONE PROJECT: runtests/ ───────┐   ┌─ cld-side glue (host) ───────────────┐
│ own Dockerfile + entrypoint, Debian   │   │ host-broker.sh  (SSH ForceCommand)   │
│ jj + python + poetry + CA certs       │◀──│ resolves master session → REVISION   │
│ single job: pytest @ a revision       │   │ (store-reading), then docker run     │
│ zero cld coupling                     │   │ --rm runtests …                      │
└───────────────────────────────────────┘   └──────────────────────────────────────┘
                                                        ▲
                                          ┌─ cld plumbing (in master image) ─┐
                                          │ restricted SSH key + host-gateway │
                                          │ `host-run` wrapper on PATH        │
                                          └───────────────────────────────────┘
```

Three units, one coupling point (the broker):

- **`runtests`** — generic tool. Knows a store, a revision, a `.env`, pytest args.
- **broker** — the only cld-aware piece; translates "master's current change" → a plain `REVISION`.
- **cld plumbing** — mounts the restricted key, adds host-gateway, installs `host-run`.

## 5. Component A — the `runtests` container (standalone)

Lives in a **separate top-level directory** `runtests/` (self-contained; movable to
its own repo later with no edits):

```
runtests/
  Dockerfile          # FROM debian:stable-slim; install jj, python3, poetry, CA certs
  entrypoint.sh       # the ~25-line single job
  README.md           # standalone usage, no cld references
```

**Image:** minimal Debian + `jj`, `python3`, `poetry`, build deps, and the internal
CA bundle (needed for TLS to the DBs). Nothing from cld.

**Contract (the entire interface):**

| Input | Via | Default | Meaning |
|---|---|---|---|
| jj store | `-v <repo>:/repo` (RW) | — | repo whose store holds the change |
| `REVISION` | `-e` | `@` | the change to test (any revset) |
| secrets | `-v <.env>:/secrets/.env:ro` | — | raw `.env`, sourced into env |
| `SECRETS_FILE` | `-e` | `/secrets/.env` | override secrets path |
| `PROJECT_SUBDIR` | `-e` | `.` | where `pyproject.toml` lives |
| `POETRY_INSTALL_ARGS` | `-e` | `--all-extras --all-groups` | `poetry install` flags (install test deps however declared) |
| `PYTEST_ADDOPTS` | `-e` | `--tb=short --disable-warnings -q --maxfail=30` | default pytest opts; explicit argv wins |
| `OUTPUT_MAX_BYTES` | `-e` | `65536` | cap on returned output (last N bytes = the summary) |
| pytest args | container **argv** | `(none)` | passed straight to pytest |

Image ships **Poetry 2.x** (reads PEP 621 `[project]` metadata; 1.x does not).

**Standalone usage (no cld involved):**
```
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v /path/to/repo:/repo -v /path/to/repo/.env:/secrets/.env:ro \
  -e REVISION=xyz  runtests:latest  -k login -x tests/unit
```

**`entrypoint.sh` (sketch):**
```bash
#!/usr/bin/env bash
set -euo pipefail
: "${REVISION:=@}"; : "${PROJECT_SUBDIR:=.}"; : "${SECRETS_FILE:=/secrets/.env}"
export HOME="${HOME:-/tmp}"

# jj needs an author identity to create the workspace's working-copy commit.
export JJ_CONFIG=/tmp/jj-config.toml
printf '[user]\nname = "runtests"\nemail = "runtests@localhost"\n' > "$JJ_CONFIG"

cd /repo
ws="runtests-${HOSTNAME:-$$}"                                  # unique per container; no SIGPIPE pipe
work="$HOME/rt-workspace"                                      # under $HOME: writable with --user
trap 'jj workspace forget "$ws" >/dev/null 2>&1 || true' EXIT   # cleanup even on crash

jj workspace add --name "$ws" -r "$REVISION" "$work"           # ← never touches host @
cd "$work/$PROJECT_SUBDIR"

set -a; [ -f "$SECRETS_FILE" ] && . "$SECRETS_FILE"; set +a      # secrets → env for pytest
poetry install -q --no-interaction
poetry run pytest "$@"                                          # arbitrary args; exit code preserved
```
`set -e` + no `exec` means a failing pytest exits with pytest's code and still fires
the `EXIT` trap that forgets the workspace.

## 6. Component B — the host broker (cld glue, Option A `ForceCommand`)

Runs on the host as the host user (has secrets, docker, jj). It is **not** pinned
to a repo: it serves any repo that has a running master, resolving the target from
that master container's host-set `org.cld.repo-root` label (Option A of §13). The
scoping boundary is the fixed `ForceCommand` + action set, not a repo pin.

**Broker config (host-side):** `RUNTESTS_IMAGE`, `PATH` — broker-wide only, nothing
per-repo.

**Dispatch model.** `$SSH_ORIGINAL_COMMAND` is `<action> <session> <base64-argv>`.
The action resolves to a shell function `action_<name>` (adding an action = defining
a function; unknown ⇒ denied). Shared context is prepared once, then the function runs.

**`host-broker.sh` (sketch):**
```bash
read -r action session payload <<<"$SSH_ORIGINAL_COMMAND"
[[ "$action" =~ ^[a-z][a-z0-9-]*$ ]] || exit 2
fn="action_${action//-/_}"; declare -F "$fn" >/dev/null || exit 2      # unknown action
[[ "$session" =~ ^cld_master_[A-Za-z0-9_-]+$ ]] || exit 2

# Repo from the calling master's label (trusted, host-set) — no whitelist, no caller path.
REPO=$(docker inspect "$session" --format '{{index .Config.Labels "org.cld.repo-root"}}')
[ -n "$REPO" ] && { [ -d "$REPO/.jj" ] || [ -d "$REPO/.git" ]; } || exit 3   # no such master

REV=$(jj -R "$REPO" log --no-graph -n1 -r "$session" -T commit_id) || exit 3  # store-reading only

# Project subdir: <repo>/. by default; overridable by pyproject_dir in <repo>/.cld/config.toml.
PROJECT_SUBDIR=$(cld_conf_get "$REPO/.cld/config.toml" pyproject_dir); : "${PROJECT_SUBDIR:=.}"
SECRETS_ENV_FILE="$REPO/$PROJECT_SUBDIR/.env"     # (absolute PROJECT_SUBDIR used as-is)

mapfile -d '' args < <(printf %s "$payload" | base64 -d)   # no eval, no host injection
"$fn" "${args[@]}"

# action_run_tests: docker run --rm -v $REPO:/repo [-v $SECRETS_ENV_FILE:/secrets/.env:ro]
#                   -e REVISION=$REV -e PROJECT_SUBDIR=$PROJECT_SUBDIR $RUNTESTS_IMAGE "$@"
```

**Safe arbitrary-args over SSH.** The container-side `host-run` wrapper NUL-joins and
base64-encodes its argv; the broker decodes into an argv array and never `eval`s it.
Result: arbitrary pytest args are allowed, but they can only ever be argv to pytest —
never a new host command.

## 7. Component C — cld plumbing (in the master image)

**Config (`cld/config.py`):**
```python
host_broker_key: str = ""                              # host path to dedicated PRIVATE key; empty = off
host_broker_endpoint: str = "host.docker.internal:2222"
host_broker_known_hosts: str = ""                      # pinned host key
```

**`build_container_args` (guarded on `host_broker_key`):**
```
--add-host=host.docker.internal:host-gateway          # Linux, Docker 20.10+
-v <key>:/run/secrets/host-broker-key:ro
-v <known_hosts>:/run/secrets/host-broker-known:ro
-e CLD_HOST_BROKER=host.docker.internal:2222
```

**`host-run` wrapper (installed by `container-init.sh` when `CLD_HOST_BROKER` set):**
```bash
payload=$(printf '%s\0' "$@" | base64 -w0)
exec ssh -i /run/secrets/host-broker-key \
  -o UserKnownHostsFile=/run/secrets/host-broker-known \
  -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes \
  -p "${CLD_HOST_BROKER##*:}" broker@"${CLD_HOST_BROKER%%:*}" \
  -- "run-tests $SESSION_NAME $payload"
```

**Host setup (once, pre-launch):** dedicated hardened `sshd` (own port bound to the
docker bridge gateway) with `Match User cld-broker` / `ForceCommand
/opt/cld/host-broker.sh`, plus the dedicated keypair. `cld` only ships the client
side (key mount + wrapper + image name); the sshd + broker script are host setup.

## 8. End-to-end workflow

```
master> host-run -k login -x tests/                       (claude or user)
  → ssh (restricted key) → host sshd → ForceCommand host-broker.sh
      → validate action=run-tests, session=cld_master_…
      → REV = tip of session bookmark   (store-reading; may lag watchman by seconds)
      → docker run --rm runtests  -e REVISION=REV  -v repo  -v .env  -- -k login -x tests/
          → jj workspace add -r REV /work        (host @ untouched)
          → source .env; poetry install; pytest -k login -x tests/
          → stdout/stderr + exit code
  ← streamed back over SSH ← ← ← ← ← ← ← master shows results; secrets never present
```

## 9. Security model & isolation boundary

- **Scoped, pre-configured, not dynamic:** the command lives host-side in the broker
  config + `ForceCommand`. The container can only pass a session id (validated) and
  pytest argv. It cannot change *what* runs or run anything else on the host.
- **Secret isolation depends on the socket being gone** (decision 1). With no socket,
  `host-run` is `claude`'s only host channel, and it returns only test output. The
  raw `.env` is mounted into the ephemeral `--rm` runner, which `claude` cannot reach.
- **VCS isolation:** `jj workspace add` guarantees the host user's `@` is never moved.
- **Repo scoping (multi-repo, §13):** the target repo comes from the calling master's
  host-set label, never caller input, so a request can only reach a repo that already
  has a master — not an arbitrary host path.
- **Blast radius:** a leaked key unlocks the defined actions for any repo that has a
  running master. No per-master key isolation (accepted; single-user host).

| Unit | Knows about | Coupling |
|---|---|---|
| `runtests` | store, revision, `.env`, pytest args | **none** (standalone) |
| broker | cld session naming, repo label, cld-config secrets key, image | the single bridge |
| cld plumbing | key/endpoint, `host-run`, image name | client side only |

## 10. Caveats

1. **Snapshot freshness** — store-reading the bookmark tip can be seconds behind
   `claude`'s latest edit (watchman lag). Accepted per decision 5.
2. **Concurrent store access** — master's watchman writes while `runtests` does
   `workspace add`; jj supports concurrent workspaces via its op-log, but this is
   the one behavior to explicitly exercise.
3. **Transient store writes** — `workspace add`/`forget` add ephemeral objects
   (GC'd later); they never touch the user's `@` or bookmarks.
4. **Ownership** — the runner runs `--user host-uid:gid` so store writes match host
   ownership; `HOME=/tmp` since that uid may not exist in the minimal image.
5. **CA bundle & poetry layout** — the image must bake the internal CAs;
   `PROJECT_SUBDIR` covers non-root `pyproject.toml`. Monorepos with several
   projects need a small extension.

## 11. Implementation plan

**Phase 1 — `runtests` standalone (no cld). ✅ done.**
- `runtests/Dockerfile` (Debian slim + jj + python3 + poetry + vendored CA certs), `entrypoint.sh`, `README.md`, `build.sh`, `ca-certs/`.
- *Verified:* image builds; against a real jj repo the entrypoint materializes `@` in an isolated `/work`, the origin `@` is unchanged, the mounted `.env` reaches pytest (missing → WARN + fail), pytest's exit code propagates, arbitrary argv (`-k`) passes through, and the workspace is `forget`-ten on exit. (The cross-host bind mount itself is exercised only in real deployment; this sandbox's docker daemon lives in a separate namespace.)
- *Implementation note:* the workspace name is derived from the container hostname, not `tr … | head` — the latter trips `set -o pipefail` via SIGPIPE.

**Phase 2 — host broker + sshd. ✅ done.**
- `host-broker/host-broker.sh`, `host-broker.conf.sample`, `sshd_cld_broker.conf`, `keygen.sh`, `README.md`.
- *Verified (with jj/docker shims):* a valid `run-tests <session> <b64>` resolves the revision via `jj -R <repo> log -r <session>` and builds the correct `docker run` with decoded pytest argv; bad action, path-traversal sessions, and shell-metachar sessions all exit non-zero. `keygen.sh` produces the client/host keys, `restrict`-prefixed authorized_keys, and the known_hosts line.

**Phase 3 — cld plumbing. ✅ done.**
- Config fields `host_broker_key` / `host_broker_endpoint` / `host_broker_known_hosts` (§7), `stage_host_broker()` wired master-only into `build_container_args`, `host-run` wrapper in `container-init.sh` (supports `[user@]host:port`), docs (CLAUDE.md, config template, `.claude/skills/host-run-tests/`), and `TestStageHostBroker` unit tests.
- *Verified end-to-end, including from a live master:* `stage_host_broker` mounts/gateway/endpoint tests pass; the generated `host-run` wrapper parses endpoints and emits the exact base64(NUL-joined) payload the broker decodes. With the host-side sshd running, `docker exec`'d into a real running `cld_master_<repo>_*` container, `/tmp/bin/host-run -q --collect-only` correctly resolved the repo via the container's `org.cld.repo-root` label, the broker ran `runtests` against that session's current change, and pytest collected the suite with exit 0 -- confirming the label-based multi-repo resolution (not just a single hard-pinned repo) works against a real master container, not just a synthetic session.

**Phase 4 — operation UX. ✅ done (operation); init/test still optional.**
- `host-broker/brokerctl.sh` drives the daemon: `start` / `restart` / `shutdown` / `status` / `logs`. Starts sshd detached (no `-D`), tracks it by PID file, logs to `$CLD_BROKER_DIR/sshd.log`, idempotent `start`, surfaces the `/run/sshd` privsep hint on failure. First-time setup stays manual (`broker-setup-home.md`); day-to-day is seamless.
- *Verified:* control logic (status/shutdown/bad-verb/logs) exercised with a fake pidfile; sshd itself not run in this sandbox (not installed).
- Still optional: `cld host-broker init|test` to scaffold keys/sshd/authorized_keys and smoke-test end to end.

## 12. Deferred / out of scope

- Docker socket removal from `cld master` (assumed done; tracked elsewhere).
- Materializing tests on the host directly (rejected — Docker gives the right env).
- Short-lived credential minting (Shape 2) — not feasible in this environment.
- Monorepo multi-project test orchestration.

## 13. Multi-repo (any repo, no whitelist)

The broker is not pinned to a repo. Per request it learns the target from the
**calling master container's `org.cld.repo-root` label** — set host-side at launch
(`build_container_args`), so it is trusted, not caller input:

```
REPO = docker inspect <session> --format '{{index .Config.Labels "org.cld.repo-root"}}'
```

`<session>` == the master container `--name`, and the regex already pins it to
`cld_master_*`. A session that names no running master resolves to empty → denied.
This covers *any* repo, because `host-run` only ever runs inside a master, so a
master (hence the label) always exists.

**Secrets per repo.** Default `<repo>/.env`; overridable by `pyproject_dir` in the
repo's own `<repo>/.cld/config.toml` (the broker reads that one flat key directly — no
docker label, no cld import). Relative to the repo root. A missing file is fine: the
runner just runs without it.

**PROJECT_SUBDIR follows `pyproject_dir`.** `pyproject.toml` and `.env` are assumed to
share a directory, named directly by `pyproject_dir` (→ `.` by default, `svc` for a
`svc/pyproject.toml` + `svc/.env` layout). One knob covers both.

**Decisions (this round):** (A) resolve via the container label — no whitelist, no
per-repo broker config; (spoof) **no** guard — one shared key, so any master can
target any *active* master's repo; results stream back but raw secrets do not; bounded
and accepted on a single-user host; (secrets) repo-root `.env` default, per-project cld
config override rather than a label; (subdir) `PROJECT_SUBDIR` = `pyproject_dir` directly,
since `pyproject.toml` and `.env` always share a directory — one dedicated config key.

## 14. Output limiting (context hygiene)

A failing run of a large suite can be tens of thousands of lines; feeding all of it
back would clog the caller's context. Two bounds live in the `runtests` entrypoint,
both overridable:

- **`PYTEST_ADDOPTS` default** `--tb=short --disable-warnings -q --maxfail=30`. Short
  tracebacks, no warning noise, and stop after 30 failures — past ~30 it's almost
  always a structural problem (e.g. DB connectivity) with every test failing, so the
  rest adds nothing. Explicit argv passed to the container still wins (verified: a
  caller `--maxfail=3` overrides the default).
- **`OUTPUT_MAX_BYTES` cap** (default 64 KiB) over the combined install+pytest log:
  the entrypoint buffers to a file and, if it exceeds the cap, emits only the last N
  bytes (pytest's summary sits at the end) with a truncation notice on stderr. pytest's
  exit code is preserved either way.

Rejected for now: structured junit digest and full-log-on-demand (design §5 option 5) —
the flag defaults + cap were judged sufficient. Revisit if digests still get large.
