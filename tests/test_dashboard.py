"""Проверки границ выделения без запуска Streamlit и PostgreSQL."""
from datetime import datetime, timezone
import unittest

from tram_pilot.services.analytics import TimeWindow, bucket_seconds, parse_plot_range, RangeHistory


class TimeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.outer = TimeWindow(datetime(2026, 9, 7, 0, tzinfo=timezone.utc),
                                datetime(2026, 9, 7, 2, tzinfo=timezone.utc))

    def test_drag_in_empty_area_uses_coordinates_and_moscow_time(self):
        selected = parse_plot_range({"start": "2026-09-07 03:15:00",
                                     "end": "2026-09-07 03:45:00"}, self.outer)
        self.assertEqual(selected.start, datetime(2026, 9, 7, 0, 15, tzinfo=timezone.utc))
        self.assertEqual(selected.end, datetime(2026, 9, 7, 0, 45, tzinfo=timezone.utc))

    def test_selection_is_clipped_and_zero_length_rejected(self):
        selected = parse_plot_range({"start": "2026-09-07 02:00:00",
                                     "end": "2026-09-07 04:30:00"}, self.outer)
        self.assertEqual(selected.start, self.outer.start)
        self.assertEqual(selected.end, datetime(2026, 9, 7, 1, 30, tzinfo=timezone.utc))
        self.assertIsNone(parse_plot_range({"start": "2026-09-07 03:00:00",
                                            "end": "2026-09-07 03:00:00"}, self.outer))
        self.assertIsNone(parse_plot_range({"reset": True}, self.outer))

    def test_bucket_remains_bounded(self):
        self.assertGreaterEqual(bucket_seconds(self.outer), 60)

class NavigationTests(unittest.TestCase):
    def test_back_restores_bucket_rule_and_sessions_are_independent(self):
        outer = TimeWindow(datetime(2026,9,7,0,tzinfo=timezone.utc),
                           datetime(2026,9,7,1,tzinfo=timezone.utc))
        first,second = RangeHistory(outer),RangeHistory(outer)
        bucket = TimeWindow(outer.start,outer.start.replace(minute=1))
        zoom = TimeWindow(outer.start.replace(second=10),outer.start.replace(second=20))
        first.select(bucket,True)
        first.select(zoom)
        first.back()
        self.assertEqual(first.current.window,bucket)
        self.assertTrue(first.current.starts_only)
        self.assertEqual(second.current.window,outer)
        first.reset()
        self.assertEqual(first.current.window,outer)
        first.back()
        self.assertTrue(first.current.starts_only)

    def test_minute_buckets_for_quarter_hour(self):
        outer = TimeWindow(datetime(2026,9,7,0,tzinfo=timezone.utc),
                           datetime(2026,9,7,0,15,tzinfo=timezone.utc))
        self.assertEqual(bucket_seconds(outer),60)

    def test_bars_keep_count_and_clip_partial_bucket(self):
        from dashboard.components.timeline import history_traces
        from datetime import timedelta
        start = datetime(2026,9,7,0,0,30,tzinfo=timezone.utc)
        window = TimeWindow(start,start+timedelta(minutes=1))
        data = dict(bucket_seconds=60,histogram=[
            dict(bucket=start-timedelta(seconds=30),category="subsystem_error",episodes=3)],
            timeline_states=[],timeline_segments=[])
        traces = history_traces(data,window,[])
        bar = next(t for t in traces if t["type"]=="bar")
        self.assertEqual(sum(bar["y"]),3)
        self.assertEqual(bar["customdata"][0][0],"2026-09-07T03:00:30")
        gaps = [t for t in traces if t.get("name")=="Нет наблюдений"]
        self.assertEqual(len(gaps),1)
        self.assertFalse(any(t.get("name")=="Признак снят" for t in traces))


if __name__ == "__main__":
    unittest.main()
