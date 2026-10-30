import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from scripts import tram_pilot_ingest as ingest
from scripts import tram_pilot_incidents as incidents
from scripts import tram_pilot_episodes as episodes
from scripts._archive import zstd
from scripts.run_example import make_archive

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
HAS_ZSTD = zstd is not None or shutil.which("zstd") is not None


def record(seconds, state="Error", **fields):
    row = {
        "timestamp": (BASE + timedelta(seconds=seconds)).isoformat(),
        "event_type": "Track", "loco_number": "DEMO-001", "device_id": "DEMO-UNIT-01", "cabin_type": "A",
        **dict.fromkeys(ingest.STATUS_KEYS, state),
    }
    row.update(fields)
    return row


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def run_records(records):
    """Exercise normalization, CSV.GZ, deduplication, sorting and both analyzers."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        writer = ingest.Writer(root / "ingested", "track", "csv", 2)
        for index, row in enumerate(records):
            writer.add(ingest.normalize(row, f"synthetic-{index}.json", index, "track", False))
        writer.flush()
        output = root / "diagnostics"
        output.mkdir()
        con = incidents.initialize_db(Path(":memory:"))
        try:
            quality = incidents.ingest(con, root / "ingested", 10000)
            summary = incidents.analyze(con, output, 30000)
        finally:
            con.close()
        errors = read_csv(output / "subsystem_error_segments.csv")
        sensors = read_csv(output / "sensor_stale_segments.csv")
        candidates, links = episodes.assemble(errors, 90, "ERR")
        stale, stale_links = episodes.assemble(sensors, 90, "STA")
        return quality, summary, errors, sensors, candidates, links, stale, stale_links


class NormalizationTests(unittest.TestCase):
    def test_booleans_preserve_unknown(self):
        for value in [True, " TRUE ", "1", 1]:
            self.assertIs(ingest.as_bool(value), True)
        for value in [False, " false ", "0", 0]:
            self.assertIs(ingest.as_bool(value), False)
        for value in [None, "", "unknown", 2]:
            self.assertIsNone(ingest.as_bool(value))

    def test_numbers(self):
        self.assertEqual(ingest.as_int("12"), 12)
        self.assertEqual(ingest.as_int(12.0), 12)
        self.assertIsNone(ingest.as_int(12.5))
        self.assertIsNone(ingest.as_int(True))
        self.assertIsNone(ingest.as_int(""))
        self.assertEqual(ingest.as_float("12.5"), 12.5)
        for value in [True, "NaN", "Infinity", "oops"]:
            self.assertIsNone(ingest.as_float(value))

    def test_timezone(self):
        self.assertEqual(ingest.millis_utc("2026-01-01T03:00:00+03:00"),
                         ingest.millis_utc("2026-01-01T00:00:00Z"))
        self.assertIsNone(ingest.millis_utc("2026-01-01T00:00:00"))
        self.assertIsNone(ingest.millis_utc("invalid"))

    def test_normalize_nested_fields_and_multi_images(self):
        row = ingest.normalize(record(0, speed="12.5", multiImages="false",
            m_report_response={"is_response": "true", "avg_delay_ms": "2.5"}), "demo.json", 2, "track", False)
        self.assertEqual(row["speed"], 12.5)
        self.assertIs(row["multiImages"], False)
        self.assertIsNone(row["ubloxGps"])
        self.assertIs(row["m_report_response_is_response"], True)
        self.assertEqual(row["m_report_response_avg_delay_ms"], 2.5)
        self.assertEqual(row["event_index"], 2)
        self.assertIsNone(row["raw_json"])


class SegmentationTests(unittest.TestCase):
    def test_duplicate_different_source_and_event_index(self):
        quality, summary, segments, *_ = run_records([record(0), record(0), record(10)])
        self.assertEqual(quality["exact_duplicates_skipped"], 1)
        self.assertEqual(summary["error_snapshot_rows"], 2)
        self.assertEqual(segments[0]["bad_snapshots"], "2")

    def test_same_timestamp_different_values_are_kept(self):
        quality, *_ = run_records([record(0, speed="1"), record(0, speed="2")])
        self.assertEqual(quality["unique_track_rows"], 2)

    def test_each_identity_dimension_is_separate(self):
        rows = [record(0), record(0, device_id="DEMO-UNIT-02"),
                record(0, cabin_type="B"), record(0, loco_number="DEMO-002")]
        _, summary, segments, _, candidates, *_ = run_records(rows)
        self.assertEqual(summary["device_groups"], 4)
        self.assertEqual(len(segments), 4)
        self.assertEqual(len(candidates), 4)

    def test_out_of_order_rows_are_sorted(self):
        _, _, segments, *_ = run_records([record(20), record(0), record(10)])
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["bad_snapshots"], "3")
        self.assertEqual(segments[0]["observed_span_seconds"], "20.0")

    def test_30_seconds_inclusive_and_longer_splits(self):
        _, _, segments, _, candidates, *_ = run_records([record(0), record(30), record(60.001)])
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["bad_snapshots"], "2")
        self.assertEqual(segments[0]["close_reason"], "telemetry_gap")
        self.assertEqual(candidates[0]["bridged_gap_count"], 1)

    def test_clearance_prevents_merging(self):
        _, _, segments, _, candidates, *_ = run_records([record(0), record(10, "ProperlyWork"), record(20)])
        self.assertEqual(segments[0]["close_reason"], "observed_non_bad")
        self.assertEqual(len(candidates), 2)

    def test_clearance_after_long_gap_prevents_merging(self):
        _, _, segments, _, candidates, *_ = run_records([record(0), record(40, "ProperlyWork"), record(80)])
        self.assertEqual(segments[0]["close_reason"], "observed_non_bad")
        self.assertEqual(len(candidates), 2)

    def test_clearance_survives_intervening_unknowns_and_gaps(self):
        rows = [record(0), record(40, None), record(50, "ProperlyWork"), record(60, None), record(85)]
        _, _, segments, _, candidates, *_ = run_records(rows)
        self.assertEqual(segments[0]["close_reason"], "telemetry_gap")
        self.assertEqual(segments[1]["left_censored"], "True")
        self.assertTrue(segments[1]["prior_good_at"])
        self.assertEqual(len(candidates), 2)

    def test_sensor_freshness_also_prevents_merging(self):
        rows = [record(0, None, ubloxGps="false"), record(40, None),
                record(50, None, ubloxGps="true"), record(60, None), record(85, None, ubloxGps="false")]
        *_, candidates, _links = run_records(rows)
        self.assertEqual(len(candidates), 2)

    def test_missing_sensor_is_unknown_not_stale(self):
        _, _, _, sensors, *_ = run_records([record(0), record(10, ubloxGps="false"), record(20)])
        self.assertEqual(len(sensors), 1)
        self.assertEqual(sensors[0]["close_reason"], "unknown_status")
        self.assertEqual(sensors[0]["right_censored"], "True")

    def test_unknown_subsystem_not_reported_as_cleared(self):
        _, _, segments, *_ = run_records([record(0), record(10, None)])
        self.assertEqual(segments[0]["close_reason"], "unknown_status")
        self.assertEqual(segments[0]["first_non_bad_at"], "")

    def test_multi_images_false_is_detected(self):
        _, _, _, sensors, *_ = run_records([record(0, None, multiImages="false")])
        self.assertEqual(sensors[0]["signal"], "multiImages")

    def test_invalid_identity_and_time_are_skipped(self):
        rows = [record(0), record(1, device_id=""), record(2, timestamp="invalid")]
        quality, *_ = run_records(rows)
        self.assertEqual(quality["unique_track_rows"], 1)
        self.assertEqual(quality["missing_identity_skipped"], 1)
        self.assertEqual(quality["missing_timestamp_skipped"], 1)

    def test_gaps_are_uncertain_not_downtime(self):
        _, _, _, _, candidates, *_ = run_records([record(0), record(60)])
        self.assertEqual(candidates[0]["bridged_gap_seconds"], 60)
        self.assertEqual(candidates[0]["sum_segment_bad_spans_seconds"], 0)
        self.assertEqual(candidates[0]["evidence_class"], "POSSIBLE_CONTINUATION_ACROSS_MISSING_DATA")

    def test_90_second_bridge_is_inclusive(self):
        for seconds, count in [(90, 1), (90.001, 2)]:
            with self.subTest(seconds=seconds):
                _, _, _, _, candidates, *_ = run_records([record(0), record(seconds)])
                self.assertEqual(len(candidates), count)

    def test_all_segments_and_counts_are_preserved(self):
        _, _, segments, _, candidates, links, *_ = run_records([record(0), record(60), record(70, "ProperlyWork"), record(80)])
        self.assertEqual(len(links), len(segments))
        self.assertEqual(sum(x["bad_snapshots"] for x in candidates), 3)
        self.assertEqual({x["source_segment_number"] for x in links}, set(range(1, len(segments) + 1)))


@unittest.skipUnless(HAS_ZSTD, "Requires Python 3.14+ or zstd on PATH")
class ArchiveIntegrationTests(unittest.TestCase):
    def cli(self, script, *args, succeeds=True):
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / script), *map(str, args)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                env={**__import__('os').environ, "PYTHONIOENCODING": "utf-8"}, timeout=30)
        if succeeds:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def test_two_json_formats_and_complete_pipeline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "synthetic.tar.zst"
            make_archive(archive)
            self.cli("tram_pilot_probe.py", archive, "--output", root / "profile")
            self.cli("tram_pilot_ingest.py", archive, "--output", root / "ingested", "--chunk-size", 3)
            self.cli("tram_pilot_incidents.py", "--input", root / "ingested", "--output", root / "diagnostics")
            self.cli("tram_pilot_episodes.py", "--input", root / "diagnostics", "--output", root / "episodes")
            profile = json.loads((root / "profile/tram_pilot_profile.json").read_text(encoding="utf-8"))
            intake = json.loads((root / "ingested/summary.json").read_text(encoding="utf-8"))
            quality = json.loads((root / "diagnostics/summary.json").read_text(encoding="utf-8"))
            summary = json.loads((root / "episodes/summary.json").read_text(encoding="utf-8"))
            self.assertEqual(profile["root_shapes"], {"direct_event": 1, "events_container": 1})
            self.assertEqual(intake["track_rows"], 21)
            self.assertEqual(intake["event_rows"], 3)
            self.assertEqual(quality["source_quality"]["exact_duplicates_skipped"], 1)
            self.assertEqual(summary["candidate_error_episodes"], 6)
            self.assertEqual(summary["candidate_sensor_episodes"], 3)
            error_segments = read_csv(root / "diagnostics/subsystem_error_segments.csv")
            sensor_segments = read_csv(root / "diagnostics/sensor_stale_segments.csv")
            errors = read_csv(root / "episodes/candidate_error_episodes.csv")
            sensors = read_csv(root / "episodes/candidate_sensor_episodes.csv")
            links = read_csv(root / "episodes/episode_segment_links.csv")
            self.assertEqual(len(error_segments), 8)
            self.assertEqual(len(sensor_segments), 5)
            self.assertEqual(len(links), len(error_segments) + len(sensor_segments))
            self.assertEqual({r["episode_id"] for r in links},
                             {r["episode_id"] for r in errors + sensors})
            self.assertEqual(sum(int(r["bad_snapshots"]) for r in errors),
                             sum(int(r["bad_snapshots"]) for r in error_segments))
            self.assertEqual(sum(int(r["bad_snapshots"]) for r in sensors),
                             sum(int(r["bad_snapshots"]) for r in sensor_segments))

    def test_limited_scan_and_inspect(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "synthetic.tar.zst"
            make_archive(archive)
            self.cli("tram_pilot_probe.py", archive, "--limit", 1, "--output", root / "profile")
            self.cli("tram_pilot_ingest.py", archive, "--max-files", 1, "--output", root / "ingested")
            summary = json.loads((root / "ingested/summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["files_read"], 1)
            self.assertFalse(summary["complete_archive_scan"])
            result = self.cli("tram_pilot_probe.py", archive, "--inspect-file", "direct_track.json")
            self.assertIn("DEMO-UNIT-01", result.stdout)

    def test_corrupt_archive_does_not_report_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "broken.tar.zst"
            archive.write_bytes(b"not a zstandard frame")
            self.cli("tram_pilot_probe.py", archive, "--output", root / "profile", succeeds=False)
            self.cli("tram_pilot_ingest.py", archive, "--output", root / "ingested", succeeds=False)
            self.assertFalse((root / "profile/tram_pilot_profile.json").exists())
            self.assertFalse((root / "ingested/summary.json").exists())


if __name__ == "__main__":
    unittest.main()
