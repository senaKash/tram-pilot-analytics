"""Нормализованные записи Track и событий с ключами дедупликации."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json


@dataclass(frozen=True)
class TrackRecord:
    """Track с исходным файлом и позицией для учёта происхождения."""
    observed_at: datetime
    loco_number: str
    device_id: str
    cabin_type: str
    attributes: dict
    source_file: str
    event_index: int

    @property
    def record_hash(self) -> str:
        # Происхождение не входит в ключ: одинаковые Track из разных файлов совпадают.
        content = [self.observed_at.isoformat(timespec="microseconds"),
                   self.loco_number, self.device_id, self.cabin_type, self.attributes]
        encoded = json.dumps(content, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class EventRecord:
    """Отдельное событие с позицией в исходном контейнере."""
    occurred_at: datetime
    loco_number: str
    event_type: str
    payload: dict
    source_name: str
    source_file: str
    event_index: int

    @property
    def record_hash(self) -> str:
        # Позиция и источник входят в ключ: два одинаковых события в массиве
        # остаются двумя наблюдаемыми событиями, а повторный импорт не дублирует их.
        content = [self.source_name, self.source_file, self.event_index,
                   self.occurred_at.isoformat(timespec="microseconds"),
                   self.loco_number, self.event_type, self.payload]
        encoded = json.dumps(content, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
