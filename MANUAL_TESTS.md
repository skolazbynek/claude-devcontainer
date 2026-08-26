# Manual Test Plan: Task Agents

Prerequisites for every section below: run from inside a `cld master` container, with
the host broker configured.

```bash
[ -x cld broker ] && [ -d /var/cld/mailboxes ] && echo master-ok
```

If this fails, spawn/reap steps won't work but read-only steps (`status`,
`transcript`, `fleet_digest`) still will — note that when you hit the failure.

---

## 1. Start a task agent in the master's own repo

**Steps**

1. `cld master repos` — confirm the master's own repo is listed, tagged `own`.
2. `cd` into that path (should already be cwd inside master).
3. `cld task-agent start <persona> -n smoke-own -p "Add a one-line comment to README.md and report back."`

**Observe / confirm success**

- Command output prints container name, slug, persona, branch, anchor hash, and
  pointer commands (`status`, `logs`, `transcript`).
- `docker ps --filter label=org.cld.kind=task-agent --filter name=smoke-own` shows a
  running container.
- `docker inspect <container>` labels include `org.cld.kind=task-agent`,
  `org.cld.parent-master=<this-master>`, `org.cld.repo-root`, `org.cld.task=smoke-own`.
- `ls /var/cld/mailboxes/<container>/` shows `meta.json` present.
- `cld task-agent status smoke-own` shows `Container: running`, `Phase: kickoff` then
  `idle` within ~60s.
- `jj log` in the repo shows a new deliverable bookmark (default name = slug) at the
  anchor, plus the session workspace bookmark.

**Debug if it fails**

- Spawn times out after 60s: `Error: task-agent '<session>' did not become ready
  within 60 s.` → run `cld task-agent logs smoke-own` (supervisor stderr) and
  `docker logs <container>`.
- `task-agent cap reached for <master>: N/N running [...]` → reap an existing agent
  (`cld task-agent shutdown <name>`) or raise `max_task_agents`/`CLD_MAX_TASK_AGENTS`.
- `Error: the host broker is not configured for this master...` → check
  `broker_key`/`CLD_BROKER_KEY` config; read-only commands still work.

---

## 2. Start a task agent in a different (sibling) repo

**Steps**

1. `cld master repos` — pick a path tagged `target` (a registered `master_targets`
   entry).
2. `cd <that-path>` (an empty placeholder dir inside master — no real repo content
   visible locally).
3. `cld task-agent start <persona> -n smoke-sibling -p "<task text>"`

**Observe / confirm success**

- Same container/label/mailbox checks as §1, but `org.cld.repo-root` points at the
  sibling repo's host path, and the peer container launches with
  `-v <sibling-host-path>:/workspace/origin:rw` (visible via `docker inspect` mounts).
- Because the master has no filesystem view of the sibling repo, you cannot `jj log`
  locally for this one — verification of committed work must wait for wrap-up (§4).

**Debug if it fails**

- `Error: anchor revision resolves inside a live agent's stack` → another live
  task-agent (or the master) already owns that anchor; reap it first
  (task-agent handoffs are strictly reap-then-spawn).
- Broker-side failures (wrong target, SSH key/known_hosts issues reaching the sibling
  host) surface as broker errors on stdout — cross-check
  `cld broker run-tests -k login -x <target>` manually if the error is opaque.

---

## 3. Set and control peer edge (communication) limits

Peer edges are declared once, at spawn time, on the *later*-spawned side only — the
earlier agent may only reply, regardless of its own `peers` map.

**Steps**

1. Start a first agent: `cld task-agent start <persona> -n peer-a -p "..."`. Note its
   full container name (from the start output).
2. Start a second agent that declares a peer edge with a small budget for easy
   testing: `cld task-agent start <persona> -n peer-b -p "..." --peer <peer-a-container>:3`
3. Instruct `peer-b` (via `messenger MCP send()` or `python -m cld.messenger.send`, with
   `--expects-reply` so it gets answers back) to message `peer-a` repeatedly (more than
   3 times) as part of its task, to exhaust the budget. Note that the ask budget
   (`root_ask_limit`, default 3) fires first if `peer-b` keeps *asking* without either
   side answering — that refusal names the ask limit, and unlike the hop refusal the
   edge stays usable for answers.

