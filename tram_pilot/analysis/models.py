"""Небольшие модели для чистой логики группировки."""
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Observation:
    """Состояние одного сигнала в одном исходном Track."""
    track_id: int
    at: datetime
    loco_number: str
    device_id: str
    cabin_type: str
    category: str
    signal: str
    state: str  # bad, clear, unknown
    value_present: bool
    raw_value: Any
    context: dict

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (self.loco_number, self.device_id, self.cabin_type,
                self.category, self.signal)


@dataclass
class Segment:
    """Непрерывный по наблюдениям отрезок одного проблемного сигнала."""
    first_bad_at: datetime
    last_bad_at: datetime
    first_track_id: int
    last_track_id: int
    bad_count: int = 1
    close_reason: str = "sample_end"
    clear_track_id: int | None = None
    db_id: int | None = None
    closing_observation: Observation | None = None


@dataclass
class Episode:
    """Кандидатный эпизод из одного или нескольких сегментов."""
    first_bad_at: datetime
    last_bad_at: datetime
    first_track_id: int
    last_track_id: int
    bad_count: int
    segment_count: int = 1
    bridged_gap_seconds: float = 0.0
    close_reason: str = "sample_end"
    db_id: int | None = None
