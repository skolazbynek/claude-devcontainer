# cld host broker

The host-side glue that lets a `cld master`, `cld agent`, or `cld task-agent`
container trigger a fixed set of host-side actions -- run the target repo's
tests, enumerate cld containers, launch/manage sibling agents -- without ever
seeing the secrets. `agent`/`task-agent` launcher actions stay effectively
master-only regardless (they gate on a label only master carries -- see
`../docs/design-cld-broker.md` §15); `run-tests`, `list-containers`, and
`graphql` are the actions all three roles can actually reach -- `graphql`'s
role gate, like `run-tests`', is prompt-based only (persona/skill text), not
mechanical (see `../docs/design-cld-broker.md` §16). Agents and task-agents
are instructed (persona prompts, not enforced here) to only run `run-tests`
or `graphql` with their master's explicit per-invocation authorization.

A container asks over SSH; a dedicated, single-purpose `sshd` answers with a
forced command (`cld-broker.sh`). It serves **any repo that has a running
master, agent, or task-agent** -- no whitelist: it resolves the target repo
from the calling container's `org.cld.repo-root` label, resolves which change
that session is on (reading the jj store, never touching the working copy),
and for `run-tests` runs the standalone [`runtests`](../runtests/) container
against it, mounting the repo's raw `.env` into that ephemeral runner.
`claude` only ever sees the streamed output. See `../docs/design-cld-broker.md`
for the full design.

```
container: cld broker run-tests -k login -x tests/
   └─ssh (restricted key)─▶ dedicated sshd ─ForceCommand▶ cld-broker.sh
        ├─ validate: action=run-tests, session=cld_master_… or cld_agent_…
        ├─ REPO = docker inspect <session> -> org.cld.repo-root label
        ├─ REV  = jj -R <repo> --ignore-working-copy log -r "<session>@"   (workspace tip, store-reading only)
        ├─ .env = <repo>/.env  (or under pyproject_dir from <repo>/.cld/config.toml)
        └─ docker run --rm runtests -e REVISION=REV -v repo -v .env -- -k login -x tests/
   ◀───────────────── stdout / stderr / exit code ─────────────────┘
```

## Files

| File | Role |
|---|---|
| `cld-broker.sh` | the `ForceCommand` target; the only thing the key can run |
| `broker.conf.sample` | broker-wide config (image, PATH, ssh-agent socket); copy to `/etc/cld/broker.conf` |
| `sshd_cld_broker.conf` | sample config for the dedicated `sshd` instance |
| `keygen.sh` | generate the broker keypair, host key, and `authorized_keys` |
| `cld-cld-brokerctl.sh` | operate the sshd: `start` / `restart` / `shutdown` / `status` / `logs` |

## Operating

After first-time setup, symlink `cld-cld-brokerctl.sh` onto your PATH and drive the
daemon with it — it starts sshd detached, tracks it by PID file, and logs to a
file, so there are no sshd flags to remember:

```
cld-brokerctl start | restart | shutdown | status | logs [N]
```

It reads `$CLD_BROKER_DIR` (default `~/.cld/broker`). `restart` is the one to run
after editing the broker config or rebuilding the `runtests` image.

## Setup (once, host-side)

