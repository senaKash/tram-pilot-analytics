#!/usr/bin/env python3
"""Tram Pilot: exploratory segments of subsystem errors and stale sensor data.

Reads previously ingested Track CSV.GZ parts (a folder or a ZIP) using only the
Python standard library. Does not unpack the original TAR.ZST archive.

Segments are OBSERVED runs, not independently verified hardware failures.
Data gaps or sample boundaries do not confirm that an error has ended.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import sqlite3
import sys
import zipfile
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SUBSYSTEMS = {
    "danger_obj": "m_danger_obj_status",
    "trafficlight": "m_trafficlight_status",
    "speed_limit": "m_speed_limit_status",
}
SENSORS = [
    # Device data freshness signals; missing values remain unknown.
    "ubloxGps", "leftImage", "minsEth", "roadModel", "dbwFbTram",
    "tramPlannerService", "multiImages",
]
EPISODE_COLS = [
    "category", "signal", "loco_number", "device_id", "cabin_type",
    "first_bad_at", "last_bad_at", "first_non_bad_at", "observed_span_seconds",
    "bad_snapshots", "adas_true_snapshots", "adas_false_snapshots",
    "first_source_file", "last_source_file", "close_reason",
    "left_censored", "right_censored", "prior_good_at", "clear_state",
    "start_statuses", "last_statuses",
]


def utc_ms_iso(ms: int | None) -> str:
    if ms is None:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds")


def normalize_bool(value: str | None) -> str:
    s = (value or "").strip().lower()
    return s if s in {"true", "false"} else ""


def normalize_state(value: str | None) -> str:
    return (value or "").strip()


@contextmanager
def track_parts(source: Path):
    if source.is_dir():
        files = sorted((source / "track").glob("part-*.csv.gz"))
        if not files:
            raise FileNotFoundError(f"Не найдены track/part-*.csv.gz внутри {source}")
        def iterator():
            for file in files:
                with gzip.open(file, "rt", encoding="utf-8", newline="") as stream:
                    yield str(file), csv.DictReader(stream)
        yield iterator()
    elif source.is_file() and zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as zip_file:
            names = sorted(n for n in zip_file.namelist()
                           if "/track/part-" in "/" + n and n.endswith(".csv.gz"))
            if not names:
                raise FileNotFoundError("В ZIP не найдены track/part-*.csv.gz")
            def iterator():
                for name in names:
                    with zip_file.open(name) as binary:
                        with gzip.GzipFile(fileobj=binary, mode="rb") as decoded:
                            with io.TextIOWrapper(decoded, encoding="utf-8", newline="") as stream:
                                yield name, csv.DictReader(stream)
            yield iterator()
    else:
        raise ValueError("--input: укажи каталог результата конвертации или ZIP с ним")


def initialize_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("""CREATE TABLE records (
        row_id INTEGER PRIMARY KEY,
        fingerprint TEXT NOT NULL UNIQUE,
        loco TEXT NOT NULL,
        device TEXT NOT NULL,
        cabin TEXT NOT NULL,
        ts INTEGER NOT NULL,
        src TEXT,
        adas TEXT,
        d TEXT, t TEXT, s TEXT,
        sensors_json TEXT NOT NULL
    )""")
    return con


def ingest(con: sqlite3.Connection, source: Path, max_rows: int) -> dict:
    raw_count = 0
    valid_count = 0
    duplicate = 0
    skipped_timestamp = 0
    skipped_identity = 0
    capped = False
    with track_parts(source) as parts:
        for partname, reader in parts:
            for row in reader:
                if raw_count >= max_rows:
                    capped = True
                    break
                raw_count += 1
                fieldset = sorted(k for k in row if k not in {"source_file", "event_index"})
                if row.get("event_type") != "Track":
                    continue
                try:
                    ts = int(row.get("timestamp_utc_ms", ""))
                except (TypeError, ValueError):
                    skipped_timestamp += 1
                    continue
                loco, device = row.get("loco_number", ""), row.get("device_id", "")
                if not loco or not device:
                    skipped_identity += 1
                    continue
                # Source filename and position in an events array are provenance.
                # Same timestamp alone is not enough to discard a record.
                digest = hashlib.blake2b(
                    json.dumps([(k, row.get(k)) for k in fieldset],
                               ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                    digest_size=16,
                ).hexdigest()
                statuses = [normalize_state(row.get(v)) for v in SUBSYSTEMS.values()]
                sensor_states = {k: normalize_bool(row.get(k)) for k in SENSORS}
                before = con.total_changes
                con.execute("""INSERT OR IGNORE INTO records
                    (fingerprint, loco, device, cabin, ts, src, adas, d, t, s, sensors_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (digest, loco, device, row.get("cabin_type", ""), ts,
                     row.get("source_file", ""), normalize_bool(row.get("is_adas_button_on")),
                     *statuses, json.dumps(sensor_states, separators=(",", ":"))))
                if con.total_changes == before:
                    duplicate += 1
                else:
                    valid_count += 1
                if raw_count % 50000 == 0:
                    con.commit()
                    print(f"Введено Track-строк: {raw_count:,}", file=sys.stderr)
            if capped:
                break
    con.commit()
    con.execute("CREATE INDEX records_order ON records(loco, device, cabin, ts, row_id)")
    con.commit()
    return {
        "input_track_rows": raw_count,
        "unique_track_rows": valid_count,
        "exact_duplicates_skipped": duplicate,
        "missing_timestamp_skipped": skipped_timestamp,
        "missing_identity_skipped": skipped_identity,
        "max_rows": max_rows,
        "reached_row_limit": capped,
    }


