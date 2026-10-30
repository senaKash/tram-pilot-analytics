#!/usr/bin/env python3
"""Read Tram Pilot JSON samples directly from a .tar.zst stream.

No third-party Python modules. Requires Python 3.14+ or `zstd` on PATH.
This is a format/quality profiler, NOT a certified incident detector.
"""

import argparse
import json
import sys
import tarfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    from ._archive import open_tar_zst, StopScan
except ImportError:
    from _archive import open_tar_zst, StopScan

SUBSYSTEMS = (
    "m_danger_obj_status", "m_trafficlight_status", "m_speed_limit_status"
)
DEVICES = (
    "minsEth", "leftImage", "tramPlannerService", "dbwFbTram", "roadModel",
    "multiImages", "ubloxGps", "odoFbTram"
)


def preview(value, depth=0):
    """Bounded, readable representation for samples, not a transformed data model."""
    if depth >= 4:
        return "<nested>"
    if isinstance(value, dict):
        keys = list(value)
        result = {str(k): preview(value[k], depth + 1) for k in keys[:50]}
        if len(keys) > 50:
            result["__more_fields__"] = len(keys) - 50
        return result
    if isinstance(value, list):
        result = [preview(x, depth + 1) for x in value[:4]]
        if len(value) > 4:
            result.append(f"<and {len(value) - 4} more elements>")
        return result
    if isinstance(value, str) and len(value) > 350:
        return value[:350] + "...<truncated>"
    return value


