"""Ограниченные COPY-пакеты во временные таблицы с последующей дедупликацией."""
from psycopg.types.json import Jsonb


def create_staging(connection):
    """Создаёт временные таблицы в транзакции импорта; при откате они исчезнут."""
    connection.execute("""CREATE TEMP TABLE stage_tracks (
        observed_at TIMESTAMPTZ, loco_number TEXT, device_id TEXT, cabin_type TEXT,
        attributes JSONB, source_name TEXT, source_file TEXT, event_index INTEGER,
        record_hash TEXT
    ) ON COMMIT DROP""")
    connection.execute("""CREATE TEMP TABLE stage_events (
        occurred_at TIMESTAMPTZ, loco_number TEXT, event_type TEXT, payload JSONB,
        source_name TEXT, source_file TEXT, event_index INTEGER, record_hash TEXT
    ) ON COMMIT DROP""")


def write_batch(connection, tracks, events, source_name: str, run_id: int) -> tuple[int, int]:
    """Пишет COPY-пакет и возвращает число дублей и конфликтующих входных строк."""
    with connection.cursor() as cursor:
        cursor.execute("TRUNCATE stage_tracks, stage_events")
        if tracks:
            with cursor.copy("""COPY stage_tracks (observed_at, loco_number, device_id,
                cabin_type, attributes, source_name, source_file, event_index, record_hash)
                FROM STDIN""") as copy:
                for row in tracks:
                    copy.write_row((row.observed_at, row.loco_number, row.device_id,
                                    row.cabin_type, Jsonb(row.attributes), source_name,
                                    row.source_file, row.event_index, row.record_hash))
        if events:
            with cursor.copy("""COPY stage_events (occurred_at, loco_number, event_type,
                payload, source_name, source_file, event_index, record_hash)
                FROM STDIN""") as copy:
                for row in events:
                    copy.write_row((row.occurred_at, row.loco_number, row.event_type,
                                    Jsonb(row.payload), row.source_name, row.source_file,
                                    row.event_index, row.record_hash))
        cursor.execute("""INSERT INTO track_records
            (observed_at, loco_number, device_id, cabin_type, attributes,
             source_file, event_index, record_hash)
            SELECT DISTINCT ON (record_hash) observed_at, loco_number, device_id,
                cabin_type, attributes, source_file, event_index, record_hash
            FROM stage_tracks ORDER BY record_hash, source_file, event_index
            ON CONFLICT (record_hash) DO NOTHING""")
        inserted_tracks = cursor.rowcount
        cursor.execute("""INSERT INTO track_record_sources
            (record_id, source_name, source_file, event_index, first_import_run_id)
            SELECT DISTINCT t.id, s.source_name, s.source_file, s.event_index, %s
            FROM stage_tracks s JOIN track_records t ON t.record_hash = s.record_hash
            WHERE true ON CONFLICT DO NOTHING""", (run_id,))
        cursor.execute("""INSERT INTO events
            (occurred_at, loco_number, event_type, payload, source_name,
             source_file, event_index, first_import_run_id, record_hash)
            SELECT DISTINCT ON (record_hash) occurred_at, loco_number, event_type,
                payload, source_name, source_file, event_index, %s, record_hash
            FROM stage_events ORDER BY record_hash
            ON CONFLICT (record_hash) DO NOTHING""", (run_id,))
        inserted_events = cursor.rowcount
        # Сравнение после INSERT видит также конфликтующие строки того же пакета.
        cursor.execute("""SELECT count(*) AS n FROM stage_tracks s WHERE EXISTS (
            SELECT 1 FROM track_records t WHERE t.loco_number=s.loco_number
              AND t.device_id=s.device_id AND t.cabin_type=s.cabin_type
              AND t.observed_at=s.observed_at AND t.record_hash<>s.record_hash)""")
        track_conflicts = cursor.fetchone()["n"]
        cursor.execute("""SELECT count(*) AS n FROM stage_events s WHERE EXISTS (
            SELECT 1 FROM events e WHERE e.source_name=s.source_name
              AND e.source_file=s.source_file AND e.event_index=s.event_index
              AND e.record_hash<>s.record_hash)""")
        event_conflicts = cursor.fetchone()["n"]
    duplicates = len(tracks) + len(events) - inserted_tracks - inserted_events
    return duplicates, track_conflicts + event_conflicts
