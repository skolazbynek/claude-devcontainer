# Quick start

1. **Start it** (needs Docker) -- one script runs both the collector and the
   background aggregator:

   ```
   ./otelctl.sh start
   ```

   Collector listens on `127.0.0.1:4318`, writes raw metrics to
   `~/.cld/otel/data/raw-metrics.jsonl`; the aggregator tails that into
   `~/.cld/otel/stats/<service.name>.json` per session as data arrives.

   Check with `./otelctl.sh status` / `logs`, stop with `./otelctl.sh stop`.
   `start` (and `restart`) also print the `export` block from step 2 below,
   already filled in with the right host and port -- copy-paste from there
   instead of retyping it.

2. **Point a Claude Code session at it.** For cld, set in config:

   ```
   otel_endpoint = "host.docker.internal:4318"
   ```

   and relaunch a `master`/`agent`/`task-agent`/devcontainer session -- it's wired in automatically.

   For any other Claude Code session (no cld involved):

   ```
   export CLAUDE_CODE_ENABLE_TELEMETRY=1
   export OTEL_METRICS_EXPORTER=otlp
   export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
   export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
   export OTEL_RESOURCE_ATTRIBUTES=service.name=my-session
   ```

   To make this persistent instead of retyping it in every terminal, `./otelctl.sh
   env` prints these same lines -- `eval "$(./otelctl.sh env)"`, or append to a
   shell rc / `.envrc`. See `README.md` for the `--docker` variant.

3. **Check the numbers**:

   ```
   cat ~/.cld/otel/stats/my-session.json
   ```

4. **Nothing there? Check it works**:

   ```
   ./otelctl.sh doctor
   ```

   Walks the whole chain end to end and tells you exactly what's missing.

See `README.md` for the full picture (data flow, output schema, caveats).

Need this on another machine? See "Getting this on another machine" in
`README.md` -- a `curl | tar` one-liner if you can reach github.com, or
`./pack.sh` to hand someone a file (no network needed either end).
