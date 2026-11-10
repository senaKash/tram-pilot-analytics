"""Время, подписи и границы запросов рабочего экрана."""
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
CATEGORIES = {None: "Все категории", "subsystem_error": "Ошибки подсистем",
              "sensor_freshness": "Актуальность данных"}
SIGNALS = {
    "m_danger_obj_status": "Обнаружение препятствий",
    "m_trafficlight_status": "Контроль светофоров",
    "m_speed_limit_status": "Ограничение скорости",
    "ubloxGps": "GPS", "leftImage": "Камера", "multiImages": "Камера: несколько изображений",
    "minsEth": "Сеть", "dbwFbTram": "CAN", "roadModel": "Модель дороги",
    "tramPlannerService": "Планировщик",
}
CLOSE_REASONS = {"observed_clearance": "Наблюдалось снятие признака",
                 "telemetry_gap": "Разрыв наблюдений", "unknown_state": "Неизвестное состояние",
                 "sample_end": "Конец выборки"}


@dataclass(frozen=True)
class TimeWindow:
    """Полуоткрытый интервал UTC для всех запросов страницы."""
    start: datetime
    end: datetime

    def __post_init__(self):
        if (self.start.tzinfo is None or self.end.tzinfo is None
                or self.start >= self.end):
            raise ValueError("Период должен иметь часовой пояс и положительную длину")

    def clipped(self, outer: "TimeWindow") -> "TimeWindow | None":
        """Ограничивает выделение исходным периодом без нулевой длины."""
        first, last = max(self.start, outer.start), min(self.end, outer.end)
        return TimeWindow(first, last) if first < last else None


def parse_plot_range(value: dict | None, outer: TimeWindow) -> TimeWindow | None:
    """Преобразует Plotly relayout в проверенные UTC-границы; точки не используются."""
    if not value or value.get("reset"):
        return None
    try:
        dates = []
        for raw in (value["start"], value["end"]):
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            dates.append(parsed.replace(tzinfo=MOSCOW).astimezone(timezone.utc)
                         if parsed.tzinfo is None
                         else parsed.astimezone(timezone.utc))
        return TimeWindow(min(dates), max(dates)).clipped(outer)
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def bucket_seconds(window: TimeWindow) -> int:
    """Выбирает не более примерно 20 временных корзин без обрезки счётчика."""
    target = (window.end - window.start).total_seconds() / 20
    for seconds in (1, 5, 15, 30, 60, 300, 900, 3600, 21600, 86400, 604800):
        if seconds >= target:
            return seconds
    return max(604800, int(target) + 1)


def local_time(value: datetime) -> str:
    """Человекочитаемое время по Москве, включая секунды."""
    return value.astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M:%S")


def plot_time(value: datetime) -> str:
    """Передаёт Plotly московское настенное время без браузерной смены зоны."""
    return value.astimezone(MOSCOW).replace(tzinfo=None).isoformat()


class AnalyticsService:
    """Собирает ровно те запросы, которые нужны выбранному экрану."""

    def __init__(self, queries):
        self.queries = queries

    def context(self, tram: str, device: str, cabin: str,
                category: str | None, window: TimeWindow, page: int, sort: str, starts_only: bool = False) -> dict:
        """Возвращает ограниченные агрегаты, шкалу и страницу журнала."""
        seconds = bucket_seconds(window)
        q = self.queries
        return {
            "histogram": q.histogram(tram, device, cabin, window.start, window.end,
                                     seconds, category),
            "journal_count": q.journal_count(tram, device, cabin, window.start,
                                             window.end, category, starts_only),
            "journal": q.journal(tram, device, cabin, window.start, window.end,
                                 category, sort, page, starts_only=starts_only),
            "timeline_segments": q.timeline_segments(tram, device, cabin,
                                                     window.start, window.end),
            "timeline_states": q.timeline_states(
                tram, device, cabin, window.start, window.end),
            "bucket_seconds": seconds,
        }


@dataclass(frozen=True)
class ViewSelection:
    """Окно и правило журнала: пересечение либо начало в выбранной корзине."""
    window: TimeWindow
    starts_only: bool = False


class RangeHistory:
    """История диапазонов одной пользовательской сессии без общих глобальных данных."""

    def __init__(self, base: TimeWindow):
        self.base = base
        self.current = ViewSelection(base)
        self.previous: list[ViewSelection] = []

    def select(self, window: TimeWindow, starts_only: bool = False) -> None:
        """Добавляет только новое непустое окно, ограниченное исходным периодом."""
        clipped = window.clipped(self.base)
        if clipped is None:
            return
        target = ViewSelection(clipped, starts_only)
        if target != self.current:
            self.previous.append(self.current)
            self.current = target

    def back(self) -> None:
        """Возвращает предыдущий диапазон вместе с правилом отбора журнала."""
        if self.previous:
            self.current = self.previous.pop()

    def reset(self) -> None:
        """Возвращает весь период; действие также можно отменить кнопкой Назад."""
        self.select(self.base)
