# Cost/token/time observability

A standalone OpenTelemetry setup for tracking Claude Code's own cost, token,
and active-time usage per session. Nothing here depends on cld -- it's an
ordinary OTel Collector plus a small aggregation script, both usable by
anyone running Claude Code, cld or not. cld just happens to be one client
that points at it (see `stage_otel()` in cld's `cld/docker.py`).

```
Claude Code (any session) --OTLP/http--> otel-collector --file exporter--> data/raw-metrics.jsonl --> aggregate.py --> stats/<service.name>/<session>.json
```

## Run it

One script controls the whole pipeline -- the collector container and the
background aggregator process:

```
./otelctl.sh start              # docker run collector (bridge-gateway IP + 127.0.0.1, port 4318) + aggregate.py --watch
./otelctl.sh status             # both, at a glance
./otelctl.sh logs               # both, collector then aggregator
./otelctl.sh logs collector 100
./otelctl.sh logs aggregate 100
./otelctl.sh stop               # both
./otelctl.sh restart
./otelctl.sh doctor              # end-to-end health check, see below
```

State lives under `$CLD_OTEL_DIR` (default `~/.cld/otel`): the raw metrics
file (`data/raw-metrics.jsonl`), the aggregator's PID file and log
(`aggregate.pid`, `aggregate.log`), and the per-session output (`stats/`).
Override the collector image with `$CLD_OTEL_IMAGE`, the port with
`$CLD_OTEL_PORT`.

## Point a Claude Code session at it

`otelctl.sh start`/`restart` print a ready-to-paste `export` block for this
(both the host and in-container variant) once the collector is up, filled in
with the actual port. The rest of this section is the same information for
reference.

Any session, not just cld's:

```
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=http://<collector-host>:4318
export OTEL_RESOURCE_ATTRIBUTES=service.name=<some-identifying-name>
```

`<collector-host>` is `localhost` for a session running directly on the same
host as the collector, or `host.docker.internal` for a session running
inside any docker container on that host. The collector binds only to the
docker bridge gateway IP and loopback -- not `0.0.0.0` -- so it's reachable
from both without being exposed on the LAN-facing interface.

`service.name` is the standard OTel resource attribute used to key the
per-session output file -- pick whatever name you want that session's stats
filed under. In cld, this is wired automatically: set `otel_endpoint` (or
`CLD_OTEL_ENDPOINT`) in config, and `master`/`agent`/`task-agent`/the bare
devcontainer point at it with `service.name` set to the session name. `cld
run` is unaffected -- it keeps its existing VCS-committed cost reporting.

To make this persistent instead of retyping it in every terminal, `./otelctl.sh
env` prints these same lines -- `eval "$(./otelctl.sh env)"` for the current
shell, or `./otelctl.sh env >> ~/.bashrc` (or `.envrc`) to keep it. See
`./otelctl.sh env --help` for the `--docker` flag.

## If nothing shows up

`./otelctl.sh doctor` walks the whole chain -- Docker running? collector
container up? port bound and answering? telemetry env vars set in *this*
shell? -- and finishes by sending one synthetic metric through the real
pipeline (collector -> `raw-metrics.jsonl` -> `aggregate.py` -> a stats file)
to prove it actually round-trips, not just that each piece looks up. Each
check prints `ok`/`warn`/`fail`/`skip` with a one-line reason and, on
failure, the next thing to try; it exits non-zero if anything failed.

The synthetic check uses `service.name=otelctl-doctor-check`, so it never
touches a real session's numbers, and it deletes the stats file it creates
(`--keep-check-artifacts` to keep it for inspection). The one thing it can't
clean up is its own line in `raw-metrics.jsonl` -- that file is append-only
by design (see "Output" below), so a stray `otelctl-doctor-check` entry is
expected to accumulate there, one line per `doctor` run.

If a session's stats file isn't updating and you're not sure why, this is
the first thing to run -- a first stats file can also just take a while to
show up: exports fire on Claude Code's export interval, 60s by default.

## Output

`otelctl.sh start` runs the aggregator (`aggregate.py --watch`) in the
background automatically. To run it directly instead -- e.g. for a one-shot
pass over whatever's arrived so far -- `./aggregate.py` (add `--watch` to
keep tailing). It defaults to `$CLD_OTEL_DIR/data/raw-metrics.jsonl` and
`$CLD_OTEL_DIR/stats`, matching where the collector writes; override with
`--input`/`--output-dir` for a different layout.

Output is one folder per `service.name`, one file per Claude Code session
inside it -- `stats/<service.name>/session-<id-prefix>.json`, or
`stats/<service.name>/<rename>-<id-prefix>.json` if the session has been
renamed with Claude Code's own `/rename` -- sums held over the metric's
whole lifetime (all three Claude Code metrics are delta counters, so
exports are summed, not overwritten):

```json
{
  "service_name": "cld_task-agent_foo",
  "session_id": "6bd5fc5c-ce52-47ff-97c7-ce6b9b490d8c",
  "cost_usd": 0.0468,
  "tokens": {
    "input": 240,
    "output": 90,
    "cache_read": 1800,
    "cache_creation": 600
  },
  "active_time_seconds": 10.4,
  "last_updated": "2026-08-26T13:34:18Z"
}
```

