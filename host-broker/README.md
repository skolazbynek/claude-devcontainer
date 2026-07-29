# cld host test broker

The host-side glue that lets a `cld master` container trigger **one** fixed
command -- run the target repo's tests -- without ever seeing the secrets.

A container asks over SSH; a dedicated, single-purpose `sshd` answers with a
forced command (`host-broker.sh`). It serves **any repo that has a running
master** -- no whitelist: it resolves the target repo from the calling master
container's `org.cld.repo-root` label, resolves which change that session is on
(reading the jj store, never touching the working copy), and runs the standalone
[`runtests`](../runtests/) container against it, mounting the repo's raw `.env`
into that ephemeral runner. `claude` only ever sees the streamed test output.
See `../docs/design-host-test-running.md` for the full design.

```
container: host-run -k login -x tests/
   └─ssh (restricted key)─▶ dedicated sshd ─ForceCommand▶ host-broker.sh
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
| `host-broker.sh` | the `ForceCommand` target; the only thing the key can run |
| `host-broker.conf.sample` | broker-wide config (image, PATH); copy to `/etc/cld/host-broker.conf` |
| `sshd_cld_broker.conf` | sample config for the dedicated `sshd` instance |
| `keygen.sh` | generate the broker keypair, host key, and `authorized_keys` |
| `brokerctl.sh` | operate the sshd: `start` / `restart` / `shutdown` / `status` / `logs` |

## Operating

After first-time setup, symlink `brokerctl.sh` onto your PATH and drive the
daemon with it — it starts sshd detached, tracks it by PID file, and logs to a
file, so there are no sshd flags to remember:

```
brokerctl start | restart | shutdown | status | logs [N]
```

It reads `$CLD_BROKER_DIR` (default `~/.cld/broker`). `restart` is the one to run
after editing the broker config or rebuilding the `runtests` image.

## Setup (once, host-side)

Assumes the `runtests` image is built (`../runtests/build.sh`) and the docker
socket is **not** exposed to `cld master` (that is what keeps `host-run` as
claude's only host channel).

1. **Keys.** `./keygen.sh /etc/cld` (or any dir), then note the printed
   `known_hosts` line.
2. **Config.** Copy `host-broker.conf.sample` to `/etc/cld/host-broker.conf` and
   set `RUNTESTS_IMAGE` + `PATH`. There is nothing per-repo to set -- the repo
   and its secrets path are resolved per request.
3. **Broker script.** Install `host-broker.sh` at `/opt/cld/host-broker.sh`
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
define a function in `host-broker.sh` — that's the whole change:

```bash
action_lint() {
    exec docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
        -v "$REPO:/repo" -e "REVISION=$REV" "${LINT_IMAGE:-runlint:latest}" "$@"
}
```

It receives the decoded argv as `"$@"` and the shared context the dispatcher
already prepared: `$REPO` (resolved from the session label), `$REV` (the
session's current change), `$SECRETS_ENV_FILE` (may not exist),
`$PROJECT_SUBDIR`, and `$RUNTESTS_IMAGE`. Call it from a container with
`host-run --action lint <args>` (no `--action` ⇒ `run-tests`). An action name
that has no matching function is denied, so enabling/disabling is just
defining/removing the function.

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