**Observe / confirm the edge is set correctly**

- `cat /var/cld/mailboxes/peer-b/meta.json` — `peers` map contains
  `{"<peer-a-container>": 3}`.
- `docker inspect <peer-b-container>` — env var `AGENT_PEERS` contains
  `<peer-a-container>:3`.
- `cld task-agent status peer-b` — detail view prints `Peers: <peer-a> (3 hops)`.

**Observe / confirm enforcement while messages flow**

- After each successful send: `cat /var/cld/mailboxes/_edges/peer-a--peer-b.json`
  (endpoints sorted alphabetically) — `count` increments toward `limit: 3`.
- `cld task-agent transcript peer-b` (and `peer-a`) — budgeted messages are annotated
  with a `hops` value; both directions consume the same counter.
- MCP `send()` return value on a successful budgeted send:
  `{"id":..., "to":..., "hops": n, "limit": 3}`.

**Observe / confirm violation (budget exhausted)**

- Once `count == limit`, the next send in either direction: MCP `send()` returns
  `{"error": "..."}` naming the spent edge; no new file appears in the recipient's
  `inbox/` or `archive/`; `_edges/peer-a--peer-b.json` `count` stays frozen at 3.
- The sender's transcript typically shows a follow-up message escalating to its
  *master* (never-budgeted channel) reporting the spent budget — confirm this appears
  rather than a retry.
- Confirm the skill guidance holds: do **not** tell the agent to retry on that edge —
  it mechanically cannot. Recovery is either manual relay by the master, or reap +
  respawn the edge with a larger `:<hops>` value.

**Note on topology vs. budget**

- There is no hard recipient allow-list at the transport layer beyond "both sides
  have `meta.json`" (task-agent to task-agent) → budgeted; the "may only talk to
  declared peers" rule is enforced by *persona instruction*, not code. As an extra
  manual check, try instructing an agent to cold-message a third, undeclared but
  live agent: the send is not rejected by name (assuming a resolvable mailbox and an
  unspent/absent edge counter), confirming this is a soft rule — document this
  observation if testing for topology enforcement expectations.

**Debug if edges misbehave**

- Wrong/missing budget: re-check the `--peer name:hops` syntax at spawn — it can only
  be set at spawn time, not modified on a live agent.
- Edge never increments: confirm both sides actually have `meta.json` (i.e. both are
  task-agents, not a master or standing `cld agent`, which are exempt/unbudgeted).
- Send to a reaped peer: refused via `mailbox_reaped()` — confirm
  `/var/cld/mailboxes/<peer>/` is gone and only `_archive/<peer>/` exists.

---

## 4. Check results / completed output

Task agents deliver via a jj/git bookmark, not a `result.json`/`summary.json` file
(that mechanism belongs to `cld run`/`cld chain`, not task agents).

**Steps**

1. Instruct the agent to wrap up (via messenger `send()`): ask it to squash its work
   into its deliverable branch and report back.
2. For a sibling-repo agent, also instruct it to push:
   `jj git push --bookmark <branch> --allow-new`.

**Observe / confirm**

- Own repo (mounted, direct): `jj log -r <deliverable-branch>` and
  `jj diff -r <deliverable-branch>` in the master's repo.
- Sibling repo, pushed: check the pushed branch/MR via GitLab (branch list or MR UI),
  since the master has no filesystem view of the sibling.
- Sibling repo, not pushed: only the agent's self-report is available — treat this as
  unverified, not confirmed.
- Cost/message stats: `cat /var/cld/mailboxes/<name>/state.json` —
  `msg_count`, `cost_usd_total`, `phase`.
- Transcript should show the wrap-up report message to the master:
  `cld task-agent transcript <name>`.

**Debug if results are missing/wrong**

- Nothing committed: recall that Watchman auto-commits every turn into the shared jj
  store regardless of squash — recover via `jj log -r 'heads(all())'` in the target
  repo even if the agent never explicitly wrapped up.
