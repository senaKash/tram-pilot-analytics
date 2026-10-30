#!/usr/bin/env python3
"""Tram Pilot: streaming TAR.ZST -> chunked CSV.GZ / Parquet datasets.

Python >= 3.10. Requires Python 3.14+ or command-line `zstd`.
CSV.GZ uses only the standard library. Optional Parquet export requires pyarrow.

This is an ingestion/normalization tool, NOT a detector of distinct failures.
It never extracts individual files from the archive to disk.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
import tarfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    from ._archive import open_tar_zst, StopScan
except ImportError:
    from _archive import open_tar_zst, StopScan

# Values in source JSON are mostly strings, including flags and numbers.
TEXT = [
    "source_file", "record_kind", "timestamp", "timestamp_local", "timestamp_source",
    "device_id", "loco_number", "cabin_type", "event_type", "event_id",
    "event_target", "m_current_route_name", "route_id", "cur_railsegm_id",
    "fsm_state", "handle", "goal_obj_type", "course_reverser_pos",
    "m_danger_obj_req", "m_trafficlight_req", "m_speed_limit_req",
    "m_danger_obj_status", "m_trafficlight_status", "m_speed_limit_status",
    "m_danger_obj_fsm_state", "m_trafficlight_fsm_state", "m_speed_limit_fsm_state",
    "m_danger_obj_act", "m_trafficlight_act", "m_speed_limit_act",
    "m_report_can_statistics_json", "m_report_ids_interrupts_json",
    "m_report_speeds_stuck_json", "raw_json",
]
INTEGER = ["event_index", "timestamp_utc_ms", "status_skip", "m_warn_level", "m_speed_mode", "m_speed_mode_driver_fb"]
FLOAT = [
    "speed", "goal_speed", "latitude", "longitude", "raw_last_latitude",
    "raw_last_longitude", "path_pos", "probability", "m_mains_voltage",
    "m_speeds_values_1", "m_speeds_values_2", "m_speeds_values_3",
    "m_report_cit_quality", "m_report_cifb_quality",
    "m_report_response_min_delay_ms", "m_report_response_max_delay_ms",
    "m_report_response_avg_delay_ms",
]
BOOLEAN = [
    "is_active_cabin", "is_adas_button_on", "is_loc_converged", "gps_with_rtk",
    "ubloxGps", "leftImage", "multiImages", "minsEth", "roadModel", "dbwFbTram",
    "odoFbTram", "tramPlannerService", "tramSlaveVisor", "t25Front",
    "tramVision", "is_can_request_but_no_response",
    "m_report_response_is_response", "m_report_response_is_request_but_no_response",
]
FIELDS = TEXT + INTEGER + FLOAT + BOOLEAN
assert len(FIELDS) == len(set(FIELDS))

STATUS_KEYS = ("m_danger_obj_status", "m_trafficlight_status", "m_speed_limit_status")
DEVICES = ("ubloxGps", "leftImage", "minsEth", "roadModel", "dbwFbTram", "odoFbTram", "tramPlannerService", "tramSlaveVisor", "t25Front", "tramVision")


def str_or_none(value):
    return str(value) if value is not None else None


def as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lower = value.strip().lower()
        if lower in ("true", "1"):
            return True
        if lower in ("false", "0"):
            return False
    if value == 1:
        return True
    if value == 0:
        return False
    return None


def as_int(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, float) and not value.is_integer():
            return None
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return None


def as_float(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        res = float(value)
        return res if res == res and res not in (float('inf'), float('-inf')) else None
    except (ValueError, TypeError, OverflowError):
        return None


def millis_utc(timestamp):
    if not isinstance(timestamp, str):
        return None
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None  # Never silently interpret timezone-less times as local.
        return round(dt.astimezone(timezone.utc).timestamp() * 1000)
    except (ValueError, OverflowError, OSError):
        return None


def as_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) if value is not None else None


def normalize(d, filename, index, kind, include_raw):
    response = d.get("m_report_response")
    if not isinstance(response, dict):
        response = {}
    row = {k: (str_or_none(d.get(k)) if k in TEXT else None) for k in FIELDS}
    row.update({k: as_int(d.get(k)) for k in INTEGER})
    row.update({k: as_float(d.get(k)) for k in FLOAT})
    row.update({k: as_bool(d.get(k)) for k in BOOLEAN})
    row["source_file"] = filename
    row["event_index"] = index
    row["record_kind"] = kind
    row["timestamp_utc_ms"] = millis_utc(d.get("timestamp"))
    for key in ("m_report_can_statistics", "m_report_ids_interrupts", "m_report_speeds_stuck"):
        row[key + "_json"] = as_json(d.get(key))
    for k in ("min_delay_ms", "max_delay_ms", "avg_delay_ms"):
        row["m_report_response_" + k] = as_float(response.get(k))
    for k in ("is_response", "is_request_but_no_response"):
        row["m_report_response_" + k] = as_bool(response.get(k))
    row["raw_json"] = as_json(d) if include_raw else None
    return row


class Writer:
    def __init__(self, output, kind, fmt, chunk_size, schema=None):
        self.directory = output / kind
        self.directory.mkdir(parents=True, exist_ok=True)
        self.fmt = fmt
        self.buffer = []
        self.chunk_size = chunk_size
        self.parts = 0
        self.written = 0
        self.schema = schema

    def add(self, row):
        self.buffer.append(row)
        if len(self.buffer) >= self.chunk_size:
            self.flush()

    def flush(self):
        if not self.buffer:
            return
        self.parts += 1
        if self.fmt == "parquet":
            import pyarrow as pa
            import pyarrow.parquet as pq
            path = self.directory / f"part-{self.parts:06d}.parquet"
            table = pa.Table.from_pylist(self.buffer, schema=self.schema)
            pq.write_table(table, str(path), compression="zstd")
        else:
            path = self.directory / f"part-{self.parts:06d}.csv.gz"
            with gzip.open(path, "wt", encoding="utf-8", newline="", compresslevel=4) as file:
                writer = csv.DictWriter(file, fieldnames=FIELDS, extrasaction="raise")
                writer.writeheader()
                for row in self.buffer:
                    writer.writerow({k: "" if value is None else (str(value).lower() if isinstance(value, bool) else value) for k, value in row.items()})
        self.written += len(self.buffer)
        self.buffer.clear()


def parquet_schema():
    try:
        import pyarrow as pa
    except ImportError as err:
        raise SystemExit("Parquet needs pyarrow. Run with --format csv for a no-dependency test, or install pyarrow in regular Ubuntu WSL/Windows Python.") from err
    d = {k: pa.string() for k in TEXT}
    d.update({k: pa.int64() for k in INTEGER})
    d.update({k: pa.float64() for k in FLOAT})
    d.update({k: pa.bool_() for k in BOOLEAN})
    return pa.schema([(k, d[k]) for k in FIELDS])


def process(args):
    archive = Path(args.archive)
    if not archive.is_file():
        raise SystemExit(f"Archive not found: {archive}")
    output = Path(args.output)
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"Output folder already contains files: {output} (choose a new --output)")
    schema = parquet_schema() if args.format == "parquet" else None
    track = Writer(output, "track", args.format, args.chunk_size, schema)
    event = Writer(output, "events", args.format, args.chunk_size, schema)
    summary = {
        "source_archive": str(archive), "format": args.format,
        "max_files": args.max_files, "root_types": {}, "event_types": {},
        "files_read": 0, "track_rows": 0, "event_rows": 0,
        "bad_json": 0, "skipped_large": 0, "unknown_shapes": 0,
        "invalid_events": 0, "missing_timestamps": 0,
        "error_snapshot_rows": 0, "error_rows_by_tram": {},
        "non_file_members": 0, "seconds": None, "notes": [
            "Error snapshot rows are NOT counts of distinct failures.",
            "Missing device field is NULL, not false; false means source data was stale.",
            "No event deduplication: event_id is unique only per vehicle and application session.",
            "Raw data remains in source archive; --include-raw optionally copies raw JSON to every output row.",
        ],
    }
    root_types, event_types, error_trams = Counter(), Counter(), Counter()
    t0 = time.monotonic()
    reached_limit = False
    try:
        with open_tar_zst(archive) as tf:
            for member in tf:
                if not member.isfile():
                    summary["non_file_members"] += 1
                    continue
                if args.max_files and summary["files_read"] >= args.max_files:
                    raise StopScan()
                summary["files_read"] += 1
                if member.size > args.max_json_mb * 1024 * 1024:
                    summary["skipped_large"] += 1
                    continue
                extracted = tf.extractfile(member)
                if extracted is None:
                    summary["bad_json"] += 1
                    continue
                try:
                    with extracted:
                        obj = json.load(extracted)
                except (ValueError, UnicodeError, RecursionError):
                    summary["bad_json"] += 1
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("events"), list):
                    root_types["events_container"] += 1
                    batch = ((x, i) for i, x in enumerate(obj["events"]))
                elif isinstance(obj, dict) and "event_type" in obj:
                    root_types["direct"] += 1
                    batch = ((obj, 0),)
                else:
                    root_types["unknown"] += 1
                    summary["unknown_shapes"] += 1
                    continue
                for d, index in batch:
                    if not isinstance(d, dict) or not d.get("event_type"):
                        summary["invalid_events"] += 1
                        continue
                    kind = "track" if d["event_type"] == "Track" else "events"
                    row = normalize(d, member.name, index, kind, args.include_raw)
                    if row["timestamp_utc_ms"] is None:
                        summary["missing_timestamps"] += 1
                    event_types[str(d["event_type"])] += 1
                    if kind == "track":
                        track.add(row)
                    else:
                        event.add(row)
                    if kind == "track" and "Error" in (d.get(k) for k in STATUS_KEYS):
                        summary["error_snapshot_rows"] += 1
                        error_trams[d.get("loco_number") or "<missing>"] += 1
                if args.progress_every and summary["files_read"] % args.progress_every == 0:
                    print(f"Processed {summary['files_read']:,} JSON files: track={track.written + len(track.buffer):,}; events={event.written + len(event.buffer):,}", flush=True)
    except StopScan:
        reached_limit = True
    track.flush()
    event.flush()
    summary.update({
        "root_types": dict(root_types), "event_types": dict(event_types),
        "track_rows": track.written, "event_rows": event.written,
        "error_rows_by_tram": dict(error_trams), "seconds": round(time.monotonic() - t0, 2),
        "track_parts": track.parts, "events_parts": event.parts,
        "complete_archive_scan": not reached_limit,
    })
    with (output / "summary.json").open("w", encoding="utf-8") as out:
        json.dump(summary, out, ensure_ascii=False, indent=2)
    print("\n=== TRAM PILOT INGEST COMPLETE ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nOutput: {output.absolute()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("archive", help="Path to movement_data_export.tar.zst")
    parser.add_argument("--output", default="data/ingested", help="Output folder (must be empty/new)")
    parser.add_argument("--format", choices=("csv", "parquet"), default="csv", help="csv creates *.csv.gz; parquet needs pyarrow")
    parser.add_argument("--max-files", type=int, default=20000, help="Read first N JSON files (0 = entire archive)")
    parser.add_argument("--chunk-size", type=int, default=25000, help="Records per part (bounded RAM)")
    parser.add_argument("--max-json-mb", type=int, default=64, help="Skip oversized individual JSON files")
    parser.add_argument("--progress-every", type=int, default=100000)
    parser.add_argument("--include-raw", action="store_true", help="Copy full source JSON into each row (large outputs)")
    args = parser.parse_args()
    if args.max_files < 0 or args.chunk_size < 1 or args.max_json_mb < 1:
        parser.error("--max-files must be >= 0; chunk-size and max-json-mb must be >= 1")
    process(args)


if __name__ == "__main__":
    main()