Assumes the `runtests` image is built (`../runtests/build.sh`) and the docker
socket is **not** exposed to `cld master` (that is what keeps the broker as
claude's only host channel).

1. **Keys.** `./keygen.sh /etc/cld` (or any dir), then note the printed
   `known_hosts` line.
2. **Config.** Copy `broker.conf.sample` to `/etc/cld/broker.conf` and
   set `RUNTESTS_IMAGE` + `PATH`. `PATH` must include both `docker` and `cld`
   (the `agent` action runs host-side `cld agent` for sibling launches). There
   is nothing per-repo to set -- the repo and its secrets path are resolved per
   request. Set `SSH_AUTH_SOCK` too if the agents and task-agents launched
   through the broker should be able to push (see the sample for why a
   login-shell agent's socket path will not do).
3. **Broker script.** Install `cld-broker.sh` at `/opt/cld/cld-broker.sh`
   (path referenced by `ForceCommand`), `chmod +x`.
4. **sshd.** Edit `sshd_cld_broker.conf` (`AllowUsers`, `ListenAddress` for your
   bridge gateway, key paths), then launch the dedicated instance:
   ```
   /usr/sbin/sshd -f /etc/cld/sshd_cld_broker.conf -D
   ```
   It runs the forced command as `AllowUsers`, so that user must own the target
   repos, read their `.env`s, and have docker access.

## Verify (no container needed)

The repo is resolved from a running master's label, so the smoke test needs a
container named like the session carrying that label (a real `cld master`, or a
throwaway):

```
REPO=/abs/path/to/some/repo
docker run -d --name cld_master_demo --label org.cld.repo-root="$REPO" alpine sleep 300
jj -R "$REPO" bookmark create cld_master_demo -r @

payload=$(printf '%s\0' -k some_test -x | base64 -w0)
ssh -i /etc/cld/broker_key \
    -o UserKnownHostsFile=<known_hosts> -o StrictHostKeyChecking=yes \
    -p 2222 <user>@172.17.0.1 -- "run-tests cld_master_demo $payload"

# rejected: anything but a defined action, or a malformed/unknown session
ssh ... -- "deploy cld_master_demo $payload"        # -> denied (bad action)
ssh ... -- "run-tests ../../etc/passwd $payload"    # -> denied (bad session)
ssh ... -- "run-tests cld_master_nope $payload"     # -> denied (no such container)

docker rm -f cld_master_demo; jj -R "$REPO" bookmark delete cld_master_demo
```

## Adding an action

The broker dispatches `$SSH_ORIGINAL_COMMAND` (`<action> <session> <base64-argv>`)
to a shell function named `action_<name>` (hyphens → underscores). To add one,
define a function in `cld-broker.sh` — that's the whole change:

```bash
action_lint() {
    exec docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
        -v "$REPO:/repo" -e "REVISION=$REV" "${LINT_IMAGE:-runlint:latest}" "$@"
}
```

It receives the decoded argv as `"$@"` and the shared context the dispatcher
prepared: `$session` (validated session id -- master or agent/task-agent) and
`$REPO` (resolved from the session label). Per-action context is resolved
lazily inside the action --
`run-tests` calls `resolve_test_context` for `$REV` / `$SECRETS_ENV_FILE` /
`$PROJECT_SUBDIR` -- so read-only actions don't depend on the session bookmark
being resolvable. Call it from a container with `cld broker lint <args>`
(action name is the first argument; `run-tests` needs no special-casing). An
action name that has no matching function is denied, so enabling/disabling is
just defining/removing the function.

**Built-in actions:** `run-tests` (pytest in the `runtests` container),
`list-containers` (read-only cld-container enumeration for the messenger /
`cld agent status`), `agent` (`<target> <op>` -- launch/manage a sibling
`cld agent` on the host), `task-agent` (`<target> <op>` -- the `cld task-agent`
lifecycle verbs), and `graphql` (`<op> [args...]` -- start/stop/restart/status/
logs for the calling session's own GraphQL server, plus query/introspect/
endpoints against it or a credentialed alias; see
`../docs/impl-graphql-broker-plan.md` and `../docs/graphql-mcp.md`). Both
launcher actions validate `<target>` against the master's host-set
`org.cld.repo-root` + `org.cld.targets` labels through the shared
`validate_target`; `graphql`, like `run-tests`, instead operates on the
calling session's own repo (`$session`'s `org.cld.repo-root` label), since it
has no separate target to launch.

Both `run-tests`' `resolve_test_context` and `graphql`'s
`resolve_graphql_context` resolve the session's revision by calling `jj`
unconditionally -- neither action works against a git-backed repo (jj
resolution `exit 3`s). `graphql`'s read-only ops (`status`/`logs`/`stop`/
`endpoints`) avoid this by not resolving a revision at all; only `start` (and
by extension `restart`) needs it.

`task-agent` is the only action that creates a container with a caller-chosen file
mounted inside it, so it also polices its argv: `--force` is denied (overriding a
reap-readiness refusal stays a human act), a caller-supplied `--parent` is denied and
the validated `$session` appended instead -- both in either spelling, `--opt` and
`--opt=value` -- and `start`'s persona positional must be a bare name (an `@ref`) -- a
path there would let a container read any host file you can. The positional check
knows which `start` options take a *separate* value (`-n`/`-p`/`--branch`/`-m`/`-r`/
`--peer`) rather than assuming every `-*` token consumes the next one, which would
let the token after an `--opt=value` slip by unchecked.

## Security notes

- **Scoped, not dynamic.** The action set and `ForceCommand` are fixed host-side.
  The caller controls only the action (must map to a defined function), a
  validated session id, and the argv. The target repo is taken from the calling
  container's host-set label, not caller input -- so the caller can only reach a
  repo that already has a master, agent, or task-agent, never an arbitrary host path.
- **No injection.** argv arrives base64(NUL-joined) and is decoded into an argv
  array, never `eval`'d -- it can only become arguments to the action's command.
- **Blast radius.** A leaked broker key unlocks `run-tests`/`list-containers` for
  any repo that has a running master, agent, or task-agent (no per-container key
  isolation in this design) -- more surface than before this key reached
  agent/task-agent containers too. `agent`/`task-agent` (the launcher actions)
  stay reachable only from a real master, gated by a label the key alone can't
  forge. With
  `SSH_AUTH_SOCK` configured that includes launching an agent that holds your
  forwarded ssh-agent, so the key buys signing inside a container it spawns and
  not only `run-tests`. Leave it unset to keep every broker-launched agent
  structurally unable to push.
- **Network scope.** Bind the instance to the docker bridge gateway so only
  containers on the bridge can reach it.