- Push failed with `Host key verification failed`: task-agent containers get SSH
  agent forwarding but ship no `known_hosts` — seed via `ssh-keyscan` before the first
  cross-repo push (known issue, not a bug to chase further).
- Claude produced no reply (agent looks stuck): check transcript for the literal
  fallback string `(no reply produced; last text: ...)` — supervisor synthesizes this
  when a turn produces nothing.
- Mid-turn crash: `cld task-agent logs <name>` shows `claude exited <code>` with
  stdout/stderr; the sender still gets a `failed: <error>` reply instead of a hang.

---

## 5. Reap agents (wrap-up / teardown)

**Steps**

1. Confirm the agent is not mid-turn: `cat /var/cld/mailboxes/<name>/state.json` —
   `phase` should be `idle`, not `processing`.
2. Confirm no other live agent depends on it as a peer: check other agents'
   `meta.json` `peers` maps for `<name>`.
3. `cld task-agent shutdown <name>`
   (or `cld task-agent shutdown --all` to reap the whole fleet).

**Observe / confirm clean teardown**

- `docker ps -a` no longer lists the container; `docker inspect <name>` errors with
  "no such object".
- `/var/cld/mailboxes/<name>/` is gone; `/var/cld/mailboxes/_archive/<name>/` now
  holds the same contents (`state.json`, `meta.json`, `outbox.log`, `archive/`).
- `jj bookmark list` — the session bookmark is forgotten; the deliverable branch
  bookmark still exists (deliverable survives, lifecycle pointer does not).
- `cld task-agent status <name>` reports `Container: gone`, `Mailbox: reaped
  (archived)`.
- `cld task-agent transcript <name>` still works after reap (reads the archive) —
  confirm this as part of the test, since it's easy to assume transcripts vanish with
  the container.

**Debug refusals**

- `refusing to reap <name>: it is mid-turn on '<subject>' ... Wait for it to reply, or
  override with --force.` (waits up to 10s automatically) — either wait, or use
  `--force` (host-only; broker refuses `--force` requests originating from inside a
  master, by design).
- `refusing to reap <name>: it is a live peer of <dependent1>, ...` → reap the
  dependents first, or wait for the pending exchange to land.
- `refusing to reap <name>: its parent master is <owner>, not <this-master>.` → a
  master can only reap its own fleet; confirm via `org.cld.parent-master` label or
  `meta.json.parent`.
- Orphaned mailbox with no container (`cld task-agent status` shows `CONTAINER:
  gone`): clear with `cld task-agent shutdown <name>` — this is a stated recovery
  path, worth deliberately triggering (e.g. `docker rm -f` the container out-of-band)
  to confirm the cleanup command still succeeds.

---

## 6. End-to-end regression pass

Run once as a final smoke test after any change to task-agent code:

1. Spawn two agents, one own-repo and one sibling-repo, with a peer edge between them
   at a small hop budget (e.g. 2).
2. Drive them to exchange messages until the edge is exhausted; confirm the block
   behavior from §3.
3. Wrap up both, confirm deliverables per §4 (own-repo via `jj log`, sibling via
   pushed branch).
4. Reap both via `cld task-agent shutdown --all`; confirm both archived cleanly per
   §5.
5. Confirm `cld task-agent status` shows an empty roster and no orphaned mailboxes
   remain: `ls /var/cld/mailboxes/` should show only `_archive/` and `_edges/`.

---

## 7. GraphQL broker (`cld broker graphql`, `docs/impl-graphql-broker-plan.md`)

Prerequisites: the target repo has `graphql_command` set in `.cld/config.toml`
(binding `0.0.0.0`, see `docs/graphql-mcp.md`), and `GRAPHQL_IMAGE` is built
(`graphqlserver/build.sh` or equivalent).

### 7.1 Start / query / restart-after-edit / stop

**Steps**

1. `start_server` (via the `graphql-tester` MCP, or `cld broker graphql start`
   directly). First call pays for `docker run` + `poetry install` — allow up
   to `GRAPHQL_START_TIMEOUT` (default 300s).