class Segment:
    def __init__(self, category, signal, key, row, prior, left_censored):
        self.category = category
        self.signal = signal
        self.loco, self.device, self.cabin = key
        self.start = self.end = row["ts"]
        self.first_source = self.last_source = row["src"]
        self.n = 1
        self.adas_true = (row["adas"] == "true")
        self.adas_false = (row["adas"] == "false")
        self.left_censored = left_censored
        self.prior_good_at = prior["ts"] if prior is not None else None
        self.start_statuses = "/".join(row["statuses"])
        self.last_statuses = self.start_statuses

    def extend(self, row):
        self.end = row["ts"]
        self.n += 1
        self.last_source = row["src"]
        self.last_statuses = "/".join(row["statuses"])
        self.adas_true += (row["adas"] == "true")
        self.adas_false += (row["adas"] == "false")

    def to_dict(self, close_reason, right_censored, good_row=None, clear_state=""):
        return dict(zip(EPISODE_COLS, [
            self.category, self.signal, self.loco, self.device, self.cabin,
            utc_ms_iso(self.start), utc_ms_iso(self.end),
            utc_ms_iso(good_row["ts"] if good_row else None),
            round((self.end - self.start) / 1000, 3),
            self.n, int(self.adas_true), int(self.adas_false),
            self.first_source, self.last_source, close_reason,
            bool(self.left_censored), bool(right_censored),
            utc_ms_iso(self.prior_good_at), clear_state,
            self.start_statuses, self.last_statuses,
        ]))


