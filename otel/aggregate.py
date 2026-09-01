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

If a session has been renamed with Claude Code's own `/rename`, `<session>`
is that name instead of the raw session id -- see `_resolve_session_name`
for where that's read from. A session that was never renamed keeps the old
`session-<id-prefix>` filename unchanged. Pass `--flat-output` (or set
`CLD_OTEL_FLAT_STATS`) to write `<output-dir>/<session>.json` directly
instead of splitting into one folder per `service.name`; the default stays
folder-split.

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
import re
import sys
import time
from pathlib import Path

def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


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


def _session_filename(session_id, custom_name=None):
    # Claude Code's session.id is a UUID; the first segment is already
    # effectively unique per container and matches what a human would
    # recognize as "the session that starts with ...". Kept as a suffix even
    # when a custom name is known, so two sessions renamed to the same thing
    # (or a name that collides with another session's id prefix) don't clash.
    id_prefix = session_id.split("-")[0] if session_id else None
    slug = _slugify(custom_name) if custom_name else None
    if slug and id_prefix:
        return f"{slug}-{id_prefix}"
    if slug:
        return slug
    if id_prefix:
        return f"session-{id_prefix}"
    return "unknown-session"


def _slugify(name):
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return slug[:80] or None


def _stats_path(output_dir, service_name, session_id, custom_name=None, flat=False):
    filename = f"{_session_filename(session_id, custom_name)}.json"
    return output_dir / filename if flat else output_dir / service_name / filename


# --- session naming (Claude Code's own /rename) --------------------------
#
# `/rename` writes nothing to OTel -- session.id never changes -- but it does
# append a `{"type": "custom-title", "customTitle": ..., "sessionId": ...}`
# line to the session's own Claude Code transcript
# (~/.claude/projects/<project-slug>/<session-id>.jsonl), re-emitted on most
# turns after the rename. cld bind-mounts ~/.claude into every container at
# the same path as the host (see stage_otel/docker.py), so that file is
# reachable from here for cld sessions without any change to what Claude Code
# exports over OTLP. A session never renamed has no such line; a host running
# this standalone (no cld, no shared ~/.claude) just never resolves a name,
# and those sessions keep today's session-id-based filename.

_CUSTOM_TITLE_TAIL_BYTES = 8192


def _find_transcript(claude_dir, session_id):
    matches = list(claude_dir.glob(f"projects/*/{session_id}.jsonl"))
    return matches[0] if matches else None


def _extract_custom_title(text, session_id):
    title = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") == "custom-title" and record.get("sessionId") == session_id:
            title = record.get("customTitle") or title
    return title


def _read_custom_title(transcript_path, session_id, tail_only):
    try:
        if tail_only:
            size = transcript_path.stat().st_size
            with transcript_path.open("rb") as f:
                f.seek(max(0, size - _CUSTOM_TITLE_TAIL_BYTES))
                data = f.read()
        else:
            data = transcript_path.read_bytes()
    except OSError:
        return None
    return _extract_custom_title(data.decode("utf-8", errors="ignore"), session_id)


def _resolve_session_name(session_id, claude_dir, session_names):
    """Current /rename title for session_id, or None if never renamed (or
    its transcript isn't reachable from this host).

    Checked on every export batch, but cheaply: the transcript is read in
    full only the first time a session is seen (to seed the name even if it
    was renamed long before this process started watching), and the file's
    size is stat'd on every later call -- an unchanged size skips the read
    entirely, and a changed one only tails the last few KB rather than
    re-parsing the whole transcript, since Claude Code re-appends the
    current title on most turns.
    """
    if not session_id:
        return None
    entry = session_names.get(session_id)
    if entry is None:
        transcript = _find_transcript(claude_dir, session_id)
        name = _read_custom_title(transcript, session_id, tail_only=False) if transcript else None
        size = transcript.stat().st_size if transcript else -1
        session_names[session_id] = {"transcript": transcript, "name": name, "size": size}
        return name
    transcript = entry["transcript"]
    if transcript is None:
        return entry["name"]
    try:
        size = transcript.stat().st_size
    except OSError:
        return entry["name"]
    if size != entry["size"]:
        entry["size"] = size
        title = _read_custom_title(transcript, session_id, tail_only=True)
        if title:
            entry["name"] = title
    return entry["name"]


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


