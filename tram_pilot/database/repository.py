"""Параметризованные запросы и применение версионированной SQL-схемы."""
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def migrate(connection) -> list[str]:
    applied = []
    with connection.transaction():
        # Serialize concurrent migration commands without an external framework.
        connection.execute("SELECT pg_advisory_xact_lock(731249105)")
        connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if connection.execute("SELECT 1 FROM schema_migrations WHERE version = %s",
                                  (path.name,)).fetchone():
                continue
            connection.execute(path.read_text(encoding="utf-8"), prepare=False)
            connection.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
            applied.append(path.name)
    return applied


def insert_tracks(connection, records) -> dict[str, int]:
    from psycopg.types.json import Jsonb

    inserted = duplicates = 0
    with connection.transaction():
        with connection.cursor() as cursor:
            for record in records:
                cursor.execute("""INSERT INTO track_records
                    (observed_at, loco_number, device_id, cabin_type, attributes,
                     source_file, event_index, record_hash)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (record_hash) DO NOTHING RETURNING id""",
                    (record.observed_at, record.loco_number, record.device_id, record.cabin_type,
                     Jsonb(record.attributes), record.source_file, record.event_index, record.record_hash))
                if cursor.fetchone() is None:
                    duplicates += 1
                else:
                    inserted += 1
    return {"inserted": inserted, "duplicates": duplicates}


def read_tracks(connection, limit: int = 10, loco_number: str | None = None) -> list[dict]:
    if not 1 <= limit <= 1000:
        raise ValueError("limit должен быть от 1 до 1000")
    query = """SELECT id, observed_at, loco_number, device_id, cabin_type,
                      attributes, source_file, event_index, record_hash FROM track_records"""
    parameters = []
    if loco_number is not None:
        query += " WHERE loco_number = %s"
        parameters.append(loco_number)
    query += " ORDER BY observed_at, id LIMIT %s"
    parameters.append(limit)
    return connection.execute(query, parameters).fetchall()


def read_events(connection, limit: int = 10, loco_number: str | None = None) -> list[dict]:
    """Читает сохранённые события с их исходной позицией."""
    if not 1 <= limit <= 1000:
        raise ValueError("limit должен быть от 1 до 1000")
    query = """SELECT id, occurred_at, loco_number, event_type, payload,
                      source_name, source_file, event_index, record_hash FROM events"""
    values = []
    if loco_number is not None:
        query += " WHERE loco_number=%s"
        values.append(loco_number)
    query += " ORDER BY occurred_at, id LIMIT %s"
    return connection.execute(query, [*values, limit]).fetchall()


def read_import_runs(connection, limit: int = 10) -> list[dict]:
    """Показывает завершённые, ошибочные и прерванные импорты."""
    if not 1 <= limit <= 1000:
        raise ValueError("limit должен быть от 1 до 1000")
    return connection.execute("SELECT * FROM import_runs ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
