# Cost/token/time observability

A standalone OpenTelemetry setup for tracking Claude Code's own cost, token,
and active-time usage per session. Nothing here depends on cld -- it's an
ordinary OTel Collector plus a small aggregation script, both usable by
anyone running Claude Code, cld or not. cld just happens to be one client
that points at it (see `stage_otel()` in `../cld/docker.py`).

```
Claude Code (any session) --OTLP/http--> otel-collector --file exporter--> data/raw-metrics.jsonl --> aggregate.py --> stats/<service.name>/<session>.json
```

## Run it

One script controls the whole pipeline -- the collector container and the
background aggregator process:

```
./otelctl.sh start              # docker run collector (127.0.0.1:4318) + aggregate.py --watch
./otelctl.sh status             # both, at a glance
./otelctl.sh logs               # both, collector then aggregator
./otelctl.sh logs collector 100
./otelctl.sh logs aggregate 100
./otelctl.sh stop               # both
./otelctl.sh restart
```

State lives under `$CLD_OTEL_DIR` (default `~/.cld/otel`): the raw metrics
file (`data/raw-metrics.jsonl`), the aggregator's PID file and log
(`aggregate.pid`, `aggregate.log`), and the per-session output (`stats/`).
Override the collector image with `$CLD_OTEL_IMAGE`, the port with
`$CLD_OTEL_PORT`.

## Point a Claude Code session at it

Any session, not just cld's:

```
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=http://<collector-host>:4318
export OTEL_RESOURCE_ATTRIBUTES=service.name=<some-identifying-name>
```

`service.name` is the standard OTel resource attribute used to key the
per-session output file -- pick whatever name you want that session's stats
filed under. In cld, this is wired automatically: set `otel_endpoint` (or
`CLD_OTEL_ENDPOINT`) in config, and `master`/`agent`/`task-agent`/the bare
devcontainer point at it with `service.name` set to the session name. `cld
run` is unaffected -- it keeps its existing VCS-committed cost reporting.

## Output

`otelctl.sh start` runs the aggregator (`aggregate.py --watch`) in the
background automatically. To run it directly instead -- e.g. for a one-shot
pass over whatever's arrived so far -- `./aggregate.py` (add `--watch` to
keep tailing). It defaults to `$CLD_OTEL_DIR/data/raw-metrics.jsonl` and
`$CLD_OTEL_DIR/stats`, matching where the collector writes; override with
`--input`/`--output-dir` for a different layout.

Output is one folder per `service.name`, one file per Claude Code session
inside it -- `stats/<service.name>/session-<id-prefix>.json` -- sums held
over the metric's whole lifetime (all three Claude Code metrics are delta
counters, so exports are summed, not overwritten):

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
so a renamed session keeps writing to the same file; forking mints a new
`session.id`, so a fork starts a new file.

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
