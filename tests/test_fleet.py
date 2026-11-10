"""Проверки состава ячеек парка без PostgreSQL и Streamlit."""
from datetime import datetime, timedelta, timezone
from unittest import TestCase

from tram_pilot.services.analytics import TimeWindow
from tram_pilot.services.fleet import FleetCell, FleetService, fleet_bucket_seconds


class FleetCellTests(TestCase):
    def setUp(self):
        self.start = datetime(2026,9,6,20,59,tzinfo=timezone.utc)
        self.window = TimeWindow(self.start,self.start+timedelta(minutes=2))

    def cell(self, error, freshness, tracks=100):
        return FleetCell("DEMO-0312",self.window,error,freshness,tracks)

    def test_proportions_and_blue_above_red(self):
        cell = self.cell(13,7)
        self.assertEqual(cell.red_percent,65)
        self.assertEqual(cell.blue_percent,35)
        self.assertEqual(cell.fill_parts,(("#2563EB",35),("#D12D38",65)))
        self.assertIn("Всего новых эпизодов: 20",cell.tooltip())

    def test_single_categories(self):
        self.assertEqual(self.cell(10,0).fill_parts,(("#D12D38",100),))
        self.assertEqual(self.cell(0,10).fill_parts,(("#2563EB",100),))

    def test_no_new_episode_and_no_telemetry_are_different(self):
        quiet,missing = self.cell(0,0),self.cell(0,0,0)
        self.assertEqual(quiet.kind,"quiet")
        self.assertEqual(quiet.fill_parts,(("#FFFFFF",100),))
        self.assertEqual(missing.kind,"missing")
        self.assertEqual(missing.fill_parts,(("#D1D5DB",100),))
        self.assertIsNone(quiet.red_percent)
        self.assertIsNone(missing.blue_percent)
        self.assertIn("Нет наблюдений Track",missing.tooltip())
        self.assertNotIn("исправно",quiet.tooltip())

    def test_tooltip_crosses_moscow_midnight(self):
        text = self.cell(1,1).tooltip()
        self.assertIn("06.09.2026 23:59:00",text)
        self.assertIn("07.09.2026 00:01:00",text)

    def test_fraction_is_not_rounded_for_rendering(self):
        cell = self.cell(1,2)
        self.assertAlmostEqual(cell.red_percent+cell.blue_percent,100)
        self.assertEqual(sum(part[1] for part in cell.fill_parts),100)

    def test_inconsistent_or_negative_counters_are_rejected(self):
        with self.assertRaises(ValueError):
            self.cell(1,0,0)
        with self.assertRaises(ValueError):
            self.cell(-1,0)

    def test_adaptive_buckets_are_bounded(self):
        for seconds in (1,60,900,86400,86400*365):
            window = TimeWindow(self.start,self.start+timedelta(seconds=seconds))
            step = fleet_bucket_seconds(window)
            self.assertGreaterEqual(step,60)
            self.assertLessEqual((seconds+step-1)//step,24)
        hundred_minutes = TimeWindow(self.start,self.start+timedelta(minutes=100))
        self.assertEqual(fleet_bucket_seconds(hundred_minutes),300)

    def test_actual_coverage_includes_last_timestamp(self):
        class Queries:
            def coverage(inner, run_id):
                return {"first_at":self.start,"last_at":self.window.end}
        service = FleetService(Queries())
        result = service.default_window(1)
        self.assertEqual(result.start,self.start)
        self.assertGreater(result.end,self.window.end)
