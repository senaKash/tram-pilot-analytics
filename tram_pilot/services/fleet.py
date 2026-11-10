"""Значения ячеек парка без зависимости от Streamlit и способа отрисовки."""
from dataclasses import dataclass
from datetime import timedelta
from math import ceil

from tram_pilot.services.analytics import TimeWindow, local_time


def fleet_bucket_seconds(window: TimeWindow) -> int:
    """Выбирает около 18 общих корзин; минимальный шаг — минута."""
    duration = (window.end-window.start).total_seconds()
    steps = (60,120,300,600,900,1800,3600,10800,21600,43200,86400,604800)
    candidates = [seconds for seconds in steps if ceil(duration/seconds) <= 24]
    if candidates:
        return min(candidates,key=lambda seconds:abs(ceil(duration/seconds)-18))
    return ceil(duration/18/86400)*86400


@dataclass(frozen=True)
class FleetCell:
    """Состав новых эпизодов и число снимков одной временной ячейки."""
    loco_number: str
    window: TimeWindow
    error_count: int
    freshness_count: int
    track_count: int

    def __post_init__(self) -> None:
        if min(self.error_count,self.freshness_count,self.track_count) < 0:
            raise ValueError("Счётчики не могут быть отрицательными")
        if self.total and not self.track_count:
            raise ValueError("Эпизоды без исходных Track: проверьте согласованность снимка")

    @property
    def total(self) -> int:
        """Число новых диагностических сигналов, не физических поломок."""
        return self.error_count+self.freshness_count

    @property
    def red_percent(self) -> float | None:
        """Доля Error; для нуля эпизодов процент не определяется."""
        return 100*self.error_count/self.total if self.total else None

    @property
    def blue_percent(self) -> float | None:
        """Дополнение красной доли до 100%, без накопления ошибки округления."""
        return 100-self.red_percent if self.total else None

    @property
    def kind(self) -> str:
        """Отличает отсутствие снимков от отсутствия новых эпизодов."""
        return "composition" if self.total else "quiet" if self.track_count else "missing"

    @property
    def fill_parts(self) -> tuple[tuple[str, float], ...]:
        """Цветные части сверху вниз: синий над красным, без пустого промежутка."""
        if not self.total:
            return (("#FFFFFF" if self.track_count else "#D1D5DB",100.0),)
        parts = []
        if self.freshness_count:
            parts.append(("#2563EB",self.blue_percent))
        if self.error_count:
            parts.append(("#D12D38",self.red_percent))
        return tuple(parts)

    def tooltip(self) -> str:
        """Формирует точную подпись с датами и московским временем."""
        lines = [f"Трамвай {self.loco_number}",
                 f"{local_time(self.window.start)} — {local_time(self.window.end)} (МСК)"]
        if not self.track_count:
            lines.append("Нет наблюдений Track")
        elif not self.total:
            lines.extend(["Новых диагностических эпизодов: 0",
                          f"Наблюдений Track: {self.track_count}"])
        else:
            lines.extend([
                f"Ошибки подсистем: {self.error_count} ({self.red_percent:g}%)",
                f"Неактуальность данных: {self.freshness_count} ({self.blue_percent:g}%)",
                f"Всего новых эпизодов: {self.total}",
                f"Наблюдений Track: {self.track_count}",
            ])
        return "\n".join(lines)


class FleetService:
    """Подготавливает ограниченную матрицу из агрегатов PostgreSQL."""

    def __init__(self, queries) -> None:
        self.queries = queries

    def default_window(self, run_id: int) -> TimeWindow | None:
        """Округляет фактический охват наружу до минут, не теряя последний Track."""
        coverage = self.queries.coverage(run_id)
        if coverage is None:
            return None
        first = coverage["first_at"].replace(second=0,microsecond=0)
        last = coverage["last_at"].replace(second=0,microsecond=0)+timedelta(minutes=1)
        return TimeWindow(first,last)

    def matrix(self, run_id: int, window: TimeWindow,
               category: str | None = None, search: str = "") -> list[FleetCell]:
        """Читает счётчики, а не исходные Track; ничего не хранит между пользователями."""
        rows = self.queries.matrix(run_id,window.start,window.end,
                                   fleet_bucket_seconds(window),category,search)
        return [FleetCell(row["loco_number"],TimeWindow(row["bucket_start"],row["bucket_end"]),
                          row["error_count"],row["freshness_count"],row["track_count"])
                for row in rows]