2. `server_status` — expect `running`, with `port`/`endpoint`/`revision`
   populated and `revision` matching the calling session's current `@`.
3. `introspect` then `query { __typename }` against `target="local"` —
   expect a schema summary, then a clean `{"data": {"__typename": "..."}}`.
4. Edit a file the server serves, then `restart_server` — expect the new
   `container` id/`revision` in the follow-up `server_status` (a fresh
   `docker run`, ruling B: no fast-restart path).
5. `stop_server` — expect `server_status` to report `not_started` afterward,
   and `docker ps -a` / `jj bookmark list` to show the `cld_gql_<session>`
   container and `gql-<session>` workspace both gone.

**Observe / confirm success**

- `docker inspect cld_gql_<session>` labels include `org.cld.gql-session` and
  `org.cld.gql-repo`.
- `jj bookmark list` in the repo shows a `gql-<session>` workspace bookmark
  while running, forgotten after `stop`.

### 7.2 Orphan sweep

**Steps**

1. `start_server`, then kill the owning session container out-of-band
   (`docker rm -f <session-container>`) without going through `stop_server`.
2. From a *different* live session in the same repo, call `server_status` or
   `start_server` (both call `sweep_gql_orphans` first).

**Observe / confirm**

- The orphaned `cld_gql_<old-session>` container and its `gql-<old-session>`
  jj workspace are gone after the sweep-triggering call.
- A session container that merely **stopped** (not removed) does *not* get
  its graphql server reaped — `sweep_gql_orphans` keys off container
  existence, not running state (see the comment at
  `broker/cld-broker.sh:sweep_gql_orphans`).

### 7.3 Unknown alias / denied raw URL

**Steps**

1. `query` with `target="nope-not-configured"` — expect a denial naming the
   unknown alias, not a broker crash.
2. `list_endpoints` — confirm it lists only the aliases actually configured
   (`CLD_GRAPHQL_URL_<ALIAS>` in the repo's secrets env file), and returns
   cleanly (empty list, not an error) for a repo with **zero** aliases
   configured — this is M2: `do_graphql_endpoints`'s `grep` must not abort
   the pipeline under `set -euo pipefail` when nothing matches.
3. `query` with `target="http://some-unlisted-host.example/graphql"` (no
   `GRAPHQL_URL_ALLOWLIST` entry) — expect `denied: host '...' is not in
   GRAPHQL_URL_ALLOWLIST ...`.

### 7.4 Missing `graphql_command`

**Steps**

1. In a repo/`.cld/config.toml` with no `graphql_command` set, call
   `start_server`.

**Observe / confirm**

- Denial names the missing `graphql_command`, not a generic broker/docker
  error.

### 7.5 `check_url_allowlisted` URL-parsing cases (C1)

A userinfo component (`user:pass@host`) ahead of the port-strip let a URL
like `http://allowed.example:80@evil.com/` bypass the allowlist entirely —
`allowed.example` matched, but curl actually connected to `evil.com`. Fixed
by stripping userinfo *before* the port (`broker/cld-broker.sh:
check_url_allowlisted`). With `GRAPHQL_URL_ALLOWLIST="allowed.example"`:

| URL | Expected |
|---|---|
| `http://allowed.example/graphql` | allowed |
| `http://evil.com/graphql` | denied (host not in allowlist) |
| `http://evilallowed.example/graphql` | denied (exact match only, no suffix match) |
| `http://allowed.example:80@evil.com/` | denied — resolves to `evil.com`, not `allowed.example` |
| `http://169.254.169.254/latest/meta-data/@allowed.example/` | denied — the `@` is in the *path*, host is still `169.254.169.254` |

Verify directly against the function in isolation (no container needed):

```bash
awk '/^check_url_allowlisted\(\) \{/,/^}/' broker/cld-broker.sh > /tmp/fn.sh
GRAPHQL_URL_ALLOWLIST="allowed.example" bash -c '
  source /tmp/fn.sh
  check_url_allowlisted "http://allowed.example:80@evil.com/"
  echo "rc=$?"'
# expect: denied: host 'evil.com' is not in GRAPHQL_URL_ALLOWLIST ...  /  rc=3
```
