from datetime import datetime, timedelta, timezone
import unittest

from tram_pilot.analysis.analyzers import SensorFreshnessAnalyzer, SubsystemErrorAnalyzer
from tram_pilot.analysis.grouping import EpisodeBuilder, SegmentBuilder
from tram_pilot.analysis.models import Observation

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def observation(seconds, state="bad", track_id=1):
    return Observation(track_id, START + timedelta(seconds=seconds), "TRAM", "DEVICE", "A",
                       "subsystem_error", "m_danger_obj_status", state, True,
                       "Error" if state == "bad" else "ProperlyWork", {})


def segments(points, gap=30):
    builder = SegmentBuilder(gap)
    result = []
    for index, (seconds, state) in enumerate(points, 1):
        closed, _, _ = builder.feed(observation(seconds, state, index))
        if closed:
            result.append(closed)
    if final := builder.finish():
        result.append(final)
    return result


def episodes(parts, bridge=90):
    builder = EpisodeBuilder(bridge)
    result = []
    for part in parts:
        closed, _, _ = builder.feed(part)
        if closed:
            result.append(closed)
    if final := builder.finish():
        result.append(final)
    return result


class AnalyzerTests(unittest.TestCase):
    def track(self, values):
        return dict(id=1, observed_at=START, loco_number="TRAM", device_id="DEVICE",
                    cabin_type="A", attributes=values)

    def test_subsystems_are_separate_and_nonerror_off_is_unknown(self):
        values = {"m_danger_obj_status": "Error", "m_danger_obj_req": "yes",
                  "m_trafficlight_status": "TemporaryOff", "m_speed_limit_status": "TurnOff"}
        rows = SubsystemErrorAnalyzer().observe(self.track(values))
        self.assertEqual([r.state for r in rows], ["bad", "unknown", "unknown"])
        self.assertEqual(rows[0].context["m_danger_obj_req"], "yes")
        self.assertEqual(len({r.key for r in rows}), 3)

    def test_sensor_false_null_absent_true_are_distinct(self):
        analyzer = SensorFreshnessAnalyzer()
        values = {"leftImage": False}
        false = analyzer.observe(self.track(values))[1]
        values["leftImage"] = None
        null = analyzer.observe(self.track(values))[1]
        values.pop("leftImage")
        absent = analyzer.observe(self.track(values))[1]
        values["leftImage"] = True
        true = analyzer.observe(self.track(values))[1]
        self.assertEqual([(r.state, r.value_present) for r in (false, null, absent, true)],
                         [("bad", True), ("unknown", True), ("unknown", False), ("clear", True)])


class GroupingTests(unittest.TestCase):
    def test_30_and_90_boundaries(self):
        self.assertEqual(len(segments([(0, "bad"), (30, "bad")])), 1)
        parts = segments([(0, "bad"), (31, "bad"), (121, "bad")])
        self.assertEqual(len(parts), 3)
        self.assertEqual(len(episodes(parts)), 1)
        self.assertEqual(len(episodes(parts, bridge=89)), 2)

    def test_clearance_is_barrier_through_unknown(self):
        parts = segments([(0, "bad"), (5, "clear"), (10, "unknown"), (20, "bad")])
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0].close_reason, "observed_clearance")
        self.assertEqual(len(episodes(parts)), 2)

    def test_unknown_gap_is_not_confirmed_downtime(self):
        parts = segments([(0, "bad"), (10, "unknown"), (60, "bad"),
                          (65, "bad"), (70, "clear")])
        result = episodes(parts)
        self.assertEqual((len(parts), len(result)), (2, 1))
        self.assertEqual(result[0].bridged_gap_seconds, 60)
        self.assertEqual(result[0].close_reason, "observed_clearance")

    def test_unknown_snapshot_splits_observed_segment_even_inside_30_seconds(self):
        parts = segments([(0, "bad"), (10, "unknown"), (20, "bad")])
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0].close_reason, "unknown_state")
        self.assertEqual(len(episodes(parts)), 1)

    def test_sample_end_and_equal_timestamp(self):
        parts = segments([(0, "bad"), (0, "clear"), (0, "bad")])
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[-1].close_reason, "sample_end")
        self.assertEqual(len(episodes(parts)), 1)

    def test_same_timestamp_conflict_is_order_independent(self):
        for state in ("clear", "unknown"):
            with self.subTest(state=state):
                left = segments([(0, "bad"), (10, state), (10, "bad"), (20, "bad")])
                right = segments([(0, "bad"), (10, "bad"), (10, state), (20, "bad")])
                self.assertEqual([(s.bad_count, s.close_reason) for s in left],
                                 [(s.bad_count, s.close_reason) for s in right])
                self.assertEqual(len(episodes(left)), 1)

    def test_hundred_bad_snapshots(self):
        parts = segments([(i * 5, "bad") for i in range(100)])
        self.assertEqual((len(parts), parts[0].bad_count), (1, 100))
        self.assertEqual(len(episodes(parts)), 1)


if __name__ == "__main__":
    unittest.main()
