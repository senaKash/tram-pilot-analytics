from copy import deepcopy
from datetime import timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tram_pilot.database.connection import DatabaseConfig, load_env_file
from tram_pilot.normalize import load_demo, normalize_event, normalize_track

ROOT = Path(__file__).resolve().parents[1]


def example():
    return json.loads((ROOT / "examples/direct_track.json").read_text(encoding="utf-8"))


class TrackDatabaseNormalizationTests(unittest.TestCase):
    def test_events_keep_type_content_and_position(self):
        source = example()
        for kind in ("Warn", "Brake", "OverSpeed"):
            source["event_type"] = kind
            first = normalize_event(source, "demo", "container.json", 2)
            second = normalize_event(source, "demo", "container.json", 3)
            self.assertEqual(first.event_type, kind)
            self.assertIs(first.payload["leftImage"], False)
            self.assertNotEqual(first.record_hash, second.record_hash)
        source["event_type"] = "Error"
        with self.assertRaises(ValueError):
            normalize_event(source, "demo", "container.json", 2)
        source["event_type"] = []
        with self.assertRaises(ValueError):
            normalize_event(source, "demo", "container.json", 2)

        source["event_type"] = "Warn"
        false = normalize_event(source, "demo", "container.json", 2)
        source["leftImage"] = None
        null = normalize_event(source, "demo", "container.json", 2)
        source.pop("leftImage")
        absent = normalize_event(source, "demo", "container.json", 2)
        self.assertIs(false.payload["leftImage"], False)
        self.assertIsNone(null.payload["leftImage"])
        self.assertNotIn("leftImage", absent.payload)
        self.assertEqual(len({r.record_hash for r in (false, null, absent)}), 3)

    def test_uses_observed_fields_and_existing_type_rules(self):
        source = example()
        record = normalize_track(source, "direct_track.json")
        self.assertEqual(record.observed_at.tzinfo, timezone.utc)
        self.assertEqual(record.loco_number, source["loco_number"])
        self.assertEqual(record.attributes["speed"], 12.5)
        self.assertIs(record.attributes["leftImage"], False)
        self.assertIs(record.attributes["multiImages"], True)
        self.assertEqual(set(record.attributes), set(source) - {
            "timestamp", "loco_number", "device_id", "cabin_type"})

    def test_false_null_and_absent_remain_different(self):
        source = example()
        false = normalize_track(source, "false.json")
        source["leftImage"] = None
        null = normalize_track(source, "null.json")
        source.pop("leftImage")
        absent = normalize_track(source, "absent.json")
        self.assertIs(false.attributes["leftImage"], False)
        self.assertIsNone(null.attributes["leftImage"])
        self.assertNotIn("leftImage", absent.attributes)
        self.assertEqual(len({r.record_hash for r in [false, null, absent]}), 3)

    def test_invalid_boolean_does_not_silently_become_null(self):
        source = example()
        source["leftImage"] = "invalid"
        with self.assertRaisesRegex(ValueError, "leftImage"):
            normalize_track(source, "invalid.json")

    def test_invalid_number_rejected(self):
        for value in ["NaN", "Infinity", True, "invalid"]:
            with self.subTest(value=value):
                source = example()
                source["speed"] = value
                with self.assertRaisesRegex(ValueError, "speed"):
                    normalize_track(source, "invalid.json")

    def test_utc_equivalence_and_submillisecond_precision(self):
        source = example()
        source["timestamp"] = "2026-01-01T12:00:00.123456+03:00"
        local = normalize_track(source, "local.json")
        source["timestamp"] = "2026-01-01T09:00:00.123456Z"
        utc = normalize_track(source, "utc.json")
        self.assertEqual(local.record_hash, utc.record_hash)
        self.assertEqual(utc.observed_at.microsecond, 123456)

    def test_missing_timezone_and_identity_rejected(self):
        for field, value in [("timestamp", "2026-01-01T09:00:00"), ("device_id", None),
                             ("loco_number", ""), ("cabin_type", 0)]:
            with self.subTest(field=field):
                source = example()
                source[field] = value
                with self.assertRaises(ValueError):
                    normalize_track(source, "invalid.json")

    def test_unknown_present_fields_preserved_without_mutating_input(self):
        source = example()
        source["extra_signal"] = {"value": None, "fresh": False}
        before = deepcopy(source)
        record = normalize_track(source, "example.json")
        self.assertEqual(record.attributes["extra_signal"], source["extra_signal"])
        self.assertEqual(source, before)

    def test_hash_ignores_key_order_source_file_and_event_position(self):
        source = example()
        first = normalize_track(source, "a.json", 0)
        second = normalize_track(dict(reversed(list(source.items()))), "b.json", 9)
        self.assertEqual(first.record_hash, second.record_hash)

    def test_equivalent_normalized_values_have_same_hash(self):
        source = example()
        first = normalize_track(source, "a.json")
        source.update(leftImage=False, speed=12.5)
        self.assertEqual(first.record_hash, normalize_track(source, "b.json").record_hash)

    def test_equal_time_conflicts_and_different_devices_are_preserved(self):
        source = example()
        first = normalize_track(source, "a.json")
        source["m_danger_obj_status"] = "ProperlyWork"
        self.assertNotEqual(first.record_hash, normalize_track(source, "b.json").record_hash)
        for field in ["device_id", "cabin_type", "loco_number"]:
            changed = example()
            changed[field] += "-OTHER"
            self.assertNotEqual(first.record_hash, normalize_track(changed, "c.json").record_hash)

    def test_both_formats_and_repeat_loading(self):
        first, skipped = load_demo(ROOT / "examples")
        second, _ = load_demo(ROOT / "examples")
        self.assertEqual(len(first), 21)
        self.assertEqual(skipped, 3)
        self.assertEqual(len({r.record_hash for r in first}), 20)
        self.assertEqual([r.record_hash for r in first], [r.record_hash for r in second])
        self.assertTrue(any(r.source_file == "events_container.json" and r.event_index > 0 for r in first))

    def test_demo_limits_and_malformed_input(self):
        with self.assertRaisesRegex(ValueError, "лимит"):
            load_demo(ROOT / "examples", max_records=1)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "invalid.json"
            for content in ['{"events": {}}', '[1]', '{"event_type":"Track","speed":NaN}']:
                path.write_text(content, encoding="utf-8")
                with self.subTest(content=content), self.assertRaises(ValueError):
                    load_demo(path)


class DatabaseConfigTests(unittest.TestCase):
    def test_env_file_and_exported_variable_precedence(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"PGPORT": "5544"}, clear=True):
            path = Path(temporary) / ".env"
            path.write_text("# local\nPGPORT=5432\nPOSTGRES_PASSWORD='unit-test-only'\n", encoding="utf-8")
            load_env_file(path)
            config = DatabaseConfig.from_env()
            self.assertEqual(config.port, 5544)
            self.assertEqual(config.password, "unit-test-only")
            self.assertNotIn("unit-test-only", repr(config))

    def test_empty_password_rejected(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "POSTGRES_PASSWORD"):
            DatabaseConfig.from_env()

    def test_invalid_ports_rejected(self):
        for port in ["abc", "0", "65536"]:
            with self.subTest(port=port), patch.dict(os.environ, {
                "POSTGRES_PASSWORD": "unit-test-only", "PGPORT": port
            }, clear=True), self.assertRaisesRegex(ValueError, "PGPORT"):
                DatabaseConfig.from_env()

    def test_missing_env_file_is_optional(self):
        with tempfile.TemporaryDirectory() as temporary:
            load_env_file(Path(temporary) / "missing.env")


if __name__ == "__main__":
    unittest.main()
