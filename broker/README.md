# cld host broker

The host-side glue that lets a `cld master` container trigger a fixed set of
host-side actions -- run the target repo's tests, enumerate cld containers,
launch/manage sibling agents -- without ever seeing the secrets.

A container asks over SSH; a dedicated, single-purpose `sshd` answers with a
forced command (`cld-broker.sh`). It serves **any repo that has a running
master** -- no whitelist: it resolves the target repo from the calling master
container's `org.cld.repo-root` label, resolves which change that session is on
(reading the jj store, never touching the working copy), and for `run-tests`
runs the standalone [`runtests`](../runtests/) container against it, mounting
the repo's raw `.env` into that ephemeral runner. `claude` only ever sees the
streamed output. See `../docs/design-cld-broker.md` for the full design.

```
container: cld broker run-tests -k login -x tests/
   └─ssh (restricted key)─▶ dedicated sshd ─ForceCommand▶ cld-broker.sh
        ├─ validate: action=run-tests, session=cld_master_…
        ├─ REPO = docker inspect <session> -> org.cld.repo-root label
        ├─ REV  = jj -R <repo> log -r <session>     (store-reading only)
        ├─ .env = <repo>/.env  (or under pyproject_dir from <repo>/.cld/config.toml)
        └─ docker run --rm runtests -e REVISION=REV -v repo -v .env -- -k login -x tests/
   ◀───────────────── stdout / stderr / exit code ─────────────────┘
```

## Files

| File | Role |
|---|---|
| `cld-broker.sh` | the `ForceCommand` target; the only thing the key can run |
| `broker.conf.sample` | broker-wide config (image, PATH); copy to `/etc/cld/broker.conf` |
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
   request.
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
ssh ... -- "run-tests cld_master_nope $payload"     # -> denied (no such master)

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
prepared: `$session` (validated master session id) and `$REPO` (resolved from
the session label). Per-action context is resolved lazily inside the action --
`run-tests` calls `resolve_test_context` for `$REV` / `$SECRETS_ENV_FILE` /
`$PROJECT_SUBDIR` -- so read-only actions don't depend on the session bookmark
being resolvable. Call it from a container with `cld broker lint <args>`
(action name is the first argument; `run-tests` needs no special-casing). An
action name that has no matching function is denied, so enabling/disabling is
just defining/removing the function.

**Built-in actions:** `run-tests` (pytest in the `runtests` container),
`list-containers` (read-only cld-container enumeration for the messenger /
`cld agent status`), `agent` (`<target> <op>` -- launch/manage a sibling
`cld agent` on the host) and `task-agent` (`<target> <op>` -- the `cld task-agent`
lifecycle verbs). Both launcher actions validate `<target>` against the master's
host-set `org.cld.repo-root` + `org.cld.targets` labels through the shared
`validate_target`.

`task-agent` is the only action that creates a container with a caller-chosen file
mounted inside it, so it also polices its argv: `--force` is denied (overriding a
reap-readiness refusal stays a human act), a caller-supplied `--parent` is denied and
the validated `$session` appended instead, and `start`'s persona positional must be a
bare name -- a path there would let a container read any host file you can.

## Security notes

- **Scoped, not dynamic.** The action set and `ForceCommand` are fixed host-side.
  The caller controls only the action (must map to a defined function), a
  validated session id, and the argv. The target repo is taken from the calling
  master's host-set label, not caller input -- so the caller can only reach a
  repo that already has a master, never an arbitrary host path.
- **No injection.** argv arrives base64(NUL-joined) and is decoded into an argv
  array, never `eval`'d -- it can only become arguments to the action's command.
- **Blast radius.** A leaked broker key unlocks the defined actions for any repo
  that has a running master (no per-master key isolation in this design).
- **Network scope.** Bind the instance to the docker bridge gateway so only
  containers on the bridge can reach it.
