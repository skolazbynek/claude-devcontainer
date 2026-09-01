# `otelctl.sh doctor` -- end-to-end health check for the otel pipeline

> **Status: design only.** Requested by `docs/review-otel-public-release.md`
> § "To-do" **T4**. Nothing here is implemented. Scope is the `otel/` folder
> only, and the folder's standing rule holds: **no dependency on cld** beyond
> defaulting the state directory to `~/.cld/otel`.

## The problem

The only signal a user has today that telemetry is flowing is whether a file
turns up under `$CLD_OTEL_DIR/stats/`. If it doesn't, there is nothing to
look at: no error, no partial state, no diagnostic. `otelctl.sh status`
(`otel/otelctl.sh:107`) answers two much weaker questions -- is a container
named `cld-otel-collector` in state `Running`, and is the aggregator's pid
alive -- both of which can be `running` while no metric will ever arrive.

The chain has seven links that can break, and `status` covers none of them:

```
shell env  ->  Claude Code  ->  :4318 otlp receiver  ->  file exporter
              ->  data/raw-metrics.jsonl  ->  aggregate.py --watch
              ->  stats/<service.name>/<session>.json
```

`doctor` walks that chain and names the first link that is broken.

## Scope and non-goals

- **Diagnose, never repair.** `doctor` starts nothing, stops nothing, and
  never restarts the collector. Its only writes are its own synthetic check
  artifacts (below), and it removes those. A user who wants a fix runs the
  command `doctor` tells them to run.
- **No new dependency.** bash + `docker` + `python3` only -- exactly what
  `otelctl.sh` already requires (`otel/otelctl.sh:24` resolves `$PYTHON`,
  `aggregate.py` is stdlib-only Python 3). No `curl`, no `jq`, no `nc`.
- **No new shipped file.** `doctor` lives in `otelctl.sh`. The folder ships
  three artifacts today; T1 (standalone install) gets easier, not harder, if
  that stays true.
- **Local pipeline only.** No `--endpoint` flag for checking a shared/remote
  collector. Two of the five checks need local access to
  `data/raw-metrics.jsonl`, so a remote target could only ever be
  half-checked. Revisit if review item #2 (team-wide collector) is ever
  built.
- **No `--json` output.** The internal line protocol (below) makes it a
  small addition later; nobody has asked for it.

## Decisions

### D1. Python 3 for the HTTP work, not `curl` -- confirmed

The brief asked this to be confirmed rather than assumed. It holds, for a
stronger reason than "curl might be missing":

- Python 3 is **already a hard requirement** of the running pipeline.
  `otelctl.sh start` launches `aggregate.py` with `$PYTHON`
  (`otel/otelctl.sh:74`); if python3 is absent, the pipeline cannot run at
  all, so `doctor` requiring it adds nothing. `curl` and `jq` are required
  by nothing in `otel/` today, and adding either would make the *diagnostic*
  heavier than the thing it diagnoses -- the exact inversion the review
  criticises in item #1.
- One `python3` process does the whole probe: build the OTLP JSON, POST it
  with a timeout, parse the response body's `partialSuccess`, TCP-connect
  probe the port, poll the raw file, poll for the stats file, and compare
  parsed JSON values. The `curl` equivalent needs `jq` for the payload and
  the response, plus shell arithmetic for the polls.
- `doctor` must use the **same `$PYTHON`** `otelctl.sh` already resolves, so
  an override (`PYTHON=python3.12 ./otelctl.sh ...`) applies consistently to
  both the watcher and the check.

`urllib.request` covers everything needed; it sets `Content-Length` itself,
which the Claude Code docs note is expected for `http/json`.

### D2. Observe the round-trip by watching the **live** watcher; use an
isolated one-shot replay only as the fault localizer

The brief offered two options: run a one-shot `aggregate.py` pass as part of
the check, or poll for the existing watcher. It also stated the one-shot pass
is "idempotent and offset-tracked, so this is safe even if the real watcher
is also running". **That is not true, and it is the single most important
finding behind this design.**

Verified experimentally against the real `otel/aggregate.py`: a one-shot pass
sharing an output directory with a live `--watch` process **double-counts
every line it consumes**, and the inflated numbers land on *real* sessions,
not just the synthetic one.

Reproduction (real `aggregate.py`, temp dirs, one export line worth 100 input
tokens):