def analyze(con: sqlite3.Connection, output: Path, gap_ms: int):
    cur = con.execute("SELECT loco, device, cabin, ts, src, adas, d, t, s, sensors_json FROM records ORDER BY loco, device, cabin, ts, row_id")
    filenames = {
        "subsystem_error": output / "subsystem_error_segments.csv",
        "sensor_stale": output / "sensor_stale_segments.csv",
    }
    handles = {}
    writers = {}
    counters = Counter()
    by_tram = Counter()
    affected_groups = set()
    all_groups = set()
    active = {}
    previous = {}
    last_good = {}
    prev_key = None

    def write_segment(seg, reason, censored, good=None, state=""):
        row = seg.to_dict(reason, censored, good, state)
        writers[seg.category].writerow(row)
        counters["segments_" + seg.category] += 1
        counters["closed_" + reason] += 1
        by_tram[seg.loco] += (seg.category == "subsystem_error")
        affected_groups.add((seg.loco, seg.device, seg.cabin, seg.category, seg.signal))

    def finish_active(reason="sample_end"):
        for seg in active.values():
            write_segment(seg, reason, True)
        active.clear()
        previous.clear()
        last_good.clear()

    try:
        for kind, filename in filenames.items():
            handles[kind] = filename.open("w", encoding="utf-8", newline="")
            writers[kind] = csv.DictWriter(handles[kind], fieldnames=EPISODE_COLS)
            writers[kind].writeheader()
        for loco, device, cabin, ts, src, adas, d, t, s, sensors_json in cur:
            key = (loco, device, cabin)
            if prev_key is not None and key != prev_key:
                finish_active()
            prev_key = key
            all_groups.add(key)
            states = (d, t, s)
            row = dict(ts=ts, src=src, adas=adas, statuses=states)
            sensors = json.loads(sensors_json)
            any_error = "Error" in states
            if any_error:
                counters["error_snapshot_rows"] += 1
                if adas == "true":
                    counters["error_snapshots_adas_on"] += 1
                elif adas == "false":
                    counters["error_snapshots_adas_off"] += 1
            if len(set(states)) != 1:
                counters["mixed_status_snapshot_rows"] += 1
            signals = [("subsystem_error", "any_subsystem", "bad" if any_error else
                       "good" if all(states) else "unknown", "/".join(states))]
            for name in SENSORS:
                st = sensors.get(name, "")
                signals.append(("sensor_stale", name,
                                "bad" if st == "false" else "good" if st == "true" else "unknown",
                                st))
                if st == "false":
                    counters["stale_rows_" + name] += 1
            for category, signal, condition, state_text in signals:
                sigkey = (category, signal)
                seg = active.get(sigkey)
                prior = previous.get(sigkey)
                gap = ts - prior["ts"] if prior is not None else None
                is_gap = gap is not None and gap > gap_ms
                if seg is not None and is_gap and condition != "good":
                    write_segment(seg, "telemetry_gap", True)
                    del active[sigkey]
                    seg = None
                if condition == "bad":
                    if seg is None:
                        previous_good = prior if prior and not is_gap and prior["condition"] == "good" else None
                        active[sigkey] = Segment(category, signal, key, row, last_good.get(sigkey),
                                                 left_censored=(previous_good is None))
                    else:
                        seg.extend(row)
                else:
                    if seg is not None:
                        if condition == "good":
                            clear_state = "all_properly_work" if category == "subsystem_error" and all(v == "ProperlyWork" for v in states) else (
                                "non_error_status" if category == "subsystem_error" else "data_fresh")
                            write_segment(seg, "observed_non_bad", False, row, clear_state)
                        else:
                            write_segment(seg, "unknown_status", True)
                        del active[sigkey]
                if condition == "good":
                    last_good[sigkey] = {"ts": ts}
                previous[sigkey] = {"ts": ts, "condition": condition}
        finish_active()
    finally:
        for f in handles.values():
            f.close()
    summary = {
        "scope": "Exploratory observed segments from Track only; NOT verified distinct failures or repairs.",
        "gap_seconds": gap_ms / 1000,
        "device_groups": len(all_groups),
        "loco_numbers": len({x[0] for x in all_groups}),
        "error_snapshot_rows": counters["error_snapshot_rows"],
        "error_snapshots_adas_on": counters["error_snapshots_adas_on"],
        "error_snapshots_adas_off": counters["error_snapshots_adas_off"],
        "mixed_status_snapshot_rows": counters["mixed_status_snapshot_rows"],
        "observed_error_segments": counters["segments_subsystem_error"],
        "observed_sensor_stale_segments": counters["segments_sensor_stale"],
        "segments_by_tram": dict(sorted(by_tram.items(), key=lambda x: (-x[1], x[0]))),
        "sensor_stale_snapshot_rows": {x: counters["stale_rows_" + x] for x in SENSORS},
        "segment_close_reasons": {k[len("closed_"):]: v for k, v in counters.items() if k.startswith("closed_")},
        "interpretation": [
            "Error from any subsystem is grouped as a common subsystem_error signal to avoid triple counting.",
            "Each (loco_number, device_id, cabin_type) is processed separately.",
            "Exact duplicate Track rows are removed if CSV fields except source_file and event_index match.",
            "telemetry_gap splits observed segments but DOES NOT prove error recovery or a new physical failure.",
            "first_bad_at and last_bad_at are observed timestamps, NOT actual onset or recovery times.",
            "observed_span_seconds is the interval between first and last bad observation, NOT confirmed downtime.",
            "A closing non-error value proves only the Error flag cleared, NOT that the subsystem recovered to full operation.",
            "Absent sensor fields count as unknown, never as sensor stale.",
            "Undocumented flags (e.g. t25Front, tramSlaveVisor, odoFbTram) are NOT classified until their meanings are confirmed.",
        ],
    }
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Каталог результата ingest или ZIP с ним")
    parser.add_argument("--output", default="data/diagnostics")
    parser.add_argument("--gap-seconds", type=float, default=30.0,
                        help="Макс. разрыв между соседними наблюдениями одного устройства (по умолчанию 30)")
    parser.add_argument("--max-rows", type=int, default=250000,
                        help="Ограничение для этапа прототипирования (по умолчанию 250000)")
    args = parser.parse_args()
    if args.gap_seconds <= 0 or args.max_rows <= 0:
        parser.error("--gap-seconds и --max-rows должны быть положительными")
    source = Path(args.input)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    dbfile = output / "_working_sort.sqlite"
    if dbfile.exists():
        parser.error(f"Временная БД {dbfile} уже существует; перенеси/удали её перед повторным запуском")
    try:
        con = initialize_db(dbfile)
        try:
            intake = ingest(con, source, args.max_rows)
            result = analyze(con, output, round(args.gap_seconds * 1000))
            result["input"] = str(source)
            result["source_quality"] = intake
        finally:
            con.close()
    finally:
        dbfile.unlink(missing_ok=True)
    (output / "summary.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\n=== TRAM PILOT DIAGNOSTIC SEGMENTS ===")
    print(json.dumps({k: result[k] for k in ["input", "loco_numbers", "device_groups", "error_snapshot_rows", "observed_error_segments", "observed_sensor_stale_segments", "segment_close_reasons", "source_quality"]}, indent=2, ensure_ascii=False))
    print("\nРезультат:", output.resolve())
    print("ВНИМАНИЕ: сегменты и наблюдаемые интервалы НЕ равны подтверждённым отказам и времени простоя.")


if __name__ == "__main__":
    main()
