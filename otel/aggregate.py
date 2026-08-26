#!/usr/bin/env python3
"""Aggregate raw OTLP-JSON metric exports into one running-totals file per session.

Standalone: no dependency on cld or any other application, stdlib only. Reads
the metric export stream written by the OpenTelemetry Collector's `file`
exporter (see otel-collector-config.yaml) and sums the three metrics Claude
Code documents at https://code.claude.com/docs/en/monitoring-usage.md --
claude_code.cost.usage, claude_code.token.usage (bucketed by its `type`
attribute), and claude_code.active_time.total -- into
<output-dir>/<service.name>/<session>.json. `service.name` is the resource
attribute the wrapping process sets (a cld container name, or whatever a bare
host session picks -- see README.md); `session.id` is Claude Code's own
per-process session identifier, attached natively to every metric point
without any wrapper involvement, so a single long-lived `service.name` gets
split into one file per distinct Claude Code session automatically.

Each metric is a delta counter, so values are summed across exports, not
overwritten. Progress through the input file is tracked in a small offset
file alongside the output dir, so re-running (e.g. after a crash) does not
double-count already-processed lines; a crash between writing a session's
stats and persisting the offset can still double-count that one line.

Usage:
    aggregate.py [--input raw-metrics.jsonl] [--output-dir stats] [--watch]
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

COST_METRIC = "claude_code.cost.usage"
TOKEN_METRIC = "claude_code.token.usage"
ACTIVE_TIME_METRIC = "claude_code.active_time.total"

# claude_code.token.usage's `type` attribute values -> our stats.json keys.
TOKEN_TYPE_KEYS = {
    "input": "input",
    "output": "output",
    "cacheRead": "cache_read",
    "cacheCreation": "cache_creation",
}


def _attr(attributes, key):
    for attr in attributes or []:
        if attr.get("key") == key:
            value = attr.get("value", {})
            for kind in ("stringValue", "intValue", "doubleValue", "boolValue"):
                if kind in value:
                    return value[kind]
    return None


def _point_value(point):
    if "asDouble" in point:
        return float(point["asDouble"])
    if "asInt" in point:
        return float(point["asInt"])
    return 0.0


def _session_filename(session_id):
    # Claude Code's session.id is a UUID; the first segment is already
    # effectively unique per container and matches what a human would
    # recognize as "the session that starts with ...".
    if session_id:
        return f"session-{session_id.split('-')[0]}"
    return "unknown-session"


def _stats_path(output_dir, service_name, session_id):
    return output_dir / service_name / f"{_session_filename(session_id)}.json"


def _empty_stats(service_name, session_id):
    return {
        "service_name": service_name,
        "session_id": session_id,
        "cost_usd": 0.0,
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
        "active_time_seconds": 0.0,
        "last_updated": None,
    }


def _load_stats(stats_path, service_name, session_id):
    if stats_path.is_file():
        return json.loads(stats_path.read_text())
    return _empty_stats(service_name, session_id)


def _apply_metric(stats, name, attributes, value):
    if name == COST_METRIC:
        stats["cost_usd"] += value
    elif name == TOKEN_METRIC:
        bucket = TOKEN_TYPE_KEYS.get(_attr(attributes, "type"))
        if bucket:
            stats["tokens"][bucket] += int(value)
    elif name == ACTIVE_TIME_METRIC:
        stats["active_time_seconds"] += value


def process_export(export, output_dir, cache):
    """Apply one OTLP ExportMetricsServiceRequest JSON object to per-session stats.

    `service.name` (folder) comes from the resource attributes, set once per
    process by whatever wraps `claude`. `session.id` (file) comes from each
    metric point's own attributes -- Claude Code stamps it natively on every
    point, and it can legitimately change mid-export-stream for a single
    `service.name` (e.g. `/fork` switches the running process to a new
    session), so it's read per point rather than once per resource_metric.
    """
    for resource_metric in export.get("resourceMetrics", []):
        resource_attrs = resource_metric.get("resource", {}).get("attributes", [])
        service_name = _attr(resource_attrs, "service.name")
        if not service_name:
            continue
        touched = set()
        for scope_metric in resource_metric.get("scopeMetrics", []):
            for metric in scope_metric.get("metrics", []):
                name = metric.get("name")
                if name not in (COST_METRIC, TOKEN_METRIC, ACTIVE_TIME_METRIC):
                    continue
                for point in metric.get("sum", {}).get("dataPoints", []):
                    point_attrs = point.get("attributes", [])
                    session_id = _attr(point_attrs, "session.id")
                    cache_key = (service_name, session_id)
                    stats = cache.get(cache_key)
                    if stats is None:
                        stats = _load_stats(_stats_path(output_dir, service_name, session_id), service_name, session_id)
                        cache[cache_key] = stats
                    _apply_metric(stats, name, point_attrs, _point_value(point))
                    touched.add(cache_key)
        for cache_key in touched:
            stats = cache[cache_key]
            stats["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            stats_path = _stats_path(output_dir, *cache_key)
            stats_path.parent.mkdir(parents=True, exist_ok=True)
            stats_path.write_text(json.dumps(stats, indent=2) + "\n")


def run(input_path, output_dir, watch):
    output_dir.mkdir(parents=True, exist_ok=True)
    offset_path = output_dir / f".{input_path.name}.offset"
    cache = {}
    with input_path.open("r") as f:
        if offset_path.is_file():
            try:
                f.seek(int(offset_path.read_text().strip()))
            except (ValueError, OSError):
                pass
        while True:
            pos = f.tell()
            line = f.readline()
            if not line or not line.endswith("\n"):
                # EOF, or a partial line still being flushed by the collector.
                if not watch:
                    break
                f.seek(pos)
                time.sleep(1.0)
                continue
            line = line.strip()
            if line:
                try:
                    export = json.loads(line)
                except json.JSONDecodeError:
                    print(f"skipping unparseable line: {line[:200]}", file=sys.stderr)
                else:
                    process_export(export, output_dir, cache)
            offset_path.write_text(str(f.tell()))


def main():
    # Matches otelctl.sh's own default: $CLD_OTEL_DIR/data is where the
    # collector's file exporter writes, so the two agree without any flags.
    data_dir = Path(os.environ.get("CLD_OTEL_DIR", "~/.cld/otel")).expanduser()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, default=data_dir / "data" / "raw-metrics.jsonl",
                         help="raw OTLP-JSON export stream (default: %(default)s)")
    parser.add_argument("--output-dir", type=Path, default=data_dir / "stats",
                         help="per-session stats output dir (default: %(default)s)")
    parser.add_argument("--watch", action="store_true",
                         help="keep tailing --input for new export lines instead of exiting at EOF")
    args = parser.parse_args()
    if not args.input.exists():
        args.input.parent.mkdir(parents=True, exist_ok=True)
        args.input.touch()
    run(args.input, args.output_dir, args.watch)


if __name__ == "__main__":
    main()
