"""Последовательное чтение Track и запись результатов расчёта."""
from psycopg.types.json import Jsonb


def publish_analysis(connection, run_id: int) -> None:
    """Атомарно назначает единственный завершённый расчёт для чтения интерфейсом."""
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(731249106)")
        row = connection.execute("""SELECT id FROM analysis_runs
            WHERE id=%s AND status='completed' FOR UPDATE""", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Можно опубликовать только завершённый расчёт")
        connection.execute("UPDATE analysis_runs SET published=false WHERE published=true")
        connection.execute("UPDATE analysis_runs SET published=true WHERE id=%s", (run_id,))
        connection.execute("""INSERT INTO published_analysis
            (singleton, analysis_run_id) VALUES (true, %s)
            ON CONFLICT (singleton) DO UPDATE SET analysis_run_id=EXCLUDED.analysis_run_id,
            published_at=now()""", (run_id,))


class AnalysisRepository:
    """Изолирует SQL от чистой логики анализаторов и группировки."""

    def __init__(self, connection, run_id: int):
        self.connection = connection
        self.run_id = run_id

    def tracks(self, batch_size: int = 500):
        """Читает упорядоченные Track серверным курсором ограниченными пакетами."""
        with self.connection.cursor(name=f"analysis_tracks_{self.run_id}") as cursor:
            cursor.execute("""SELECT id, observed_at, loco_number, device_id, cabin_type, attributes
                FROM track_records WHERE id <= (SELECT max_track_record_id FROM analysis_runs WHERE id=%s)
                ORDER BY loco_number, device_id, cabin_type, observed_at, id""", (self.run_id,))
            while rows := cursor.fetchmany(batch_size):
                yield from rows

    def create_segment(self, key, segment) -> int:
        """Создаёт сегмент при первом плохом снимке; границы обновятся при закрытии."""
        return self.connection.execute("""INSERT INTO diagnostic_segments
            (analysis_run_id, loco_number, device_id, cabin_type, category, signal,
             first_bad_at, last_bad_at, first_track_id, last_track_id, bad_count, close_reason)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (self.run_id, *key, segment.first_bad_at, segment.last_bad_at,
             segment.first_track_id, segment.last_track_id, segment.bad_count,
             segment.close_reason)).fetchone()["id"]

    def add_observation(self, segment_id: int, observation) -> None:
        """Сохраняет исходное значение, включая снятие и неизвестность на границе."""
        self.connection.execute("""INSERT INTO segment_observations
            (segment_id, track_record_id, state, value_present, raw_value, context)
            VALUES (%s,%s,%s,%s,%s,%s)""",
            (segment_id, observation.track_id, observation.state, observation.value_present,
             Jsonb(observation.raw_value), Jsonb(observation.context)))

    def close_segment(self, segment) -> None:
        """Записывает окончательные границы и основание завершения."""
        self.connection.execute("""UPDATE diagnostic_segments SET last_bad_at=%s,
            last_track_id=%s, bad_count=%s, close_reason=%s, clear_track_id=%s WHERE id=%s""",
            (segment.last_bad_at, segment.last_track_id, segment.bad_count,
             segment.close_reason, segment.clear_track_id, segment.db_id))

    def segments(self, batch_size: int = 500):
        """Читает завершённые сегменты в устойчивом порядке для эпизодов."""
        with self.connection.cursor(name=f"analysis_segments_{self.run_id}") as cursor:
            cursor.execute("""SELECT * FROM diagnostic_segments WHERE analysis_run_id=%s
                ORDER BY loco_number, device_id, cabin_type, category, signal,
                         first_bad_at, id""", (self.run_id,))
            while rows := cursor.fetchmany(batch_size):
                yield from rows

    def create_episode(self, key, episode) -> int:
        """Создаёт кандидатный эпизод для первого сегмента."""
        return self.connection.execute("""INSERT INTO episodes
            (analysis_run_id, loco_number, device_id, cabin_type, category, signal,
             first_bad_at, last_bad_at, first_track_id, last_track_id, bad_count,
             segment_count, bridged_gap_seconds, close_reason, evidence_class)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (self.run_id, *key, episode.first_bad_at, episode.last_bad_at,
             episode.first_track_id, episode.last_track_id, episode.bad_count,
             episode.segment_count, episode.bridged_gap_seconds, episode.close_reason,
             "observed")).fetchone()["id"]

    def add_episode_segment(self, episode_id: int, segment_id: int, ordinal: int) -> None:
        """Связывает сегмент с эпизодом и тем же запуском анализа."""
        self.connection.execute("""INSERT INTO episode_segments
            (analysis_run_id, episode_id, segment_id, ordinal) VALUES (%s,%s,%s,%s)""",
            (self.run_id, episode_id, segment_id, ordinal))

    def close_episode(self, episode) -> None:
        """Сохраняет число сегментов, пропуски и итоговое основание закрытия."""
        self.connection.execute("""UPDATE episodes SET last_bad_at=%s, last_track_id=%s,
            bad_count=%s, segment_count=%s, bridged_gap_seconds=%s,
            close_reason=%s, evidence_class=%s WHERE id=%s""",
            (episode.last_bad_at, episode.last_track_id, episode.bad_count,
             episode.segment_count, episode.bridged_gap_seconds, episode.close_reason,
             "bridged" if episode.segment_count > 1 else "observed", episode.db_id))