def get_timestamp(d):
    s = d.get("timestamp")
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def scan(archive, limit, inspect=None, max_preview=16000, output=Path("data/profile")):
    root_shapes = Counter()
    root_events = Counter()
    tram_counts = Counter()
    root_keys = Counter()
    root_types = defaultdict(Counter)
    subsystem_statuses = defaultdict(Counter)
    subsystem_combos = Counter()
    stale_devices = Counter()
    error_trams = Counter()
    error_with_adas = Counter()
    nested_shapes = Counter()
    nested_event_types = Counter()
    nested_event_keys = Counter()
    nested_element_count = 0
    parse_errors = 0
    too_large = 0
    file_count = 0
    non_files = 0
    first_timestamp = None
    last_timestamp = None
    first_seen = None
    last_seen = None
    samples = {"event_containers": [], "nested_elements": [], "errors": [],
               "other_objects": [], "invalid_json": []}
    t0 = time.perf_counter()

    try:
        with open_tar_zst(archive) as tf:
            for member in tf:
                if not member.isfile():
                    non_files += 1
                    continue
                if inspect is None and file_count >= limit:
                    raise StopScan()
                if inspect is not None and member.name.removeprefix("./") != inspect.removeprefix("./"):
                    continue
                file_count += 1
                if member.size > 50 * 1024 * 1024:
                    too_large += 1
                    if inspect is not None:
                        print(f"{member.name}: >50 МиБ, просмотр прерван")
                        raise StopScan(1)
                    continue
                f = tf.extractfile(member)
                if f is None:
                    continue
                try:
                    d = json.load(f)
                except (ValueError, UnicodeDecodeError) as exc:
                    parse_errors += 1
                    if len(samples["invalid_json"]) < 3:
                        samples["invalid_json"].append({"file": member.name,
                                                        "error": str(exc)[:160]})
                    if inspect is not None:
                        print(f"{member.name}: ошибка JSON: {exc}")
                        raise StopScan(1)
                    continue
                finally:
                    f.close()

                if inspect is not None:
                    raw = json.dumps(d, ensure_ascii=False, indent=2)
                    print(f"=== {member.name} ({member.size} байт) ===")
                    print(raw[:max_preview])
                    if len(raw) > max_preview:
                        print(f"\n... [показано {max_preview} из {len(raw)} символов]")
                    raise StopScan(0)

                if not isinstance(d, dict):
                    root_shapes[type(d).__name__] += 1
                    if len(samples["other_objects"]) < 3:
                        samples["other_objects"].append({"file": member.name, "value": preview(d)})
                    continue

                for k, v in d.items():
                    root_keys[k] += 1
                    root_types[k][type(v).__name__] += 1
                timestamp = get_timestamp(d)
                if timestamp is not None:
                    if first_timestamp is None or timestamp < first_timestamp:
                        first_timestamp = timestamp
                    if last_timestamp is None or timestamp > last_timestamp:
                        last_timestamp = timestamp
                    if first_seen is None:
                        first_seen = d.get("timestamp")
                    last_seen = d.get("timestamp")

                if "events" in d:
                    root_shapes["events_container"] += 1
                    evs = d["events"]
                    nested_shapes[type(evs).__name__] += 1
                    if len(samples["event_containers"]) < 4:
                        samples["event_containers"].append({"file": member.name,
                                                             "value": preview(d)})
                    if isinstance(evs, list):
                        nested_element_count += len(evs)
                        for ev in evs:
                            if isinstance(ev, dict):
                                nested_event_types[str(ev.get("event_type", "<missing>"))] += 1
                                for k in ev:
                                    nested_event_keys[k] += 1
                                if len(samples["nested_elements"]) < 6:
                                    samples["nested_elements"].append({"file": member.name,
                                                                       "value": preview(ev)})
                            else:
                                nested_event_types[f"<{type(ev).__name__}>"] += 1
                    continue

                if "event_type" not in d:
                    root_shapes["other_object"] += 1
                    if len(samples["other_objects"]) < 3:
                        samples["other_objects"].append({"file": member.name, "value": preview(d)})
                    continue

                root_shapes["direct_event"] += 1
                root_events[str(d.get("event_type"))] += 1
                tram_counts[str(d.get("loco_number", "<missing>"))] += 1
                statuses = tuple(str(d.get(k, "<missing>")) for k in SUBSYSTEMS)
                subsystem_combos[statuses] += 1
                for key, value in zip(SUBSYSTEMS, statuses):
                    subsystem_statuses[key][value] += 1
                for key in DEVICES:
                    if str(d.get(key, "")).lower() == "false":
                        stale_devices[key] += 1
                if "Error" in statuses:
                    tram = str(d.get("loco_number", "<missing>"))
                    error_trams[tram] += 1
                    error_with_adas[str(d.get("is_adas_button_on", "<missing>"))] += 1
                    if len(samples["errors"]) < 8:
                        samples["errors"].append({
                            "file": member.name,
                            "timestamp": d.get("timestamp"),
                            "loco_number": d.get("loco_number"),
                            "statuses": dict(zip(SUBSYSTEMS, statuses)),
                            "requested": {k: d.get(k) for k in (
                                "m_danger_obj_req", "m_trafficlight_req", "m_speed_limit_req")},
                            "is_adas_button_on": d.get("is_adas_button_on"),
                            "is_loc_converged": d.get("is_loc_converged"),
                            "ubloxGps": d.get("ubloxGps"),
                            "speed": d.get("speed"),
                        })
    except StopScan as stop:
        if inspect is not None:
            return stop.args[0] if stop.args else 0

    if inspect is not None:
        print(f"Не найден файл: {inspect}")
        return 1

    elapsed = round(time.perf_counter() - t0, 3)
    results = {
        "archive": str(archive), "limit": limit, "files_checked": file_count,
        "non_file_members": non_files, "json_parse_errors": parse_errors,
        "oversized_files_skipped": too_large, "scan_seconds": elapsed,
        "root_shapes": dict(root_shapes),
        "direct_event_types": dict(root_events),
        "tram_numbers": dict(tram_counts.most_common()),
        "root_field_presence": dict(root_keys.most_common()),
        "root_field_json_types": {k: dict(v) for k, v in sorted(root_types.items())},
        "status_counts": {k: dict(v) for k, v in subsystem_statuses.items()},
        "status_combinations": [
            {"statuses": list(st), "count": n}
            for st, n in subsystem_combos.most_common()
        ],
        "device_stale_record_counts": dict(stale_devices.most_common()),
        "records_with_error_by_tram": dict(error_trams.most_common()),
        "error_records_by_adas_button": dict(error_with_adas),
        "nested_events_container_shapes": dict(nested_shapes),
        "nested_list_element_count": nested_element_count,
        "nested_event_types": dict(nested_event_types.most_common()),
        "nested_event_keys": dict(nested_event_keys.most_common()),
        "min_utc_timestamp": first_timestamp.isoformat() if first_timestamp else None,
        "max_utc_timestamp": last_timestamp.isoformat() if last_timestamp else None,
        "first_seen_timestamp": first_seen,
        "last_seen_timestamp": last_seen,
        "note": "Counts refer to JSON records, not to distinct system failures. Samples are bounded."
    }
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    out_profile = output / "tram_pilot_profile.json"
    out_samples = output / "tram_pilot_samples.json"
    out_profile.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    out_samples.write_text(json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=== TRAM PILOT: ЭТАП 1 / РАЗВЕДКА ФОРМАТА ===")
    print(f"Файлов: {file_count}; ошибок JSON: {parse_errors}; секунд: {elapsed}")
    print("Структуры файлов:", dict(root_shapes))
    print("Прямые типы событий:", dict(root_events))
    print("Вложенное events:", dict(nested_shapes))
    print("Элементов в списках events:", nested_element_count)
    print("Типы вложенных событий:", dict(nested_event_types.most_common(12)))
    print("UTC-диапазон:", results["min_utc_timestamp"], "—", results["max_utc_timestamp"])
    print("Трамваев:", len(tram_counts))
    print("Error по трамваям:", dict(error_trams.most_common(12)))
    print("Комбинации состояний:")
    for item in results["status_combinations"][:8]:
        print(f"  {item['count']:>7}  {item['statuses']}")
    print("Неактуальные данные устройств (число записей):", dict(stale_devices))
    print(f"\nСохранено: {out_profile.resolve()}")
    print(f"Сохранено: {out_samples.resolve()}")
    print("Примечание: эти числа — строки телеметрии, не количество самостоятельных отказов.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Потоковая разведка формата Tram Pilot tar.zst")
    parser.add_argument("archive", nargs="?", default="movement_data_export.tar.zst",
                        help="Путь к .tar.zst")
    parser.add_argument("--limit", type=int, default=20000,
                        help="Сколько файлов прочитать для выборки (по умолчанию 20000)")
    parser.add_argument("--inspect-file", default=None,
                        help="Вывести один JSON из архива, например direct_track.json")
    parser.add_argument("--max-preview", type=int, default=16000,
                        help="Максимум символов при --inspect-file")
    parser.add_argument("--output", default="data/profile", help="Каталог локальных отчётов и образцов")
    args = parser.parse_args()
    if not Path(args.archive).is_file():
        parser.error(f"Архив не найден: {args.archive}")
    if args.limit < 1 or args.max_preview < 100:
        parser.error("--limit должен быть >=1, --max-preview >=100")
    return scan(args.archive, args.limit, args.inspect_file, args.max_preview, Path(args.output))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, tarfile.TarError, RuntimeError) as exc:
        print(f"Ошибка чтения архива: {exc}", file=sys.stderr)
        sys.exit(2)
