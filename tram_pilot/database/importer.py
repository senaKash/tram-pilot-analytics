"""Управление одним потоковым импортом с явным статусом завершения."""
from dataclasses import dataclass
from pathlib import Path
import time

from ..archive_reader import read_documents
from ..normalize import EVENT_TYPES, normalize_event, normalize_track
from .batch import create_staging, write_batch

NORMALIZER_VERSION = "2"


@dataclass
class ImportStats:
    processed_json: int = 0
    track_count: int = 0
    event_count: int = 0
    duplicate_count: int = 0
    conflict_count: int = 0
    invalid_count: int = 0


class BatchImporter:
    """Читает источник, нормализует записи и атомарно сохраняет COPY-пакеты."""

    def __init__(self, connection, source: Path, batch_size: int = 100,
                 max_files: int = 20000, max_json_mb: int = 5):
        if not 1 <= batch_size <= 1000:
            raise ValueError("batch_size должен быть от 1 до 1000")
        self.connection = connection
        self.source = Path(source).resolve()
        self.source_name = str(self.source)
        self.batch_size = batch_size
        self.max_files = max_files
        self.max_json_mb = max_json_mb
        self.stats = ImportStats()
        self.errors = []

    def run(self) -> dict:
        """Фиксирует данные только после полного сканирования заданной выборки."""
        conn = self.connection
        # Сессионная блокировка не даёт одновременно импортировать один источник.
        lock = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS locked",
                            (self.source_name,)).fetchone()["locked"]
        conn.commit()
        if not lock:
            raise RuntimeError("Этот источник уже импортируется")
        try:
            conn.execute("""UPDATE import_runs SET status='interrupted', finished_at=now(),
                error_message='Предыдущий процесс завершился до фиксации результата'
                WHERE source_name=%s AND status='running'""", (self.source_name,))
            run_id = conn.execute("""INSERT INTO import_runs
                (source_name, status, normalizer_version) VALUES (%s, 'running', %s)
                RETURNING id""", (self.source_name, NORMALIZER_VERSION)).fetchone()["id"]
            conn.commit()
            started = time.perf_counter()
            try:
                with conn.transaction():
                    create_staging(conn)
                    self._scan(run_id)
                    conn.execute("""UPDATE import_runs SET status='completed', finished_at=now(),
                        processed_json=%s, track_count=%s, event_count=%s,
                        duplicate_count=%s, conflict_count=%s, invalid_count=%s, error_message=%s
                        WHERE id=%s""", (*self._counts(), "; ".join(self.errors) or None, run_id))
            except BaseException as exc:
                conn.rollback()
                status = "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed"
                conn.execute("""UPDATE import_runs SET status=%s, finished_at=now(),
                    processed_json=%s, track_count=%s, event_count=%s,
                    duplicate_count=%s, conflict_count=%s, invalid_count=%s, error_message=%s WHERE id=%s""",
                    (status, *self._counts(), f"{type(exc).__name__}: импорт прерван", run_id))
                conn.commit()
                raise
            return {"run_id": run_id, "status": "completed", **self.stats.__dict__,
                    "seconds": round(time.perf_counter() - started, 3)}
        finally:
            # Блокировка нужна лишь на время одного запуска, даже при ошибке.
            conn.rollback()
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (self.source_name,))
            conn.commit()

    def _counts(self):
        s = self.stats
        return (s.processed_json, s.track_count, s.event_count, s.duplicate_count,
                s.conflict_count, s.invalid_count)

    def _invalid(self, name: str, reason: str):
        self.stats.invalid_count += 1
        if len(self.errors) < 5:
            self.errors.append(f"{name}: {reason}")

    def _scan(self, run_id: int):
        tracks, events = [], []
        for name, document, error in read_documents(self.source, self.max_files, self.max_json_mb):
            self.stats.processed_json += 1
            if error:
                self._invalid(name, error)
                continue
            if "events" in document:
                if not isinstance(document["events"], list):
                    self._invalid(name, "events не является массивом")
                    continue
                rows = document["events"]
            else:
                rows = [document]
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    self._invalid(name, f"элемент {index} не является объектом")
                    continue
                kind = row.get("event_type")
                try:
                    if kind == "Track":
                        tracks.append(normalize_track(row, name, index))
                        self.stats.track_count += 1
                    elif isinstance(kind, str) and kind in EVENT_TYPES:
                        events.append(normalize_event(row, self.source_name, name, index))
                        self.stats.event_count += 1
                    else:
                        self._invalid(name, f"неизвестный event_type элемента {index}")
                except (ValueError, TypeError):
                    self._invalid(name, f"не прошла нормализация элемента {index}")
                if len(tracks) + len(events) >= self.batch_size:
                    duplicates, conflicts = write_batch(self.connection, tracks, events,
                                                        self.source_name, run_id)
                    self.stats.duplicate_count += duplicates
                    self.stats.conflict_count += conflicts
                    tracks.clear()
                    events.clear()
        if tracks or events:
            duplicates, conflicts = write_batch(self.connection, tracks, events,
                                                self.source_name, run_id)
            self.stats.duplicate_count += duplicates
            self.stats.conflict_count += conflicts
