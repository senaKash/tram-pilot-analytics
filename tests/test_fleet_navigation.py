"""Проверка границы браузер/Python и независимого контекста пользователей."""
from datetime import datetime,timedelta,timezone
from unittest import TestCase

from tram_pilot.services.analytics import TimeWindow
from tram_pilot.services.fleet import FleetCell
from tram_pilot.services.fleet_navigation import FleetContext,FleetNavigation


class FleetNavigationTests(TestCase):
    def setUp(self):
        start = datetime(2026,9,7,tzinfo=timezone.utc)
        self.context = FleetContext(12,TimeWindow(start,start+timedelta(hours=1)),
                                    "sensor_freshness","0312")

    def event(self,tram="trm0312",run_id=12):
        return {"loco_number":tram,"analysis_run_id":run_id}

    def test_open_and_return_preserve_all_fleet_filters(self):
        nav = FleetNavigation(self.context)
        self.assertTrue(nav.open_selection(self.event(),12,{"trm0312"}))
        self.assertEqual(nav.page,"investigation")
        self.assertEqual(nav.loco_number,"trm0312")
        self.assertEqual(nav.context,self.context)
        nav.back()
        self.assertEqual(nav.page,"fleet")
        self.assertEqual(nav.context,self.context)
        self.assertTrue(nav.open_selection(self.event(),12,{"trm0312"}))
        self.assertEqual(nav.entry,2)

    def test_rejects_unknown_tram_and_forged_versions(self):
        for event in (None,[],{},self.event("unknown"),self.event(run_id="12"),
                      self.event(run_id=True),self.event(run_id=11),
                      {"analysis_run_id":12,"loco_number":["trm0312"]}):
            with self.subTest(event=event):
                nav = FleetNavigation(self.context)
                self.assertFalse(nav.open_selection(event,12,{"trm0312"}))
                self.assertEqual(nav.page,"fleet")
                self.assertIsNone(nav.loco_number)

    def test_requires_current_server_publication_not_browser_version(self):
        nav = FleetNavigation(self.context)
        self.assertFalse(nav.open_selection(self.event(),13,{"trm0312"}))
        self.assertFalse(nav.open_selection(self.event(run_id=13),13,{"trm0312"}))
        self.assertFalse(nav.open_selection(self.event(),None,{"trm0312"}))

    def test_removed_vehicle_is_not_opened(self):
        nav = FleetNavigation(self.context)
        self.assertFalse(nav.open_selection(self.event(),12,set()))

    def test_two_sessions_do_not_share_route_or_filters(self):
        first,second = FleetNavigation(self.context),FleetNavigation(self.context)
        first.open_selection(self.event(),12,{"trm0312"})
        second.context = FleetContext(12,self.context.window,None,"")
        self.assertEqual(second.page,"fleet")
        self.assertEqual(first.context.search,"0312")
        self.assertEqual(first.context.category,"sensor_freshness")
        self.assertEqual(second.context.search,"")

    def test_payload_keeps_database_text_as_structured_values(self):
        from dashboard.components.fleet_matrix import matrix_data
        tram = '<img src=x onerror="alert(1)">'
        window = self.context.window
        payload = matrix_data([FleetCell(tram,window,13,7,100)],12)
        self.assertEqual(payload["rows"][0]["loco_number"],tram)
        self.assertEqual(payload["rows"][0]["cells"][0]["parts"],
                         [{"color":"#2563EB","percent":35},
                          {"color":"#D12D38","percent":65}])
        self.assertNotIn("html",payload)

    def test_payload_order_is_stable_for_unsorted_cells(self):
        from dashboard.components.fleet_matrix import matrix_data
        window = self.context.window
        later = TimeWindow(window.end,window.end+timedelta(hours=1))
        cells = [FleetCell("B",later,0,0,0),FleetCell("A",window,0,0,1),
                 FleetCell("B",window,1,0,2),FleetCell("A",later,0,1,2)]
        payload = matrix_data(cells,12)
        self.assertEqual([row["loco_number"] for row in payload["rows"]],["A","B"])
        self.assertEqual(payload["rows"][1]["cells"][0]["kind"],"composition")
        self.assertEqual(payload["rows"][1]["cells"][1]["kind"],"missing")

    def test_no_publication_shows_empty_state_without_matrix(self):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_string('''
from unittest.mock import MagicMock,patch
import dashboard.app as application
pool = MagicMock()
with patch.object(application,"connection_pool",return_value=pool), \\
     patch.object(application.DashboardQueries,"published_run",return_value=None):
    application.main()
''').run(timeout=10)
        self.assertEqual(len(app.exception),0)
        self.assertEqual(len(app.error),0)
        self.assertEqual([message.value for message in app.info],
                         ["Нет опубликованного завершённого анализа"])