The split is automatic and needs no configuration -- `session.id` is a
standard attribute Claude Code stamps on every metric point itself, so one
long-lived `service.name` (e.g. a `cld master` container, or a host session
that reuses the same name across runs) still gets one file per distinct
session, without cld or this pipeline having to track session identity
itself. Renaming a session in Claude Code doesn't change its `session.id`,
so a renamed session keeps writing to the same file (see below); forking
mints a new `session.id`, so a fork starts a new file.

`/rename` itself never touches OTel -- the aggregator instead reads the name
back out of the session's own Claude Code transcript
(`~/.claude/projects/<project-slug>/<session-id>.jsonl`, matched by
`session.id`), which cld bind-mounts into every container at the same path
as the host. This is checked on every export batch, so a rename shows up on
the next export after it happens, and the existing file's history moves
with it -- across a `/fork`, an `otelctl.sh restart`, and any further
renames. A session that's never been renamed keeps the plain
`session-<id-prefix>.json` name. On a host where `~/.claude` isn't reachable
(no cld, not the same machine as the collector), names just never resolve
and every session keeps the id-based filename -- override the transcript
location with `$CLD_CLAUDE_DIR` (default `~/.claude`) if it lives elsewhere.

Pass `--flat-output` to `aggregate.py` (or set `CLD_OTEL_FLAT_STATS=1`, which
`otelctl.sh` picks up the same way it does `$CLD_OTEL_DIR`) to write
`stats/<session>.json` directly instead of splitting into one folder per
`service.name`. The default stays folder-split; switching the setting on an
already-running session adopts its existing file rather than forking a new
one under the other layout.

`cache_read` tokens are billed far cheaper than fresh `input` tokens; a
session with high `cache_read` relative to `input` is getting real value out
of prompt caching, not just generating a big token count.

Re-running `aggregate.py` (e.g. after a crash) does not double-count already
-processed lines -- progress is tracked in a small `.raw-metrics.jsonl.offset`
file next to the output dir. A crash between writing one session's stats.json
and persisting the new offset can still double-count that single line; this
is best-effort, not transactional.

## Requirements

- `otel/opentelemetry-collector-contrib` image (the `file` exporter used here
  isn't in the core `otel/opentelemetry-collector` image).
- `aggregate.py` is stdlib-only Python 3 -- no install step.
- Docker installed and the daemon running. `otelctl.sh start` checks for both
  up front, and diagnoses common failures (port already in use, image pull
  failure, bind-mount permission denied) instead of leaving you to `docker
  logs` it out -- see the raw error printed alongside the diagnosis if a
  failure doesn't match a known cause.

## Getting this on another machine

Cloning the whole `cld` repo just to get these six files is unnecessary.
Two ways to get just `otel/`, each for a different situation:

### If you can reach github.com

```
curl -fsSL https://codeload.github.com/skolazbynek/claude-devcontainer/tar.gz/main \
  | tar xz --strip-components=1 claude-devcontainer-main/otel
```

This fetches whatever is on `main` right now -- not necessarily whatever's in
front of you locally. The `claude-devcontainer-main/` prefix follows the ref:
swap `main` for a tag or a commit SHA and the prefix has to change to match
(`claude-devcontainer-<ref>/otel`), or the archive won't have a member by that
name.

### If you can't, or you want to hand someone a file

From inside any copy of `otel/`:

```
./pack.sh
```

This produces a single self-extracting file, `otel-standalone-<date>-<rev>.sh`
(~26 KB) -- a readable header followed by a base64-encoded copy of this
directory. Hand it to someone over whatever channel already exists (scp, a
chat paste, a wiki attachment); they run:

```
bash otel-standalone-*.sh
```

and get an `otel/` directory to follow `QUICK-START.md` from. No repo access,
no hosting, no build step needed on either end -- `pack.sh` is itself part of
the payload, so a recipient can re-pack and pass it on. Unlike the `curl` form
above, this packs *your* copy of the tree, not `main`.

Useful flags:

```
otel-standalone-*.sh --list         # print provenance + manifest, extract nothing
otel-standalone-*.sh --check        # verify the payload checksum only
otel-standalone-*.sh --dir PATH     # extract somewhere other than ./otel
pack.sh --tarball                   # emit a plain .tar.gz instead, for anyone
                                     # who'd rather not run a script they were sent
```

The artifact is generated fresh from whatever is on disk every time
`pack.sh` runs -- there's no separately maintained copy to drift out of sync.
Each artifact carries a `PROVENANCE` file recording the source revision it
was packed from.

---

`curl` leads because it's genuinely shorter for the common case. `pack.sh`
isn't a fallback in the apologetic sense -- it's the only path that works
offline, air-gapped, without GitHub access, or from a copy someone was
already handed.

If you already have repo access, `git sparse-checkout` works too and needs no
new tooling -- a convenience for a live checkout, not really an install path:

```
git clone --filter=blob:none --sparse <url> && git sparse-checkout set otel
```