def _find_existing_stats_path(output_dir, service_name, session_id):
    """This session's stats file from a previous aggregate.py run, wherever
    an earlier /rename -- or a since-changed --flat-output setting -- may
    have left it, found by the stable `...-<id-prefix>.json` suffix every
    stats filename carries (see `_session_filename`), content-verified
    against the full session_id since an 8-char prefix collision, while
    unlikely, isn't impossible. Checks both the flat and folder-split
    locations regardless of the current setting, so toggling --flat-output
    doesn't fork a session's history into a second file. None if this
    session has no stats file yet.
    """
    id_prefix = session_id.split("-")[0] if session_id else None
    if not id_prefix:
        return None
    for search_dir in (output_dir, output_dir / service_name):
        for candidate in search_dir.glob(f"*-{id_prefix}.json"):
            try:
                if json.loads(candidate.read_text()).get("session_id") == session_id:
                    return candidate
            except (OSError, json.JSONDecodeError):
                continue
    return None


def _current_stats_path(output_dir, cache_key, stats_paths, claude_dir, session_names, flat):
    """Stats path for cache_key, migrating/renaming the file on disk if the
    session's resolved name is new (first sight this run) or has changed (a
    /rename since we started tracking it) so its running history moves with
    it instead of forking into a second file. First sight is deliberately
    re-checked against disk every process start, not just cache_key's
    absence from an in-memory cache -- the aggregator restarting between two
    renames of the same session must not orphan the file left under the
    first name.
    """
    service_name, session_id = cache_key
    custom_name = _resolve_session_name(session_id, claude_dir, session_names)
    stats_path = _stats_path(output_dir, service_name, session_id, custom_name, flat)
    if cache_key not in stats_paths:
        existing = _find_existing_stats_path(output_dir, service_name, session_id)
        old_path = existing
    else:
        old_path = stats_paths[cache_key]
    if old_path is not None and old_path != stats_path and old_path.is_file():
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.rename(stats_path)
    stats_paths[cache_key] = stats_path
    return stats_path


def process_export(export, output_dir, cache, stats_paths, claude_dir, session_names, flat):
    """Apply one OTLP ExportMetricsServiceRequest JSON object to per-session stats.

    `service.name` (folder) comes from the resource attributes, set once per
    process by whatever wraps `claude`. `session.id` (file, alongside any
    resolved /rename name -- see `_resolve_session_name`) comes from each
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
                    stats_path = _current_stats_path(output_dir, cache_key, stats_paths, claude_dir, session_names, flat)
                    stats = cache.get(cache_key)
                    if stats is None:
                        stats = _load_stats(stats_path, service_name, session_id)
                        cache[cache_key] = stats
                    _apply_metric(stats, name, point_attrs, _point_value(point))
                    touched.add(cache_key)
        for cache_key in touched:
            stats = cache[cache_key]
            stats["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            stats_path = stats_paths[cache_key]
            stats_path.parent.mkdir(parents=True, exist_ok=True)
            stats_path.write_text(json.dumps(stats, indent=2) + "\n")


def run(input_path, output_dir, watch, claude_dir, flat=False):
    output_dir.mkdir(parents=True, exist_ok=True)
    offset_path = output_dir / f".{input_path.name}.offset"
    cache = {}
    stats_paths = {}
    session_names = {}
    with input_path.open("r") as f:
        if offset_path.is_file():
            try:
                offset = int(offset_path.read_text().strip())
                # If the input file is now shorter than the stored offset (e.g.
                # the collector restarted and truncated it), seeking there would
                # park us past current EOF forever, silently processing nothing
                # new. Start over from the top instead.
                if offset <= input_path.stat().st_size:
                    f.seek(offset)
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
                    process_export(export, output_dir, cache, stats_paths, claude_dir, session_names, flat)
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
    parser.add_argument("--flat-output", action="store_true", default=_env_flag("CLD_OTEL_FLAT_STATS"),
                         help="write stats/<name>.json directly instead of the default "
                              "stats/<service.name>/<name>.json (env: CLD_OTEL_FLAT_STATS)")
    args = parser.parse_args()
    if not args.input.exists():
        args.input.parent.mkdir(parents=True, exist_ok=True)
        args.input.touch()
    claude_dir = Path(os.environ.get("CLD_CLAUDE_DIR", "~/.claude")).expanduser()
    run(args.input, args.output_dir, args.watch, claude_dir, args.flat_output)


if __name__ == "__main__":
    main()
