"""Нормализация без превращения отсутствующих ключей в null."""
from datetime import datetime, timezone
import json
from pathlib import Path

from scripts.tram_pilot_ingest import BOOLEAN, FLOAT, INTEGER, as_bool, as_float, as_int
from .models import EventRecord, TrackRecord

IDENTITY = ("loco_number", "device_id", "cabin_type")
CONVERTERS = {**dict.fromkeys(BOOLEAN, as_bool), **dict.fromkeys(FLOAT, as_float),
              **dict.fromkeys(INTEGER, as_int)}
EVENT_TYPES = {"Warn", "Brake", "OverSpeed"}


def observed_time(record: dict) -> datetime:
    """Возвращает момент UTC; время без часового пояса недопустимо."""
    timestamp = record.get("timestamp")
    if not isinstance(timestamp, str):
        raise ValueError("Отсутствует timestamp")
    try:
        observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Некорректный timestamp") from exc
    if observed.tzinfo is None:
        raise ValueError("timestamp должен содержать часовой пояс")
    return observed.astimezone(timezone.utc)


def normalized_fields(record: dict, excluded: tuple[str, ...]) -> dict:
    """Нормализует только присутствующие ключи, сохраняя явный null."""
    attributes = {}
    for key, value in record.items():
        if key in excluded:
            continue
        converted = CONVERTERS[key](value) if key in CONVERTERS and value is not None else value
        if value is not None and key in CONVERTERS and converted is None:
            raise ValueError(f"Некорректный тип или значение поля {key}")
        attributes[key] = converted
    json.dumps(attributes, allow_nan=False)
    return attributes


def normalize_track(record: dict, source_file: str, event_index: int = 0) -> TrackRecord:
    if not isinstance(record, dict) or record.get("event_type") != "Track":
        raise ValueError("Ожидалась запись event_type=Track")
    identity = []
    for key in IDENTITY:
        value = record.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Отсутствует или некорректен идентификатор {key}")
        identity.append(value)
    attributes = normalized_fields(record, (*IDENTITY, "timestamp"))
    return TrackRecord(observed_time(record), *identity, attributes,
                       source_file, event_index)


def normalize_event(record: dict, source_name: str, source_file: str,
                    event_index: int) -> EventRecord:
    """Нормализует Warn, Brake или OverSpeed без классификации как неисправности."""
    if (not isinstance(record, dict) or not isinstance(record.get("event_type"), str)
            or record["event_type"] not in EVENT_TYPES):
        raise ValueError("Ожидалось событие Warn, Brake или OverSpeed")
    loco = record.get("loco_number")
    if not isinstance(loco, str) or not loco.strip():
        raise ValueError("Отсутствует или некорректен loco_number")
    payload = normalized_fields(record, ("timestamp", "loco_number", "event_type"))
    return EventRecord(observed_time(record), loco, record["event_type"], payload,
                       source_name, source_file, event_index)


def load_demo(directory: Path, max_records: int = 1000) -> tuple[list[TrackRecord], int]:
    """Читает ограниченный набор JSON; события считает, но не импортирует."""
    if max_records < 1:
        raise ValueError("max_records должен быть положительным")
    directory = Path(directory)
    files = sorted(directory.glob("*.json")) if directory.is_dir() else [directory]
    if not files or len(files) > 100:
        raise ValueError("Ожидалось от 1 до 100 демонстрационных JSON")
    tracks, skipped, count = [], 0, 0

    def invalid_constant(_value):
        raise ValueError("JSON содержит нечисловую константу")

    for path in files:
        if path.suffix.lower() != ".json" or path.stat().st_size > 5 * 1024 * 1024:
            raise ValueError("Демо-импорт принимает только JSON до 5 МиБ каждый")
        with path.open(encoding="utf-8-sig") as stream:
            obj = json.load(stream, parse_constant=invalid_constant)
        if not isinstance(obj, dict):
            raise ValueError(f"{path.name}: ожидался JSON-объект")
        rows = obj["events"] if "events" in obj else [obj]
        if not isinstance(rows, list):
            raise ValueError(f"{path.name}: events должен быть массивом")
        for index, row in enumerate(rows):
            count += 1
            if count > max_records:
                raise ValueError("Превышен лимит демонстрационных записей")
            if not isinstance(row, dict) or not isinstance(row.get("event_type"), str):
                raise ValueError(f"{path.name}: некорректная запись {index}")
            if row["event_type"] != "Track":
                skipped += 1
                continue
            try:
                tracks.append(normalize_track(row, path.name, index))
            except ValueError as exc:
                raise ValueError(f"{path.name}, запись {index}: {exc}") from exc
    return tracks, skipped
