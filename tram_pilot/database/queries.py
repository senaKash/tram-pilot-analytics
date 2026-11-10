"""Ограниченные SQL-запросы только к опубликованному завершённому расчёту."""
from datetime import datetime


class DashboardQueries:
    """Читает готовые результаты; не запускает анализ и не загружает весь Track."""

    def __init__(self, connection):
        self.connection = connection

    def published_run(self) -> dict | None:
        """Возвращает текущую версию или None при отсутствии публикации."""
        return self.connection.execute("""SELECT a.id, a.pipeline_version, a.parameters,
            a.finished_at, p.published_at FROM published_analysis p
            JOIN analysis_runs a ON a.id=p.analysis_run_id AND a.status='completed'
            WHERE p.singleton=true""").fetchone()

    def trams(self) -> list[str]:
        """Показывает трамваи, имеющие опубликованные эпизоды."""
        rows = self.connection.execute("""SELECT DISTINCT loco_number FROM published_episodes
            ORDER BY loco_number LIMIT 1000""").fetchall()
        return [row["loco_number"] for row in rows]

    def bounds(self, tram: str, device: str, cabin: str) -> tuple[datetime, datetime] | None:
        """Границы наблюдаемых эпизодов выбранного источника."""
        row = self.connection.execute("""SELECT min(first_bad_at) AS first_at,
            max(last_bad_at) AS last_at FROM published_episodes
            WHERE loco_number=%s AND device_id=%s AND cabin_type=%s""",
            (tram, device, cabin)).fetchone()
        return (row["first_at"], row["last_at"]) if row["first_at"] else None

    def histogram(self, tram: str, device: str, cabin: str, start: datetime,
                  end: datetime, bucket_seconds: int, category: str | None) -> list[dict]:
        """Считает каждый новый эпизод один раз по first_bad_at в PostgreSQL."""
        return self.connection.execute("""SELECT date_bin(make_interval(secs => %s),
            first_bad_at, TIMESTAMPTZ '2000-01-01 00:00:00+00') AS bucket,
            category, count(*) AS episodes FROM published_episodes
            WHERE loco_number=%s AND device_id=%s AND cabin_type=%s
              AND first_bad_at >= %s AND first_bad_at < %s
              AND (%s::text IS NULL OR category=%s::text)
            GROUP BY bucket, category ORDER BY bucket, category LIMIT 600""",
            (bucket_seconds, tram, device, cabin, start, end,
             category, category)).fetchall()

    def journal_count(self, tram: str, device: str, cabin: str, start: datetime,
                      end: datetime, category: str | None, starts_only: bool = False) -> int:
        """Считает также эпизоды, начавшиеся до окна и пересекающие его."""
        return self.connection.execute("""SELECT count(*) AS n FROM published_episodes
            WHERE loco_number=%s AND device_id=%s AND cabin_type=%s
              AND first_bad_at < %s AND last_bad_at >= %s
              AND (%s::text IS NULL OR category=%s::text)
              AND (NOT %s OR first_bad_at >= %s)""",
            (tram, device, cabin, end, start, category, category, starts_only, start)).fetchone()["n"]

    def journal(self, tram: str, device: str, cabin: str, start: datetime,
                end: datetime, category: str | None, sort: str, page: int,
                page_size: int = 20, starts_only: bool = False) -> list[dict]:
        """Возвращает одну страницу журнала с проверенной сортировкой."""
        order = {"newest": "first_bad_at DESC, id DESC",
                 "oldest": "first_bad_at ASC, id ASC",
                 "last": "last_bad_at DESC, id DESC"}.get(sort)
        if order is None or page < 1 or not 1 <= page_size <= 100:
            raise ValueError("Некорректная сортировка или страница")
        sql = """SELECT id, category, signal, first_bad_at, last_bad_at,
            device_id, cabin_type, bad_count, segment_count, bridged_gap_seconds,
            close_reason FROM published_episodes
            WHERE loco_number=%s AND device_id=%s AND cabin_type=%s
              AND first_bad_at < %s AND last_bad_at >= %s
              AND (%s::text IS NULL OR category=%s::text)
              AND (NOT %s OR first_bad_at >= %s) ORDER BY """ + order + " LIMIT %s OFFSET %s"
        return self.connection.execute(sql, (tram, device, cabin, end, start,
                                              category, category, starts_only, start, page_size,
                                              (page - 1) * page_size)).fetchall()

    def episode(self, episode_id: int) -> dict | None:
        """Не раскрывает эпизод из неопубликованного запуска."""
        return self.connection.execute("SELECT * FROM published_episodes WHERE id=%s",
                                       (episode_id,)).fetchone()

    def episode_segments(self, episode_id: int) -> list[dict]:
        """Сегменты выбранного опубликованного эпизода в исходном порядке."""
        return self.connection.execute("""SELECT s.*, link.ordinal FROM episode_segments link
            JOIN published_episodes e ON e.id=link.episode_id
            JOIN published_segments s ON s.id=link.segment_id
            WHERE e.id=%s ORDER BY link.ordinal LIMIT 1000""", (episode_id,)).fetchall()

    def segment_sources(self, segment_id: int) -> list[dict]:
        """Исходные значения и ссылки Track только опубликованного сегмента."""
        return self.connection.execute("""SELECT o.track_record_id, o.state,
            o.value_present, o.raw_value, o.context, t.observed_at,
            t.source_file, t.event_index FROM segment_observations o
            JOIN published_segments s ON s.id=o.segment_id
            JOIN track_records t ON t.id=o.track_record_id
            WHERE s.id=%s ORDER BY t.observed_at, t.id LIMIT 500""",
            (segment_id,)).fetchall()

    def timeline_segments(self, tram: str, device: str, cabin: str,
                          start: datetime, end: datetime) -> list[dict]:
        """Даёт наблюдаемые проблемные сегменты, не растягивая их на весь эпизод."""
        return self.connection.execute("""SELECT signal, category, first_bad_at,
            last_bad_at, close_reason FROM published_segments
            WHERE loco_number=%s AND device_id=%s AND cabin_type=%s
              AND first_bad_at < %s AND last_bad_at >= %s
            ORDER BY first_bad_at, id LIMIT 1000""",
            (tram, device, cabin, end, start)).fetchall()

    def timeline_observations(self, tram: str, device: str, cabin: str,
                              start: datetime, end: datetime,
                              bucket_seconds: int) -> list[dict]:
        """Агрегирует состояния Track в БД; браузер получает не сырые снимки."""
        return self.connection.execute("""WITH observations AS (
          SELECT date_bin(make_interval(secs => %s), t.observed_at,
              TIMESTAMPTZ '2000-01-01 00:00:00+00') AS bucket,
              v.signal, v.state
          FROM track_records t CROSS JOIN LATERAL (VALUES
            ('m_danger_obj_status', CASE WHEN t.attributes->>'m_danger_obj_status'='Error' THEN 'bad' WHEN t.attributes->>'m_danger_obj_status'='ProperlyWork' THEN 'clear' ELSE 'unknown' END),
            ('m_trafficlight_status', CASE WHEN t.attributes->>'m_trafficlight_status'='Error' THEN 'bad' WHEN t.attributes->>'m_trafficlight_status'='ProperlyWork' THEN 'clear' ELSE 'unknown' END),
            ('m_speed_limit_status', CASE WHEN t.attributes->>'m_speed_limit_status'='Error' THEN 'bad' WHEN t.attributes->>'m_speed_limit_status'='ProperlyWork' THEN 'clear' ELSE 'unknown' END),
            ('ubloxGps', CASE WHEN t.attributes->'ubloxGps'='false'::jsonb THEN 'bad' WHEN t.attributes->'ubloxGps'='true'::jsonb THEN 'clear' ELSE 'unknown' END),
            ('leftImage', CASE WHEN t.attributes->'leftImage'='false'::jsonb THEN 'bad' WHEN t.attributes->'leftImage'='true'::jsonb THEN 'clear' ELSE 'unknown' END),
            ('multiImages', CASE WHEN t.attributes->'multiImages'='false'::jsonb THEN 'bad' WHEN t.attributes->'multiImages'='true'::jsonb THEN 'clear' ELSE 'unknown' END),
            ('minsEth', CASE WHEN t.attributes->'minsEth'='false'::jsonb THEN 'bad' WHEN t.attributes->'minsEth'='true'::jsonb THEN 'clear' ELSE 'unknown' END),
            ('dbwFbTram', CASE WHEN t.attributes->'dbwFbTram'='false'::jsonb THEN 'bad' WHEN t.attributes->'dbwFbTram'='true'::jsonb THEN 'clear' ELSE 'unknown' END),
            ('roadModel', CASE WHEN t.attributes->'roadModel'='false'::jsonb THEN 'bad' WHEN t.attributes->'roadModel'='true'::jsonb THEN 'clear' ELSE 'unknown' END),
            ('tramPlannerService', CASE WHEN t.attributes->'tramPlannerService'='false'::jsonb THEN 'bad' WHEN t.attributes->'tramPlannerService'='true'::jsonb THEN 'clear' ELSE 'unknown' END)
          ) v(signal,state)
          WHERE t.loco_number=%s AND t.device_id=%s AND t.cabin_type=%s
            AND t.observed_at >= %s AND t.observed_at < %s
            AND t.id <= (SELECT a.max_track_record_id FROM analysis_runs a
              JOIN published_analysis p ON p.analysis_run_id=a.id
              WHERE a.status='completed')
        ) SELECT bucket, signal, bool_or(state='bad') AS has_bad,
            bool_or(state='clear') AS has_clear,
            bool_or(state='unknown') AS has_unknown, count(*) AS snapshots
          FROM observations GROUP BY bucket,signal ORDER BY bucket,signal LIMIT 3000""",
            (bucket_seconds, tram, device, cabin, start, end)).fetchall()

    def timeline_states(self, tram: str, device: str, cabin: str,
                        start: datetime, end: datetime) -> list[dict]:
        """Сжимает снимки в наблюдаемые серии, сохраняя отключения и разрывы.

        Это подготовка изображения, а не пересчёт диагностических сегментов.
        Противоречия одного времени становятся неопределённостью независимо от ID.
        Возвращает максимум 5001 серию: последняя нужна для проверки ограничения.
        """
        return self.connection.execute("""WITH expanded AS (
          SELECT t.observed_at, v.signal,
            CASE
              WHEN NOT (t.attributes ? v.signal) THEN 'unknown'
              WHEN t.attributes->>v.signal IN ('TurnOff','TemporaryOff') THEN 'off'
              WHEN t.attributes->>v.signal='Error' OR
                   t.attributes->v.signal='false'::jsonb THEN 'bad'
              WHEN t.attributes->>v.signal='ProperlyWork' OR
                   t.attributes->v.signal='true'::jsonb THEN 'clear'
              ELSE 'unknown'
            END AS state,
            CASE WHEN NOT (t.attributes ? v.signal) THEN 'Поле отсутствует'
                 ELSE left((t.attributes->v.signal)::text,100) END AS detail
          FROM track_records t CROSS JOIN LATERAL unnest(ARRAY[
            'm_danger_obj_status','m_trafficlight_status','m_speed_limit_status',
            'ubloxGps','leftImage','multiImages','minsEth','dbwFbTram',
            'roadModel','tramPlannerService']) v(signal)
          WHERE t.loco_number=%s AND t.device_id=%s AND t.cabin_type=%s
            AND t.observed_at >= %s AND t.observed_at < %s
            AND t.id <= (SELECT a.max_track_record_id FROM analysis_runs a
              JOIN published_analysis p ON p.analysis_run_id=a.id WHERE a.status='completed')
        ), moments AS (
          SELECT observed_at, signal,
            CASE WHEN count(DISTINCT state)=1 AND count(DISTINCT detail)=1
                 THEN min(state) ELSE 'conflict' END AS state,
            string_agg(DISTINCT detail, ', ' ORDER BY detail) AS detail,
            count(*) AS snapshots
          FROM expanded GROUP BY observed_at,signal
        ), previous AS (
          SELECT *, lag(observed_at) OVER w AS previous_at,
            lag(state) OVER w AS previous_state, lag(detail) OVER w AS previous_detail
          FROM moments WINDOW w AS (PARTITION BY signal ORDER BY observed_at)
        ), numbered AS (
          SELECT *, sum(CASE WHEN previous_at IS NULL OR
            observed_at-previous_at > INTERVAL '30 seconds' OR
            state <> previous_state OR detail <> previous_detail THEN 1 ELSE 0 END)
            OVER (PARTITION BY signal ORDER BY observed_at) AS series
          FROM previous
        )
        SELECT signal,state,detail,min(observed_at) AS first_at,
          max(observed_at) AS last_at,sum(snapshots) AS snapshots
        FROM numbered GROUP BY signal,state,detail,series
        ORDER BY signal,first_at LIMIT 5001""", (tram,device,cabin,start,end)).fetchall()
