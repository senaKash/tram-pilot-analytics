"""Версионированный расчёт сегментов и эпизодов поверх Track в PostgreSQL."""
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from .analyzers import ANALYZERS
from .grouping import EpisodeBuilder, SegmentBuilder
from .models import Segment
from ..database.analysis_repository import AnalysisRepository

PIPELINE_VERSION = "2"


@dataclass(frozen=True)
class AnalysisOptions:
    gap_seconds: float = 30
    bridge_seconds: float = 90

    def __post_init__(self):
        if self.gap_seconds <= 0 or self.bridge_seconds <= 0:
            raise ValueError("Пороги должны быть положительными")


class AnalysisPipeline:
    """Оркестрирует чистые анализаторы и группировщики, сохраняя версии в БД."""

    def __init__(self, connection, options: AnalysisOptions = AnalysisOptions()):
        self.connection = connection
        self.options = options

    def run(self) -> dict:
        """Создаёт новый запуск; при ошибке откатывает все результаты запуска."""
        conn = self.connection
        versions = {item.identifier: item.version for item in ANALYZERS}
        parameters = {"gap_seconds": self.options.gap_seconds,
                      "bridge_seconds": self.options.bridge_seconds}
        run_id = conn.execute("""INSERT INTO analysis_runs
            (status, pipeline_version, analyzer_versions, parameters)
            VALUES ('running', %s, %s, %s) RETURNING id""",
            (PIPELINE_VERSION, Jsonb(versions), Jsonb(parameters))).fetchone()["id"]
        conn.commit()
        counts = {"track_count": 0, "segment_count": 0, "episode_count": 0}
        try:
            with conn.transaction():
                # Снимок и ограничение по ID не позволяют смешать разные состояния импорта.
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                maximum = conn.execute("SELECT COALESCE(max(id),0) AS n FROM track_records").fetchone()["n"]
                conn.execute("UPDATE analysis_runs SET max_track_record_id=%s WHERE id=%s",
                             (maximum, run_id))
                repo = AnalysisRepository(conn, run_id)
                self._build_segments(repo, counts)
                self._build_episodes(repo, counts)
                conn.execute("""UPDATE analysis_runs SET status='completed', finished_at=now(),
                    track_count=%s, segment_count=%s, episode_count=%s WHERE id=%s""",
                    (*counts.values(), run_id))
        except BaseException as exc:
            conn.rollback()
            conn.execute("""UPDATE analysis_runs SET status='failed', finished_at=now(),
                error_message=%s WHERE id=%s""", (type(exc).__name__, run_id))
            conn.commit()
            raise
        return {"run_id": run_id, "status": "completed", **counts}

    def _build_segments(self, repo: AnalysisRepository, counts: dict) -> None:
        group = None
        builders: dict[tuple, SegmentBuilder] = {}

        def finish_group():
            for builder in builders.values():
                if segment := builder.finish():
                    if segment.closing_observation:
                        repo.add_observation(segment.db_id, segment.closing_observation)
                    repo.close_segment(segment)

        for track in repo.tracks():
            identity = (track["loco_number"], track["device_id"], track["cabin_type"])
            if identity != group:
                finish_group()
                builders.clear()
                group = identity
            counts["track_count"] += 1
            for analyzer in ANALYZERS:
                for observation in analyzer.observe(track):
                    builder = builders.setdefault(observation.key,
                                                  SegmentBuilder(self.options.gap_seconds))
                    closed, active, created = builder.feed(observation)
                    if closed:
                        if closed.closing_observation:
                            repo.add_observation(closed.db_id, closed.closing_observation)
                        repo.close_segment(closed)
                    if active:
                        if created:
                            active.db_id = repo.create_segment(observation.key, active)
                            counts["segment_count"] += 1
                        repo.add_observation(active.db_id, observation)
        finish_group()

    def _build_episodes(self, repo: AnalysisRepository, counts: dict) -> None:
        group = None
        builder = EpisodeBuilder(self.options.bridge_seconds)
        for row in repo.segments():
            key = tuple(row[k] for k in ("loco_number", "device_id", "cabin_type",
                                         "category", "signal"))
            if key != group:
                if episode := builder.finish():
                    repo.close_episode(episode)
                builder = EpisodeBuilder(self.options.bridge_seconds)
                group = key
            segment = Segment(row["first_bad_at"], row["last_bad_at"],
                              row["first_track_id"], row["last_track_id"],
                              row["bad_count"], row["close_reason"], row["clear_track_id"],
                              row["id"])
            closed, active, created = builder.feed(segment)
            if closed:
                repo.close_episode(closed)
            if created:
                active.db_id = repo.create_episode(key, active)
                counts["episode_count"] += 1
            repo.add_episode_segment(active.db_id, segment.db_id, active.segment_count)
        if episode := builder.finish():
            repo.close_episode(episode)
