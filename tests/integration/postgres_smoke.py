"""Explicit opt-in: python -m unittest discover -s tests/integration -p postgres_smoke.py -v."""
from dataclasses import replace
from unittest.mock import patch
import tempfile
import json
from pathlib import Path
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql

from tram_pilot.database.connection import connect, load_env_file
from tram_pilot.database.repository import insert_tracks, migrate, read_tracks, read_events, read_import_runs
from tram_pilot.database.importer import BatchImporter
from tram_pilot.analysis.pipeline import AnalysisPipeline, AnalysisOptions
from tram_pilot.database.analysis_repository import publish_analysis
from tram_pilot.database.queries import DashboardQueries
from tram_pilot.normalize import load_demo, normalize_track

ROOT = Path(__file__).resolve().parents[2]


class PostgreSQLIntegrationTests(unittest.TestCase):
    def setUp(self):
        load_env_file()
        self.connection = connect()
        self.connection.autocommit = True
        self.schema = "test_track_" + uuid4().hex
        self.connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
        self.addCleanup(self.cleanup_schema)

    def cleanup_schema(self):
        try:
            self.connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))
        finally:
            self.connection.close()

    def sample(self):
        return json.loads((ROOT / "examples/direct_track.json").read_text(encoding="utf-8"))

    def test_migration_repeat_import_and_readback(self):
        self.assertEqual(migrate(self.connection), ["001_track_records.sql", "002_imports_events.sql",
                                                     "003_import_conflicts.sql", "004_analysis.sql",
                                                     "005_published_analysis.sql"])
        self.assertEqual(migrate(self.connection), [])
        records, _ = load_demo(ROOT / "examples")
        self.assertEqual(insert_tracks(self.connection, records), {"inserted": 20, "duplicates": 1})
        self.assertEqual(insert_tracks(self.connection, records), {"inserted": 0, "duplicates": 21})
        stored = read_tracks(self.connection, 1000)
        self.assertEqual(len(stored), 20)
        original = {r.record_hash: r for r in records}
        for row in stored:
            self.assertEqual(row["attributes"], original[row["record_hash"]].attributes)
            self.assertEqual(row["observed_at"], original[row["record_hash"]].observed_at)
        columns = self.connection.execute("""SELECT column_name, data_type FROM information_schema.columns
            WHERE table_schema = %s AND table_name = 'track_records'""", (self.schema,)).fetchall()
        types = {r["column_name"]: r["data_type"] for r in columns}
        self.assertEqual(types["observed_at"], "timestamp with time zone")
        self.assertEqual(types["attributes"], "jsonb")

    def test_jsonb_false_null_absent_and_timestamp_conflicts(self):
        migrate(self.connection)
        source = self.sample()
        source["timestamp"] = "2026-01-01T12:00:10+03:00"
        records = [normalize_track(source, "false.json")]
        source["leftImage"] = None
        records.append(normalize_track(source, "null.json"))
        source.pop("leftImage")
        records.append(normalize_track(source, "absent.json"))
        self.assertEqual(insert_tracks(self.connection, records)["inserted"], 3)
        stored = read_tracks(self.connection)
        by_source = {row["source_file"]: row for row in stored}
        self.assertEqual(by_source["false.json"]["observed_at"].isoformat(),
                         "2026-01-01T09:00:10+00:00")
        self.assertIs(by_source["false.json"]["attributes"]["leftImage"], False)
        self.assertIsNone(by_source["null.json"]["attributes"]["leftImage"])
        self.assertNotIn("leftImage", by_source["absent.json"]["attributes"])
        types = self.connection.execute("""SELECT source_file,
            attributes ? 'leftImage' AS present, jsonb_typeof(attributes->'leftImage') AS value_type
            FROM track_records""").fetchall()
        actual = {row["source_file"]: (row["present"], row["value_type"]) for row in types}
        self.assertEqual(actual, {"false.json": (True, "boolean"), "null.json": (True, "null"),
                                  "absent.json": (False, None)})

    def test_conflicting_statuses_are_not_overwritten(self):
        migrate(self.connection)
        source = self.sample()
        error = normalize_track(source, "error.json")
        source["m_danger_obj_status"] = "ProperlyWork"
        clear = normalize_track(source, "clear.json")
        self.assertEqual(insert_tracks(self.connection, [error, clear])["inserted"], 2)
        rows = read_tracks(self.connection, loco_number=source["loco_number"])
        self.assertEqual({r["attributes"]["m_danger_obj_status"] for r in rows}, {"Error", "ProperlyWork"})

    def test_failed_batch_rolls_back(self):
        migrate(self.connection)
        source = self.sample()
        good = normalize_track(source, "good.json")
        source["leftImage"] = True
        bad = replace(normalize_track(source, "bad.json"), event_index=-1)
        with self.assertRaises(psycopg.errors.CheckViolation):
            insert_tracks(self.connection, [good, bad])
        self.assertEqual(read_tracks(self.connection), [])


    def test_copy_events_repeat_and_provenance(self):
        migrate(self.connection)
        source = ROOT / "examples"
        first = BatchImporter(self.connection, source, batch_size=2, max_files=10).run()
        self.assertEqual((first["track_count"], first["event_count"], first["duplicate_count"]),
                         (21, 3, 1))
        self.assertEqual(len(read_tracks(self.connection, 100)), 20)
        event_rows = read_events(self.connection, 10)
        self.assertEqual({r["event_type"] for r in event_rows}, {"Warn", "Brake", "OverSpeed"})
        self.assertTrue(all(r["source_file"] == "events_container.json" for r in event_rows))
        self.assertEqual(len({r["event_index"] for r in event_rows}), 3)
        second = BatchImporter(self.connection, source, batch_size=3, max_files=10).run()
        self.assertEqual(second["duplicate_count"], 24)
        self.assertEqual(len(read_events(self.connection, 10)), 3)
        sources = self.connection.execute("SELECT count(*) AS n FROM track_record_sources").fetchone()
        self.assertEqual(sources["n"], 21)
        self.assertEqual([r["status"] for r in read_import_runs(self.connection, 2)],
                         ["completed", "completed"])

    def test_conflicting_events_same_timestamp_are_preserved(self):
        migrate(self.connection)
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            base = self.sample()
            base.update(event_type="Warn", event_id="DEMO-EVENT-1")
            other = dict(base, event_id="DEMO-EVENT-2")
            (source / "events.json").write_text(json.dumps({"events": [base, other]}), encoding="utf-8")
            result = BatchImporter(self.connection, source, batch_size=1).run()
        self.assertEqual(result["event_count"], 2)
        self.assertEqual(result["duplicate_count"], 0)
        self.assertEqual(result["conflict_count"], 0)
        self.assertEqual(len(read_events(self.connection, 10)), 2)

    def test_changed_record_at_same_origin_is_counted_as_conflict(self):
        migrate(self.connection)
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            path = source / "one.json"
            first_track = self.sample()
            path.write_text(json.dumps(first_track), encoding="utf-8")
            first = BatchImporter(self.connection, source).run()
            changed_track = dict(first_track, m_danger_obj_status="ProperlyWork")
            path.write_text(json.dumps(changed_track), encoding="utf-8")
            second = BatchImporter(self.connection, source).run()
            self.assertEqual((first["conflict_count"], second["conflict_count"]), (0, 1))
            self.assertEqual(len(read_tracks(self.connection)), 2)

            first_event = dict(first_track, event_type="Warn")
            path.write_text(json.dumps({"events": [first_event]}), encoding="utf-8")
            BatchImporter(self.connection, source).run()
            changed_event = dict(first_event, event_id="DEMO-CHANGED")
            path.write_text(json.dumps({"events": [changed_event]}), encoding="utf-8")
            fourth = BatchImporter(self.connection, source).run()
            self.assertEqual(fourth["conflict_count"], 1)
            self.assertEqual(len(read_events(self.connection, 10)), 2)

    def test_invalid_json_count_and_interrupted_retry(self):
        migrate(self.connection)
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "bad.json").write_text("{broken", encoding="utf-8")
            (source / "good.json").write_text(json.dumps(self.sample()), encoding="utf-8")
            (source / "invalid_type.json").write_text(
                json.dumps({"event_type": []}), encoding="utf-8")
            result = BatchImporter(self.connection, source, batch_size=1).run()
            self.assertEqual((result["processed_json"], result["invalid_count"], result["track_count"]),
                             (3, 2, 1))
            run = read_import_runs(self.connection, 1)[0]
            self.assertEqual(run["status"], "completed")
            self.assertIn("bad.json", run["error_message"])
            self.connection.execute("DELETE FROM track_record_sources")
            self.connection.execute("DELETE FROM track_records")

            def interrupted(*_args):
                yield "good.json", self.sample(), None
                raise KeyboardInterrupt()

            with patch("tram_pilot.database.importer.read_documents", interrupted):
                with self.assertRaises(KeyboardInterrupt):
                    BatchImporter(self.connection, source, batch_size=1).run()
            self.assertEqual(read_tracks(self.connection), [])
            self.assertEqual(read_import_runs(self.connection, 1)[0]["status"], "interrupted")
            retry = BatchImporter(self.connection, source, batch_size=1).run()
            self.assertEqual((retry["track_count"], retry["invalid_count"], retry["duplicate_count"]),
                             (1, 2, 0))
            self.assertEqual(len(read_tracks(self.connection)), 1)

    def test_repeated_track_from_other_source_keeps_both_origins(self):
        migrate(self.connection)
        with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
            content = json.dumps(self.sample())
            (Path(left) / "first.json").write_text(content, encoding="utf-8")
            (Path(right) / "second.json").write_text(content, encoding="utf-8")
            one = BatchImporter(self.connection, Path(left)).run()
            two = BatchImporter(self.connection, Path(right)).run()
        self.assertEqual((one["duplicate_count"], two["duplicate_count"]), (0, 1))
        self.assertEqual(len(read_tracks(self.connection)), 1)
        origins = self.connection.execute("SELECT source_file FROM track_record_sources").fetchall()
        self.assertEqual({r["source_file"] for r in origins}, {"first.json", "second.json"})

    def test_next_run_marks_abandoned_status(self):
        migrate(self.connection)
        source = ROOT / "examples"
        self.connection.execute("""INSERT INTO import_runs
            (source_name, status, normalizer_version) VALUES (%s, 'running', 'previous')""",
            (str(source.resolve()),))
        BatchImporter(self.connection, source).run()
        self.assertEqual([r["status"] for r in read_import_runs(self.connection, 2)],
                         ["completed", "interrupted"])
        with connect() as second_connection:
            acquired = second_connection.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS locked",
                (str(source.resolve()),)).fetchone()["locked"]
            self.assertTrue(acquired)
            second_connection.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                                      (str(source.resolve()),))

    def test_analysis_separate_signals_devices_and_versions(self):
        migrate(self.connection)
        sample = self.sample()
        samples = []
        for offset, status in [(0, "Error"), (5, "Error"), (60, "Error"),
                               (65, "Error"), (70, "ProperlyWork"), (75, "Error")]:
            row = dict(sample, timestamp=f"2026-01-01T09:{offset // 60:02d}:{offset % 60:02d}Z",
                       m_danger_obj_status=status, m_trafficlight_status="Error",
                       m_speed_limit_status="TurnOff", leftImage=False)
            samples.append(normalize_track(row, f"track-{offset}.json"))
        other = dict(sample, device_id="DEMO-UNIT-OTHER", m_danger_obj_status="Error")
        samples.append(normalize_track(other, "other.json"))
        insert_tracks(self.connection, samples)
        first = AnalysisPipeline(self.connection).run()
        second = AnalysisPipeline(self.connection, AnalysisOptions(30, 30)).run()
        self.assertNotEqual(first["run_id"], second["run_id"])
        version = self.connection.execute("SELECT pipeline_version FROM analysis_runs WHERE id=%s",
                                          (first["run_id"],)).fetchone()["pipeline_version"]
        self.assertEqual(version, "2")
        rows = self.connection.execute("""SELECT signal, device_id, segment_count,
            bridged_gap_seconds, close_reason FROM episodes WHERE analysis_run_id=%s""",
            (first["run_id"],)).fetchall()
        danger = [r for r in rows if r["signal"] == "m_danger_obj_status"
                  and r["device_id"] == sample["device_id"]]
        self.assertEqual(len(danger), 2)
        self.assertEqual(sorted(r["segment_count"] for r in danger), [1, 2])
        bridged = next(r for r in danger if r["segment_count"] == 2)
        self.assertEqual(bridged["bridged_gap_seconds"], 55)
        self.assertEqual(bridged["close_reason"], "observed_clearance")
        self.assertTrue(any(r["signal"] == "m_trafficlight_status" for r in rows))
        self.assertTrue(any(r["device_id"] == "DEMO-UNIT-OTHER" for r in rows))
        self.assertFalse(any(r["signal"] == "m_speed_limit_status"
                             and r["device_id"] == sample["device_id"] for r in rows))
        linked = self.connection.execute("""SELECT count(*) n FROM segment_observations so
            JOIN diagnostic_segments s ON s.id=so.segment_id WHERE s.analysis_run_id=%s""",
            (first["run_id"],)).fetchone()["n"]
        self.assertGreater(linked, 0)
        cleared = self.connection.execute("""SELECT count(*) n FROM segment_observations so
            JOIN diagnostic_segments s ON s.id=so.segment_id
            WHERE s.analysis_run_id=%s AND so.state='clear'""",
            (first["run_id"],)).fetchone()["n"]
        self.assertGreater(cleared, 0)
        self.assertEqual(self.connection.execute("""SELECT count(*) n FROM episodes
            WHERE analysis_run_id=%s""", (first["run_id"],)).fetchone()["n"], len(rows))
        narrow = self.connection.execute("""SELECT count(*) n FROM episodes
            WHERE analysis_run_id=%s AND signal='m_danger_obj_status' AND device_id=%s""",
            (second["run_id"], sample["device_id"])).fetchone()["n"]
        self.assertEqual(narrow, 3)

    def test_equal_timestamp_conflict_does_not_create_false_recovery(self):
        migrate(self.connection)
        source = self.sample()
        records = []
        for device, middle in (("DEVICE-A", ("ProperlyWork", "Error")),
                               ("DEVICE-B", ("Error", "ProperlyWork"))):
            for index, (second, status) in enumerate(
                    [(0, "Error"), (10, middle[0]), (10, middle[1]), (20, "Error")]):
                row = dict(source, device_id=device,
                           timestamp=f"2026-01-01T09:00:{second:02d}Z",
                           m_danger_obj_status=status)
                records.append(normalize_track(row, f"{device}-{index}.json"))
        insert_tracks(self.connection, records)
        run = AnalysisPipeline(self.connection).run()
        rows = self.connection.execute("""SELECT device_id, bad_count, segment_count,
            close_reason FROM episodes WHERE analysis_run_id=%s
            AND signal='m_danger_obj_status' ORDER BY device_id""",
            (run["run_id"],)).fetchall()
        self.assertEqual([(r["device_id"], r["bad_count"], r["segment_count"],
                           r["close_reason"]) for r in rows],
                         [("DEVICE-A", 3, 1, "sample_end"),
                          ("DEVICE-B", 3, 1, "sample_end")])

    def test_published_queries_exclude_unfinished_runs_and_count_new_episodes(self):
        from datetime import timedelta

        migrate(self.connection)
        queries = DashboardQueries(self.connection)
        self.assertIsNone(queries.published_run())
        self.assertEqual(queries.trams(), [])
        self.connection.execute("""INSERT INTO analysis_runs
            (status, pipeline_version, analyzer_versions, parameters)
            VALUES ('running','x','{}','{}')""")
        with self.assertRaisesRegex(ValueError, "завершённый"):
            publish_analysis(self.connection, 1)
        insert_tracks(self.connection, [normalize_track(self.sample(), "sample.json")])
        first = AnalysisPipeline(self.connection).run()
        tram = self.sample()["loco_number"]
        device = self.sample()["device_id"]
        cabin = self.sample()["cabin_type"]
        self.assertEqual(queries.trams(), [])
        publish_analysis(self.connection, first["run_id"])
        self.assertEqual(queries.published_run()["id"], first["run_id"])
        self.assertEqual(queries.trams(), [tram])
        at = queries.bounds(tram, device, cabin)[0]
        before, after = at - timedelta(seconds=1), at + timedelta(seconds=1)
        bars = queries.histogram(tram, device, cabin, before, after, 60, None)
        self.assertEqual(sum(row["episodes"] for row in bars), 4)
        self.assertEqual(queries.journal_count(tram, device, cabin, at, after, None), 4)
        self.assertEqual(queries.journal_count(tram, device, cabin, after, after +
                                               timedelta(minutes=1), None), 0)
        self.assertEqual(len(queries.journal(tram, device, cabin, before, after,
                                             None, "newest", 1)), 4)
        points = queries.timeline_observations(tram, device, cabin, before, after, 60)
        self.assertEqual(len(points), 10)
        second = AnalysisPipeline(self.connection).run()
        publish_analysis(self.connection, second["run_id"])
        self.assertEqual(queries.published_run()["id"], second["run_id"])
        self.assertEqual(queries.journal_count(tram, device, cabin, before, after, None), 4)

    def test_dashboard_states_and_bucket_start_filter(self):
        from datetime import datetime,timedelta,timezone
        migrate(self.connection)
        source = self.sample()
        first = datetime(2026,1,1,9,tzinfo=timezone.utc)
        records = []
        values = [(0,"ProperlyWork"),(5,"TurnOff"),(10,"TemporaryOff"),
                  (15,None),(20,"missing"),(25,"Error"),(25,"ProperlyWork"),
                  (60,"Error"),(65,"Error")]
        for i,(seconds,value) in enumerate(values):
            row = dict(source,timestamp=(first+timedelta(seconds=seconds)).isoformat(),
                       m_danger_obj_status=value,ubloxGps=False)
            if value=="missing":
                row.pop("m_danger_obj_status")
            records.append(normalize_track(row,f"ui-{i}.json"))
        insert_tracks(self.connection,records)
        run = AnalysisPipeline(self.connection).run()
        publish_analysis(self.connection,run["run_id"])
        q = DashboardQueries(self.connection)
        args = (source["loco_number"],source["device_id"],source["cabin_type"])
        states = q.timeline_states(*args,first,first+timedelta(seconds=70))
        danger = [r for r in states if r["signal"]=="m_danger_obj_status"]
        self.assertEqual([r["state"] for r in danger],
                         ["clear","off","off","unknown","unknown","conflict","bad"])
        self.assertIn("null",danger[3]["detail"])
        self.assertEqual(danger[4]["detail"],"Поле отсутствует")
        self.assertEqual(danger[-1]["snapshots"],2)
        start,end = first+timedelta(seconds=1),first+timedelta(seconds=70)
        intersect = q.journal(*args,start,end,None,"oldest",1)
        started = q.journal(*args,start,end,None,"oldest",1,starts_only=True)
        self.assertTrue(any(r["signal"]=="ubloxGps" for r in intersect))
        self.assertFalse(any(r["signal"]=="ubloxGps" for r in started))
        self.assertEqual(q.journal_count(*args,start,end,None,True),len(started))

    def fleet_sample(self):
        from datetime import datetime,timedelta,timezone
        migrate(self.connection)
        first = datetime(2026,9,6,20,59,tzinfo=timezone.utc)
        base = dict(self.sample(),m_danger_obj_status="ProperlyWork",
                    m_trafficlight_status="ProperlyWork",m_speed_limit_status="ProperlyWork",
                    leftImage=True,ubloxGps=True)
        rows = []
        for second in (0,10,30,60,120):
            row = dict(base,loco_number="FLEET-A",
                       timestamp=(first+timedelta(seconds=second)).astimezone(
                           timezone(timedelta(hours=3))).isoformat())
            if second in (0,10):
                row.update(m_danger_obj_status="Error",m_trafficlight_status="Error",ubloxGps=False)
            if second==60:
                row["m_danger_obj_status"]="Error"
            rows.append(row)
        rows.extend([
            dict(base,loco_number="FLEET-A",device_id="SECOND-DEVICE",
                 timestamp=(first+timedelta(seconds=5)).isoformat()),
            dict(base,loco_number="FLEET-B",timestamp=first.isoformat()),
            dict(base,loco_number="FLEET-B",ubloxGps=None,
                 timestamp=(first+timedelta(seconds=70)).isoformat()),
            dict(base,loco_number="FLEET-C",
                 timestamp=(first-timedelta(minutes=1)).isoformat()),
        ])
        insert_tracks(self.connection,[normalize_track(row,f"fleet-{i}.json")
                                      for i,row in enumerate(rows)])
        run = AnalysisPipeline(self.connection).run()
        return first,run["run_id"],base

    def test_fleet_matrix_counts_boundaries_and_neutral_cells(self):
        from datetime import timedelta
        from tram_pilot.database.fleet_queries import FleetQueries
        from tram_pilot.services.fleet import FleetService
        from tram_pilot.services.analytics import TimeWindow
        first,run_id,base = self.fleet_sample()
        query = FleetQueries(self.connection)
        end = first+timedelta(minutes=2)
        self.assertIsNone(query.coverage(run_id))
        self.assertEqual(query.matrix(run_id,first,end,60),[])
        publish_analysis(self.connection,run_id)
        rows = query.matrix(run_id,first,end,60)
        self.assertEqual(len(rows),6)
        self.assertEqual({r["loco_number"] for r in rows},{"FLEET-A","FLEET-B","FLEET-C"})
        a = [r for r in rows if r["loco_number"]=="FLEET-A"]
        self.assertEqual([(r["error_count"],r["freshness_count"],r["track_count"]) for r in a],
                         [(2,1,4),(1,0,1)])
        self.assertEqual(a[0]["bucket_end"],a[1]["bucket_start"])
        self.assertEqual(a[1]["bucket_end"],end)
        cells = FleetService(query).matrix(run_id,TimeWindow(first,end))
        self.assertEqual([c.kind for c in cells if c.loco_number=="FLEET-B"],["quiet","quiet"])
        self.assertEqual([c.kind for c in cells if c.loco_number=="FLEET-C"],["missing","missing"])
        # Ранние эпизоды продолжаются в окне, но не становятся новыми повторно.
        shifted = query.matrix(run_id,first+timedelta(seconds=1),end,60,search="FLEET-A")
        self.assertEqual(sum(r["error_count"] for r in shifted),1)
        self.assertEqual(sum(r["freshness_count"] for r in shifted),0)
        selected = query.matrix(run_id,first,end,60,"sensor_freshness","FLEET-A")
        self.assertEqual([r["error_count"] for r in selected],[0,0])
        self.assertEqual([r["freshness_count"] for r in selected],[1,0])
        self.assertEqual(query.matrix(run_id,first,end,60,search="%"),[])
        self.assertEqual(query.coverage(run_id)["tram_count"],3)
        self.assertEqual(query.vehicles(run_id),["FLEET-A","FLEET-B","FLEET-C"])
        self.assertTrue(query.devices(run_id,"FLEET-C"))
        self.assertEqual(query.devices(run_id,"not-loaded"),[])

    def test_fleet_snapshot_excludes_late_tracks_and_unpublished_versions(self):
        from datetime import timedelta
        from tram_pilot.database.fleet_queries import FleetQueries
        first,run_id,base = self.fleet_sample()
        q = FleetQueries(self.connection)
        end = first+timedelta(minutes=2)
        publish_analysis(self.connection,run_id)
        late = dict(base,loco_number="FLEET-LATE",timestamp=first.isoformat())
        insert_tracks(self.connection,[normalize_track(late,"late.json")])
        self.assertEqual(q.coverage(run_id)["tram_count"],3)
        newer = AnalysisPipeline(self.connection).run()["run_id"]
        self.assertEqual(q.matrix(newer,first,end,60),[])
        self.assertEqual(len(q.matrix(run_id,first,end,60)),6)
        publish_analysis(self.connection,newer)
        self.assertEqual(q.matrix(run_id,first,end,60),[])
        self.assertIsNone(q.coverage(run_id))
        self.assertEqual(q.coverage(newer)["tram_count"],4)
        self.assertEqual(q.vehicles(run_id),[])
        self.assertIn("FLEET-LATE",q.vehicles(newer))
        self.assertEqual(len(q.matrix(newer,first,end,60)),8)


if __name__ == "__main__":
    unittest.main()
