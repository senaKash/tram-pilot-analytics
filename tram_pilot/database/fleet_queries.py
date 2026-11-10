"""Агрегаты парка для конкретного опубликованного снимка анализа."""
from datetime import datetime
from math import ceil

class FleetQueries:
    """Читает покрытие и матрицу; не соединяет отдельные Track с эпизодами."""

    def __init__(self, connection) -> None:
        self.connection = connection

    def coverage(self, run_id: int) -> dict | None:
        """Возвращает фактический охват Track, включая трамваи без эпизодов."""
        row = self.connection.execute("""SELECT min(t.observed_at) AS first_at,
            max(t.observed_at) AS last_at, count(DISTINCT t.loco_number) AS tram_count
            FROM analysis_runs a
            JOIN published_analysis p ON p.analysis_run_id=a.id
            JOIN track_records t ON t.id <= a.max_track_record_id
            WHERE a.id=%s AND a.status='completed'""", (run_id,)).fetchone()
        return row if row["first_at"] is not None else None

    def vehicles(self, run_id: int) -> list[str]:
        """Список доступных трамваев снимка, включая машины без эпизодов."""
        rows = self.connection.execute("""SELECT DISTINCT t.loco_number
            FROM track_records t JOIN analysis_runs a ON t.id <= a.max_track_record_id
            JOIN published_analysis p ON p.analysis_run_id=a.id
            WHERE a.id=%s AND a.status='completed' ORDER BY t.loco_number""",
            (run_id,)).fetchall()
        return [row["loco_number"] for row in rows]

    def devices(self, run_id: int, tram: str) -> list[tuple[str, str]]:
        """Источники Track выбранной машины, даже если анализ не нашёл эпизодов."""
        rows = self.connection.execute("""SELECT DISTINCT t.device_id,t.cabin_type
            FROM track_records t JOIN analysis_runs a ON t.id <= a.max_track_record_id
            JOIN published_analysis p ON p.analysis_run_id=a.id
            WHERE a.id=%s AND a.status='completed' AND t.loco_number=%s
            ORDER BY t.device_id,t.cabin_type""", (run_id,tram)).fetchall()
        return [(row["device_id"],row["cabin_type"]) for row in rows]

    def matrix(self, run_id: int, start: datetime, end: datetime,
               seconds: int, category: str | None = None, search: str = "") -> list[dict]:
        """Возвращает общие корзины [start,end), не более 24 на каждый трамвай.

        Track и эпизоды агрегируются независимо. Список трамваев берётся из всего
        опубликованного снимка, поэтому пустой период не скрывает строки парка.
        Если публикация переключилась, старый ID не подменяется новой версией.
        """
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError("Период должен иметь часовой пояс и положительную длину")
        if (run_id < 1 or seconds < 1 or
                ceil((end-start).total_seconds()/seconds) > 24):
            raise ValueError("Некорректный запуск или больше 24 временных корзин")
        if category not in (None, "subsystem_error", "sensor_freshness"):
            raise ValueError("Неизвестная категория")
        return self.connection.execute("""WITH parameters AS (
            SELECT %s::timestamptz AS start_at, %s::timestamptz AS end_at,
                make_interval(secs => %s) AS step, %s::text AS category,
                %s::text AS search
        ), selected_run AS (
            SELECT a.id,a.max_track_record_id FROM analysis_runs a
            JOIN published_analysis p ON p.analysis_run_id=a.id
            WHERE a.id=%s AND a.status='completed'
        ), vehicles AS (
            SELECT DISTINCT t.loco_number FROM track_records t
            CROSS JOIN selected_run a CROSS JOIN parameters p
            WHERE t.id <= a.max_track_record_id
              AND strpos(lower(t.loco_number),lower(p.search)) > 0
        ), bins AS (
            SELECT bucket AS bucket_start, least(bucket+p.step,p.end_at) AS bucket_end
            FROM parameters p CROSS JOIN LATERAL
                generate_series(p.start_at,p.end_at,p.step) b(bucket)
            WHERE bucket < p.end_at
        ), tracks AS (
            SELECT t.loco_number,date_bin(p.step,t.observed_at,p.start_at) AS bucket_start,
                count(*) AS track_count
            FROM track_records t JOIN vehicles v USING (loco_number)
            CROSS JOIN selected_run a CROSS JOIN parameters p
            WHERE t.id <= a.max_track_record_id
              AND t.observed_at >= p.start_at AND t.observed_at < p.end_at
            GROUP BY t.loco_number,bucket_start
        ), problems AS (
            SELECT e.loco_number,date_bin(p.step,e.first_bad_at,p.start_at) AS bucket_start,
                count(*) FILTER (WHERE e.category='subsystem_error') AS error_count,
                count(*) FILTER (WHERE e.category='sensor_freshness') AS freshness_count
            FROM episodes e JOIN selected_run a ON a.id=e.analysis_run_id
            JOIN vehicles v USING (loco_number) CROSS JOIN parameters p
            WHERE e.first_bad_at >= p.start_at AND e.first_bad_at < p.end_at
              AND (p.category IS NULL OR e.category=p.category)
            GROUP BY e.loco_number,bucket_start
        ) SELECT v.loco_number,b.bucket_start,b.bucket_end,
            coalesce(e.error_count,0) AS error_count,
            coalesce(e.freshness_count,0) AS freshness_count,
            coalesce(t.track_count,0) AS track_count
        FROM vehicles v CROSS JOIN bins b
        LEFT JOIN tracks t ON t.loco_number=v.loco_number AND t.bucket_start=b.bucket_start
        LEFT JOIN problems e ON e.loco_number=v.loco_number AND e.bucket_start=b.bucket_start
        ORDER BY v.loco_number,b.bucket_start""",
            (start,end,seconds,category,search,run_id)).fetchall()