1. start `aggregate.py --watch --input R --output-dir S`
2. append one export line to `R`
3. immediately run `aggregate.py --input R --output-dir S` (no `--watch`)
4. wait ~2s for the watcher's next poll

Result: `tokens.input == 200`, not 100.

Cause: the watcher reads the offset file **once, at startup**
(`otel/aggregate.py:315-326`) and thereafter tracks position with its own
file handle, rewriting the offset after each line
(`otel/aggregate.py:345`). Its read loop sleeps 1s at EOF
(`otel/aggregate.py:335`). A one-shot pass that runs inside that 1s window
seeks to the persisted offset, processes the pending line, and advances the
offset -- and the watcher, which never re-reads the offset file, processes
the same line again on its next poll. Offset tracking protects
*sequential* re-runs, not *concurrent* ones.

So the design must never point a one-shot pass at the real `stats/`
directory. That leaves:

| | Approach | Verdict |
|---|---|---|
| **A** | One-shot `aggregate.py` pass against the real `--output-dir` | **Rejected.** Silently corrupts real session totals whenever the watcher is up, which is the normal case. |
| **B** | Poll `stats/` for the synthetic session file, up to a timeout | **Chosen as the primary observation.** Verifies the actual running pipeline, including the watcher -- the strongest possible statement, and it is what the user cares about. Only weakness: it cannot distinguish "watcher stalled" from "aggregate.py can't parse this". |
| **C** | One-shot pass with **both** `--input` and `--output-dir` redirected into a throwaway temp dir | **Chosen as the fallback/discriminator.** Zero interaction with real state (the offset file lives next to the output dir, `otel/aggregate.py:311`, so a temp output dir gets a private offset). Fast: the temp input holds only the one extracted line. Proves aggregate.py's parse/bucket/write path, but says nothing about the running watcher. |
| **D** | Real raw file as `--input`, temp `--output-dir` | Rejected. No offset file in the temp dir means replaying the whole history -- unbounded (review item #7) and pointlessly slow. |

**B first, C only when B does not pass.** The happy path then has the fewest
moving parts, and the unhappy path gets a precise verdict: if B fails and C
passes, the aggregator code is fine and the running watcher is the fault; if
both fail, `aggregate.py` itself rejected the line.

### D3. Delete the synthetic artifacts, and pre-delete them too

- **Stats file: deleted by default**, before *and* after the check.
  - Before, because `aggregate.py` loads any pre-existing file at the target
    path and adds to it (`otel/aggregate.py:202-205`) -- a leftover from an
    earlier run would make the sentinel comparison fail confusingly and its
    stale `session_id` would survive.
  - After, because it is not usage data. Review item #5 wants a
    cross-session rollup; a fake session left in `stats/` would silently
    join every future total.
  - `--keep-check-artifacts` keeps it for debugging, and the report says so.
- **The `raw-metrics.jsonl` line cannot be cleaned, and this must be stated
  in the report and the README.** The file is deliberately append-only
  (`otel/otel-collector-config.yaml:21-29`); rewriting it desyncs the
  aggregator's byte offset. Each `doctor` run therefore leaves one
  permanent ~1 KB line in the raw stream. Consequence to document: a user
  who later deletes `stats/` and re-runs `aggregate.py` from scratch will see
  the `otelctl-doctor-check` sessions reappear. `--no-synthetic` skips
  check 5 entirely for anyone who wants a read-only diagnosis.
- **Nothing else is ever touched**: not the offset file, not other stats, not
  the collector.
- Forward note for whoever builds the rollup (review item #5): skip
  `service.name == "otelctl-doctor-check"`.

### D4. Run every check, skip only what has become meaningless

Stopping at the first failure hides the information that makes the report
useful -- "collector down" *and* "env vars unset" is a different fix from
either alone. So: run all checks, in chain order, streaming each result as it
completes.

A check whose precondition failed reports `skip` **with the reason**, never
`fail` -- a skip is "not knowable", and conflating it with a real failure
inflates the failure count and misdirects the user.

Dependencies are a small graph, not a line. In particular **check 5 depends
on check 3 (the port answers), not on Docker**: a collector run some other
way (systemd, a hand-rolled `docker run`, a `otelcol` binary) still passes
3-5, and the report should stay green with a note rather than pretending it
is broken.

### D5. Four states, and the shell-env check is advisory unless it
contradicts itself

States: `ok`, `warn`, `fail`, `skip`. Exit **0** if no `fail` (warnings do
not fail the run), **1** if any `fail`, **2** on usage error -- matching the
existing dispatch (`otel/otelctl.sh:130`).

The env-var check (check 4) needs a rule of its own, because the shell
running `doctor` is very often *not* the shell that will run `claude`. A
child process sees only what its parent `export`ed, and for cld-managed
sessions the telemetry env is injected inside the container and is *expected*
to be absent on the host. So:

- **No telemetry variable set at all** -> one `warn`, plus the visibility
  note: *"`doctor` can only see variables this shell `export`ed. If your
  sessions are launched by a wrapper (cld, a systemd unit, an IDE) their env
  lives there, not here -- this is not necessarily a problem."*
- **Any telemetry variable set** -> the shell is claiming to be configured,
  so validate strictly and report contradictions as `fail`. A half-configured
  shell is broken with certainty, unlike an empty one.

This keeps a healthy pipeline from being reported red because the user ran
`doctor` from a different terminal, while still catching the typo'd endpoint
that motivated the ticket.

### D6. Where the code lives, and the bash/python seam

`doctor()` is a bash function in `otelctl.sh` alongside `status()` and `logs()`.
Docker inspection, formatting and the summary are natural in bash; the
network probe and round-trip are one inline `$PYTHON - <<'PY'` block.

Contract between the two halves: the python block writes one result per line
to stdout as `state|label|message|detail` (detail optional, may be empty),
and bash renders it through the same `report()` function its own checks use.
Config goes in as exported `DOCTOR_*` env vars, not argv -- readable inside a
heredoc.

Implementation footgun to respect: read the python block's output with
**process substitution**, `while IFS='|' read -r ... done < <($PYTHON - <<'PY'
... PY)`, not a pipe. A pipe puts `report()` in a subshell and the
pass/fail counters it maintains are lost.

## The checks

Config is read exactly as the rest of the script reads it -- `$CLD_OTEL_DIR`
(default `~/.cld/otel`), `$CLD_OTEL_PORT` (default 4318), `$CLD_OTEL_IMAGE`,
`$PYTHON`, container name `cld-otel-collector`
(`otel/otelctl.sh:20-26`). `doctor` must not invent its own defaults.

### Check 0 -- preflight

| Item | `ok` | `fail` / `warn` |
|---|---|---|
| `python3` | `$PYTHON` runs and reports a 3.x version | `fail`: not found -> checks 3 and 5 skip; the *pipeline itself* cannot run either, so say that. |
| state dir | `$CLD_OTEL_DIR` exists and is writable | `fail` if it exists and is not writable; `warn` if absent (nothing has ever started here -- suggest `otelctl.sh start`). |
| running inside a container | -- | `warn` if `/.dockerenv` exists: `doctor` is meant for the host; from inside a container `127.0.0.1:$PORT` is not the collector, so checks 3 and 5 will misreport. Say it once, loudly, and continue. |

### Check 1 -- Docker

1. **CLI installed**: `command -v docker`. `fail` -> checks 2 skip.
2. **Daemon reachable**: `docker info --format '{{.ServerVersion}}'` with a
   short timeout. `fail` carries docker's own first error line (the
   "Cannot connect to the Docker daemon" text is already the actionable
   message). -> check 2 skips.

`ok` prints the server version.

### Check 2 -- collector container

1. **Running**: reuse `collector_running()` (`otel/otelctl.sh:30`).
   - `fail` if the container does not exist -> "never started here; run
     `otelctl.sh start`".
   - `fail` if it exists but is not running -> report `.State.Status`,
     `.State.ExitCode` and `.State.Error` from one `docker inspect`, and
     point at `otelctl.sh logs collector`.
   - `ok` prints image and uptime; **`warn` if `.RestartCount` > 0** (a
     crash-looping collector looks "running" between restarts).
2. **Data mount matches the current `$CLD_OTEL_DIR`** [`fail` on mismatch].
   From `docker inspect` `.Mounts`, the source bound at `/data` must be
   `$CLD_OTEL_DIR/data` (`otel/otelctl.sh:51`). If the user changed
   `CLD_OTEL_DIR` since starting the collector, `doctor` and the aggregator
   are looking at a file the collector is not writing -- and check 5 would
   otherwise fail with a baffling message. Name both paths.
3. **Config mount and drift** [`warn`]. The source bound at
   `/etc/otelcol-contrib/config.yaml` should be
   `$HERE/otel-collector-config.yaml`; if it is a different path, say which.
   If that file's mtime is newer than the container's `.State.StartedAt`, the
   running collector is using a stale config -> "edited since start; run
   `otelctl.sh restart` to apply".
4. **Log scan** [`warn`]. `docker logs --tail 200` filtered for `error`,
   `permission denied`, `address already in use`. Print the match count and
   the most recent matching line, and point at `otelctl.sh logs collector`.
   This is what surfaces the bind-mount uid/gid trap the script's own comment
   warns about (`otel/otelctl.sh:44-45`) -- a collector that starts fine and
   then cannot write `raw-metrics.jsonl`.

### Check 3 -- the port is bound and the receiver answers

Three escalating probes; the third is the one that matters.

1. **Published ports** [info, folded into the `ok` message]. From `docker
   inspect` `.NetworkSettings.Ports`, list the host bindings for `4318/tcp`.
   Expect the bridge gateway IP and `127.0.0.1`, both on `$PORT`
   (`otel/otelctl.sh:48-49`).
2. **TCP connect** to `127.0.0.1:$PORT`, 2s timeout. `fail` = "nothing
   listening"; if check 2 said the container is running, add "the container
   is up but its port is not published -- was it started with a different
   `CLD_OTEL_PORT`?".
3. **HTTP-level liveness**: POST a deliberately malformed body (`{`) to
   `http://127.0.0.1:$PORT/v1/metrics` with `Content-Type: application/json`,
   3s timeout.
   - any **4xx** (the otlp receiver rejects unparseable JSON with 400) or
     **200** -> `ok`, "OTLP receiver answering".
   - **404** -> `fail`, "something is answering on $PORT but `/v1/metrics`
     is not there -- is that really an OTel collector?"
   - **5xx**, connection reset, or timeout -> `fail`, "port is bound but the
     receiver did not answer" -- the case the brief calls out, where
     docker-proxy holds the host port while the process inside is dead or
     restarting.

   A malformed body is used deliberately in preference to an empty-but-valid
   payload: it proves the OTLP receiver is parsing, and it cannot add a junk
   line to `raw-metrics.jsonl`.
4. **Bridge-gateway drift** [`warn`]. Compare the published gateway IP from
   (1) against the *current* `docker network inspect bridge --format
   '{{(index .IPAM.Config 0).Gateway}}'` (the same expression `collector_start`
   uses, `otel/otelctl.sh:42`). A mismatch -- the docker daemon restarted and
   renumbered the bridge -- means host-side sessions still work over
   loopback while every containerised session pointed at
   `host.docker.internal` silently fails. Verdict: `warn`, "run `otelctl.sh
   restart`". This is invisible to every other check, since `doctor` itself
   probes loopback.

### Check 4 -- telemetry env vars in *this* shell

Advisory or strict per **D5**. Evaluate the Claude Code documented semantics
(https://code.claude.com/docs/en/monitoring-usage.md), honouring per-signal
precedence: `OTEL_EXPORTER_OTLP_METRICS_*` overrides `OTEL_EXPORTER_OTLP_*`
for metrics.

| Variable | Rule | On violation |
|---|---|---|
| `CLAUDE_CODE_ENABLE_TELEMETRY` | must be `1` | `fail` if unset while other OTEL vars are set; `warn` if set to some other truthy string (only `1` is documented) |
| `OTEL_METRICS_EXPORTER` | comma list must contain `otlp` | `fail` -- `console`/`prometheus`/`none` alone never reach a collector |
| effective protocol (`OTEL_EXPORTER_OTLP_METRICS_PROTOCOL` else `OTEL_EXPORTER_OTLP_PROTOCOL`) | `http/json` or `http/protobuf` | `fail` if unset: **Claude Code documents no default protocol**, so nothing is exported. `fail` on `grpc`: the collector only opens the HTTP receiver on 4318 (`otel-collector-config.yaml:10-14`) |
| effective endpoint (`..._METRICS_ENDPOINT` else `..._ENDPOINT`) | scheme `http`; port == `$PORT`; host resolvable; host is one the collector binds | `fail` per specific cause, and say which: `https` (no TLS configured), port mismatch (print both), unresolvable host (the typo'd-endpoint case -- `getaddrinfo` failure quoted), or a host that is neither loopback / `host.docker.internal` / the bridge gateway |
| `OTEL_RESOURCE_ATTRIBUTES` | should contain `service.name=<name>`; must contain no spaces | `warn` if `service.name` is missing: `aggregate.py` keys output on it and **skips any export lacking it** (`otel/aggregate.py:281-283`), so at best every session merges under whatever default the SDK picks. `fail` on embedded spaces (documented as invalid) |
| `OTEL_METRICS_INCLUDE_SESSION_ID` | not `false` | `warn`: with `session.id` gone, every session collapses into one `unknown-session.json` (`otel/aggregate.py:92`) |
| `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` | not `cumulative` | `fail`: `aggregate.py` sums every point as a delta (`otel/aggregate.py:208-216`), so cumulative export inflates totals without bound |
| `OTEL_METRIC_EXPORT_INTERVAL` | -- | info: print the effective value (default 60000 ms) and "a new session's first stats file can take that long to appear" -- the single most common false alarm |
| `OTEL_EXPORTER_OTLP_HEADERS` | -- | info if set; this collector needs no auth |

Two notes worth printing rather than hiding: `http/protobuf` is fine even
though README/QUICK-START only mention `http/json` -- the file exporter
re-marshals to JSON regardless of receive encoding
(`otel-collector-config.yaml:21-23`); and check 4 never gates check 5.

**The endpoint-host rule has two traps, both of which produce a false `fail`
if taken literally.**

1. **`host.docker.internal` must never be resolution-tested.** It is the
   *correct* endpoint host for any containerised session (`otel/README.md:46-48`),
   and it does not resolve on a Linux host at all -- only inside a container.
   Since `doctor` is meant to run on the host, resolving it would report the
   recommended configuration as broken. So: match the host against the
   accepted set **first** (loopback, `host.docker.internal`, the bridge
   gateway IP, a literal IP), and only run a resolution probe on a host that
   is *not* in that set. That is the only case where resolution adds
   information -- it is what separates "typo" from "some other machine's
   collector".
2. **Resolution must be portable or skipped, not `getent`.** `getent` is a
   glibc tool: it does not exist on macOS, and Docker Desktop on macOS is a
   first-class target for this folder (`host.docker.internal` is its idiom).
   A missing `getent` that is treated as a failed lookup marks every macOS
   user's endpoint unresolvable. Use `socket.getaddrinfo` in the check-5
   python block, or -- if python3 is unavailable (check 0 failed) -- report
   this one sub-item as `skip` ("cannot resolve without python3"). Never
   infer "does not resolve" from the absence of the tool doing the resolving.

The rest of check 4 is pure string work on the process environment and
should stay in bash, ungated by python3.

### Check 5 -- synthetic metric round-trip

Skipped entirely under `--no-synthetic`, or if check 3 did not pass ("no
receiver to send to").

**Identity of the fake session.** Resource attribute
`service.name = otelctl-doctor-check`; point attribute
`session.id = doctorcheck-<epoch-seconds>`. Both are deliberate:

- `service.name` puts the whole thing in its own folder,
  `stats/otelctl-doctor-check/`, trivially isolatable and deletable, and
  obviously not a real session to anyone browsing `stats/`.
- `session.id` has **no dash before the unique part**, so
  `_session_filename` (`otel/aggregate.py:78-92`) takes the segment before
  the first `-` as the id prefix and the filename is always exactly
  `session-doctorcheck.json` -- a deterministic path to poll and to delete,
  no glob needed -- while the epoch inside the file's `session_id` field
  still proves the file came from *this* run.

**Sentinel values**, chosen small enough to be harmless if a cleanup ever
fails and distinct enough to prove the bucketing (`otel/aggregate.py:52-57`,
`212-214`): `cost.usage` = `0.000001` USD; `token.usage` = 1 `input`,
2 `output`, 3 `cacheRead`, 4 `cacheCreation`; `active_time.total` = `0.001` s
with `type=cli`.

**5a. POST accepted.** `POST http://127.0.0.1:$PORT/v1/metrics`,
`Content-Type: application/json`, 5s timeout, body exactly this
OTLPExportMetricsServiceRequest (validated end to end against the real
`aggregate.py` -- see "Validation" below):

```json
{"resourceMetrics": [{
  "resource": {"attributes": [
    {"key": "service.name", "value": {"stringValue": "otelctl-doctor-check"}}]},
  "scopeMetrics": [{
    "scope": {"name": "otelctl.doctor"},
    "metrics": [
      {"name": "claude_code.cost.usage", "unit": "USD",
       "sum": {"aggregationTemporality": 1, "isMonotonic": true, "dataPoints": [
         {"attributes": [{"key": "session.id", "value": {"stringValue": "doctorcheck-<epoch>"}}],
          "startTimeUnixNano": "<now_ns>", "timeUnixNano": "<now_ns>", "asDouble": 0.000001}]}},
      {"name": "claude_code.token.usage", "unit": "tokens",
       "sum": {"aggregationTemporality": 1, "isMonotonic": true, "dataPoints": [
         {"attributes": [{"key": "session.id", "value": {"stringValue": "doctorcheck-<epoch>"}},
                         {"key": "type", "value": {"stringValue": "input"}}],
          "startTimeUnixNano": "<now_ns>", "timeUnixNano": "<now_ns>", "asInt": "1"}]}},
      {"name": "claude_code.active_time.total", "unit": "s",
       "sum": {"aggregationTemporality": 1, "isMonotonic": true, "dataPoints": [
         {"attributes": [{"key": "session.id", "value": {"stringValue": "doctorcheck-<epoch>"}},
                         {"key": "type", "value": {"stringValue": "cli"}}],
          "startTimeUnixNano": "<now_ns>", "timeUnixNano": "<now_ns>", "asDouble": 0.001}]}}
    ]}]}]}
```

(the `token.usage` metric carries all four `dataPoints` -- `input`/`output`/
`cacheRead`/`cacheCreation` -- elided above for length; `aggregationTemporality: 1`
is DELTA, matching Claude Code's documented default.)

- `ok`: HTTP 200 **and** an empty/absent `partialSuccess` in the response
  body.
- `fail`: non-200, or `partialSuccess.rejectedDataPoints > 0` -- quote
  `partialSuccess.errorMessage`, which is the collector telling us exactly
  what it disliked.

**5b. It reached the raw file.** Poll
`$CLD_OTEL_DIR/data/raw-metrics.jsonl` every 0.2s for up to **5s** for a
line containing `otelctl-doctor-check` **and** this run's `session.id`. Read
only the tail (the file is unbounded -- review item #7); seek to the size
recorded just before the POST and read forward from there.

- `ok`, with the elapsed time. Expect it to be near-instant: the pipeline has
  no `batch` processor, so one export request is one line
  (`otel-collector-config.yaml:31-35`).
- `fail`: "the collector accepted the metric but never wrote it" -- the file
  exporter is broken, almost always the mount-permission trap; point at
  check 2's log scan and at `otelctl.sh logs collector`.

**5c. It became a stats file.** Per **D2**, live-watcher poll first,
isolated replay as the discriminator. Pre-delete the artifacts (D3) *before*
5a, not here.

Poll for **either** candidate path -- `stats/otelctl-doctor-check/session-doctorcheck.json`
and the flat-mode `stats/session-doctorcheck.json` -- accepting whichever
appears. Both are checked regardless of this shell's `CLD_OTEL_FLAT_STATS`,
because the running watcher may have been started with a different value:
`otelctl.sh` never passes `--flat-output`, it relies on `aggregate.py`
reading the env var itself (`otel/aggregate.py:359-361`).

| Situation | Verdict |
|---|---|
| Watcher pid alive (`agg_running()`, `otel/otelctl.sh:63`) **and** a candidate file appears within `--timeout` (default 10s) with `session_id` == ours and all sentinel values matching | `ok`: "live pipeline verified end to end in N.Ns" -- the strongest statement `doctor` can make |
| File appears but values or `session_id` do not match | `fail`: "stats file present but not from this run -- a leftover artifact that could not be pre-deleted?" Print the mismatch. |
| No watcher pid | run replay. Replay `ok` -> **`warn`**: "no aggregator running; `aggregate.py` converts the metric correctly, so raw data is being captured and will be aggregated as soon as one starts (offset tracking picks it up, `otel/aggregate.py:311`). Run `otelctl.sh start`." Nothing is lost -- say so, it is the difference between a shrug and a panic. |
| Watcher pid alive but nothing appears in time | run replay. Replay `ok` -> **`fail`**: "aggregator alive (pid N) but did not pick up the metric in Ns, while `aggregate.py` handles the same line fine in isolation -- the watcher is stalled or watching something else." Print the three facts that localize it: the offset file value vs. the raw file size, and the watcher's actual argv from `ps -p <pid> -o args=` (which exposes a mismatched `--input`/`--output-dir`), then point at `otelctl.sh logs aggregate`. |
| Replay itself fails | **`fail`**: "`aggregate.py` could not turn the received metric into stats" + its stderr. This is a real bug in the aggregator or a payload the collector mangled -- the only outcome that is our own code's fault. |

**Replay mechanism (option C).** `mktemp -d`; write the *single* matched line
from 5b into `<tmp>/raw-metrics.jsonl`; run
`$PYTHON aggregate.py --input <tmp>/raw-metrics.jsonl --output-dir <tmp>/stats`;
assert the expected stats file exists with the sentinel values. `rm -rf` the
temp dir on exit via `trap`. Nothing outside the temp dir is read or written,
so a live watcher is completely unaffected.

**Cleanup line** (its own reported line, never silent):
`removed stats/otelctl-doctor-check/` -- or, under
`--keep-check-artifacts`, `kept stats/otelctl-doctor-check/ (--keep-check-artifacts)`.
Either way it also states the residue that cannot be removed: one synthetic
line in `raw-metrics.jsonl`.

## Grammar

```
otelctl.sh doctor [--timeout SECS] [--no-synthetic] [--keep-check-artifacts]
```

- `--timeout SECS` (default 10) -- the 5c stats-file poll only. 5b's raw-file
  poll stays fixed at 5s, and the network probes at 2-5s; the aggregator poll
  is the only one whose right value depends on the user's machine.
- `--no-synthetic` -- skip check 5. Read-only diagnosis, and no permanent
  line added to `raw-metrics.jsonl`.
- `--keep-check-artifacts` -- leave the synthetic stats file in place.

An unknown flag exits 2 with the usage line, matching the existing dispatch.
`doctor` joins the usage strings at `otel/otelctl.sh:120` and `:130` and the
`case` at `:124`.

## Report format

One line per check: a 6-column state tag, a fixed-width label, a message.
Optional continuation lines are indented under the message and carry the
remedy, prefixed `->`. Results stream as they complete -- the whole run is
seconds, and streaming means a hang is visibly attributable to one check.

Fully green:

```
otelctl doctor -- state /home/zet/.cld/otel, collector port 4318

[ ok ] python3           Python 3.12.3 (/usr/bin/python3)
[ ok ] state dir         /home/zet/.cld/otel, writable
[ ok ] docker            daemon reachable (server 27.1.1)
[ ok ] collector         cld-otel-collector up 3h12m (otel/opentelemetry-collector-contrib:latest)
[ ok ] mounts            /data -> ~/.cld/otel/data, config -> otel/otel-collector-config.yaml
[ ok ] collector logs    no errors in last 200 lines
[ ok ] port 4318         bound on 172.17.0.1, 127.0.0.1; OTLP receiver answering
[ ok ] shell env         telemetry on, otlp/http-json -> http://localhost:4318, service.name=my-session
                         -> export interval 60000ms: a new session's first stats file can take that long
[ ok ] round trip        POST accepted -> raw-metrics.jsonl (0.1s) -> stats file (1.3s), live watcher pid 4242
[ ok ] cleanup           removed stats/otelctl-doctor-check/ (1 synthetic line remains in raw-metrics.jsonl)

10 ok -- telemetry is flowing
```

Docker not installed at all (the brief's explicit question). The Docker and
container checks fail/skip, but the port and round-trip checks still run,
because a collector could be running some other way -- here it is not:

```
otelctl doctor -- state /home/zet/.cld/otel, collector port 4318

[ ok ] python3           Python 3.12.3 (/usr/bin/python3)
[warn] state dir         /home/zet/.cld/otel does not exist -- nothing has ever run here
[fail] docker            `docker` not found on PATH
                         -> the collector runs as a container; install Docker, then `otelctl.sh start`
[skip] collector         requires docker
[skip] mounts            requires docker
[skip] collector logs    requires docker
[fail] port 4318         nothing listening on 127.0.0.1:4318
                         -> no collector is reachable, by Docker or otherwise
[warn] shell env         no telemetry variables set in this shell
                         -> doctor sees only exported variables; a wrapper (cld, systemd, an IDE) sets
                            them in its own environment, which may be fine
[skip] round trip        requires a reachable OTLP receiver

1 ok, 2 warnings, 2 failures -- telemetry is NOT being collected
next: install Docker and run `otelctl.sh start`
```

Collector healthy, shell misconfigured -- the ticket's motivating case:

```
[ ok ] port 4318         bound on 172.17.0.1, 127.0.0.1; OTLP receiver answering
[fail] shell env         OTEL_EXPORTER_OTLP_PROTOCOL is not set
                         -> Claude Code has no default protocol, so nothing is exported;
                            export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
[fail] shell env         OTEL_EXPORTER_OTLP_ENDPOINT host "localhsot" does not resolve
                         -> export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
[ ok ] round trip        POST accepted -> raw-metrics.jsonl (0.1s) -> stats file (0.9s), live watcher pid 4242

8 ok, 2 failures -- the pipeline is healthy, but this shell will not export to it
next: fix the two shell env failures above (see `otelctl.sh env` for the full block)
```

Rules for the summary line:

- Counts in `ok, warnings, failures` order; omit zero categories; skips are
  listed separately only if any occurred.
- The verdict clause is chosen from where the failures are: no failures ->
  "telemetry is flowing"; failures only in check 4 -> "the pipeline is
  healthy, but this shell will not export to it"; anything else ->
  "telemetry is NOT being collected".
- `next:` names exactly **one** action -- the earliest failing link in the
  chain, since fixing it may change everything downstream. No `next:` line on
  a clean run.

## Validation already done

Against the real `otel/aggregate.py`, in a temp directory:

- The payload above, fed through the aggregator, produces
  `stats/otelctl-doctor-check/session-doctorcheck.json` containing
  `service_name: otelctl-doctor-check`, `session_id: doctorcheck-<epoch>`,
  `cost_usd: 1e-06`, `tokens: {input: 1, output: 2, cache_read: 3,
  cache_creation: 4}`, `active_time_seconds: 0.001`. Filename and directory
  are exactly as predicted, so 5c's poll paths are correct.
- The concurrent-double-count experiment in **D2** (`tokens.input` 100 -> 200).

Two facts the implementer should not have to rediscover:

- `aggregate.py` ignores any line not terminated by `\n`
  (`otel/aggregate.py:330`) -- treat a match on a partial final line as
  not-yet-arrived and keep polling.
- `cost_usd` serializes as `1e-06`. Compare **parsed floats** with a
  tolerance, never strings.

## Also part of this change

- `otel/README.md`: add `doctor` to the command block in "Run it" and a short
  "If nothing shows up" paragraph pointing at it, mentioning the permanent
  synthetic line and the 60s export interval.
- `otel/QUICK-START.md`: a step 4, "Check it works: `./otelctl.sh doctor`".
- Suggested tests, following the `tests/test_broker_sh.py` precedent
  (source one function out of the real script and drive it with `bash`):
  the env-var evaluator's verdicts across a table of environments (pure
  logic, no docker), the summary/exit-code mapping from a synthetic list of
  states, and the replay path against a temp raw file. The docker- and
  network-dependent checks are not worth mocking.

## Implementation notes

- `set -euo pipefail` is active (`otel/otelctl.sh:17`) and a failing check
  must not abort the run. Every probe needs `if ! ...; then` or an explicit
  `|| true`, and `local x=$(cmd)` masks `cmd`'s exit status -- declare, then
  assign.
- `doctor` must not `exit` early anywhere except on a usage error; the
  summary and the exit code are computed once, at the end.
- Reuse `collector_running()` and `agg_running()` rather than
  re-implementing them, so `doctor` cannot drift from `status`.
- One `docker inspect` call for the container, parsed once, feeds checks 2
  and 3.
- **Do not refactor T5's `collector_preflight()`.** T5 (actionable startup
  errors) lands in the same chain and adds its own docker-installed /
  daemon-reachable check to `otelctl.sh`. The duplication with check 1 is
  deliberate and accepted: T5's helper *exits* on failure, which is right for
  `start`, whereas check 1 must record a `fail` and keep going. Folding them
  into one shared helper would force one of the two behaviors on the other.
- **Report an unexercised check as untested, never as passing.** Any check
  that could not actually be run in the build environment (no docker daemon
  in a container, say) is a known gap to state plainly. A known gap ships
  fine; a false green does not.
