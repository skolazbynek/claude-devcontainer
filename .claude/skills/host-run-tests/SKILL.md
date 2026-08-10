---
name: host-run-tests
description: >
  Run the target repo's test suite (pytest) via the host test broker's
  `host-run` wrapper instead of raw ssh, mysql, or docker commands. Use this
  whenever you need to run tests inside a `cld master` container and secrets
  (DB/Redis credentials etc.) are not otherwise available in-container. Invoke
  when the user asks to run tests, run pytest, or check whether tests pass,
  and you're running inside a `cld master` session.
user-invocable: true
---

# Run tests via the host test broker (`host-run`)

Some repos keep test secrets (MySQL/Redis/etc. credentials) entirely on the
host and never mount them into `cld master`. For those repos, the master
container ships a `host-run` wrapper that triggers a fixed, host-side action
over a restricted SSH connection: it runs the repo's tests in an isolated
`runtests` container against your current change, and streams back only the
output. You never see or need the raw secrets, and you never construct the
SSH call yourself.

**Do not hand-roll `ssh` to the broker.** The wrapper already encodes the
session name and base64-packs your argv exactly the way the broker expects;
a manually-built `ssh ... "run-tests ..."` command will not match and will be
denied, and it bypasses the safety property that arbitrary args can only ever
become pytest arguments.

## Step 1: Confirm you're inside a master with the broker wired

This only exists for `cld master` sessions (never `cld agent`, `cld run`, or
bare `cld`). Check for the wrapper:

```bash
command -v host-run || command -v /tmp/bin/host-run
```

If neither is found, this repo's master isn't configured with a host test
broker (`host_broker_key` unset in its `.cld/config.toml`). Don't try to set
one up yourself -- that's host-side infrastructure the user configures
out-of-band. Don't fall back to running the plain test command in-container
either: without the broker there is no other way to get real DB/Redis/etc.
secrets into this container, and a suite run without them is not a
meaningful test result -- it'll look like it ran while actually testing
nothing real (or erroring on missing config in a way that's easy to
misread as a code bug). Tell the user the broker isn't configured for this
repo and stop.

If `command -v host-run` finds nothing but `/tmp/bin/host-run` exists, use
the full path for the rest of this skill -- PATH may not include `/tmp/bin`
in every attached shell.

## Step 2: Run the tests

```bash
host-run <pytest args...>
```

Examples:

```bash
host-run                       # full suite
host-run -k login -x tests/    # filtered, stop on first failure
host-run tests/unit/test_foo.py
```

There's no need to pass `--action`; it defaults to `run-tests`. (A repo's
broker admin may have defined extra actions -- `host-run --action <name>
<args>` -- but `run-tests` is the only one that exists unless you were told
otherwise.)

The command runs synchronously and streams pytest's own stdout/stderr; its
exit code is pytest's exit code (0 = passed, 1 = failures, etc.) in the
normal case.

## Step 3: Interpret broker-level failures

These come from the broker itself (printed to stderr, distinct from pytest
failures):

| Message | Meaning | What to do |
|---|---|---|
| `denied: bad action` / `denied: unknown action '...'` | Malformed action name | Shouldn't happen via the wrapper; report it, don't retry with a hand-built command |
| `denied: bad session id` | Session name doesn't look like `cld_master_*` | Shouldn't happen from inside a real master; report it |
| `no master/repo for session ...` | The host broker couldn't find a running container matching this session with the expected repo label | Host-side problem (broker or container state) -- tell the user, don't attempt an ssh workaround |
| ssh-level connection/auth errors | The broker's sshd is unreachable or misconfigured | Host-side problem -- tell the user; this is not something fixable from inside the container |

## Why this exists

Full design: the repo's own `docs/design-host-test-running.md` and
`host-broker/README.md` (only present when working in the `cld` tool's own
repo, not in target repos). The short version: `jj`/`git`'s multi-workspace
model isolates the test run from your host `@`, and the raw `.env` is
mounted only into the ephemeral, `claude`-unreachable `runtests` container --
`host-run` is the *only* host-facing channel, and it can only ever run the
fixed, pre-defined action with pytest-shaped arguments.
