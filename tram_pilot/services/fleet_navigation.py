"""Контекст и проверка переходов парка без зависимости от Streamlit."""
from dataclasses import dataclass

from tram_pilot.services.analytics import TimeWindow


@dataclass(frozen=True)
class FleetContext:
    """Сохраняемый контекст матрицы одной пользовательской сессии."""
    analysis_run_id: int
    window: TimeWindow
    category: str | None = None
    search: str = ""


class FleetNavigation:
    """Маршрут и контекст пользователя; экземпляр хранится только в session_state."""

    def __init__(self, context: FleetContext) -> None:
        self.context = context
        self.page = "fleet"
        self.loco_number: str | None = None
        self.entry = 0

    def open_selection(self, event: object, published_run_id: int | None,
                       available: set[str]) -> bool:
        """Проверяет недоверенное событие по свежей публикации и серверному списку."""
        if not isinstance(event, dict):
            return False
        run_id = event.get("analysis_run_id")
        tram = event.get("loco_number")
        if (type(run_id) is not int or run_id != published_run_id or
                run_id != self.context.analysis_run_id or
                not isinstance(tram,str) or tram not in available):
            return False
        self.loco_number = tram
        self.entry += 1
        self.page = "investigation"
        return True

    def back(self) -> None:
        """Возвращает матрицу, сохраняя исходные фильтры и опубликованную версию."""
        self.page = "fleet"
