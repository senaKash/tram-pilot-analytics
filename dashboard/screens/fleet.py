"""Стартовый экран матрицы диагностических наблюдений парка."""
from datetime import timezone
import streamlit as st

from dashboard.components.fleet_matrix import show_fleet_matrix
from tram_pilot.services.analytics import CATEGORIES,MOSCOW,TimeWindow
from tram_pilot.services.fleet import FleetService
from tram_pilot.services.fleet_navigation import FleetContext,FleetNavigation


def render_fleet(queries, run: dict, navigation: FleetNavigation) -> tuple[object,set[str]]:
    """Отображает матрицу и возвращает недоверенное событие вместе с доступными строками."""
    context = navigation.context
    # Widget-ключи Streamlit удаляет, когда экран скрыт. Постоянный контекст
    # хранится отдельно, поэтому возврат восстанавливает именно фильтры парка.
    defaults = {
        "fleet_start":context.window.start.astimezone(MOSCOW).replace(tzinfo=None),
        "fleet_end":context.window.end.astimezone(MOSCOW).replace(tzinfo=None),
        "fleet_category":context.category or "all","fleet_search":context.search,
    }
    for key,value in defaults.items():
        st.session_state.setdefault(key,value)
    brand,start,end,category,search = st.columns([1.2,1.5,1.5,1.5,1.3])
    with brand,st.container(key="brand"):
        st.markdown("**TRAM PILOT / ПАРК**")
    with start:
        first = st.datetime_input("Период: начало",key="fleet_start",step=60)
    with end:
        last = st.datetime_input("Период: конец",key="fleet_end",step=60)
    with category:
        category_key = st.selectbox("Категория",["all","subsystem_error","sensor_freshness"],key="fleet_category",
            format_func=lambda key:"Все проблемы" if key == "all" else CATEGORIES[key])
        selected_category = None if category_key == "all" else category_key
    with search:
        text = st.text_input("Поиск трамвая",key="fleet_search")
    st.caption(f"Исторические данные · Europe/Moscow · Опубликованный расчёт №{run['id']}")
    try:
        if first is None or last is None:
            raise ValueError("Укажите начало и конец периода")
        window = TimeWindow(first.replace(tzinfo=MOSCOW).astimezone(timezone.utc),
                            last.replace(tzinfo=MOSCOW).astimezone(timezone.utc))
    except ValueError:
        st.warning("Укажите период: конец должен быть позже начала")
        return None,set()
    navigation.context = FleetContext(run["id"],window,selected_category,text)
    cells = FleetService(queries).matrix(run["id"],window,selected_category,text)
    if not cells:
        st.info("Нет трамваев по заданному поиску")
        return None,set()
    trams = {cell.loco_number for cell in cells}
    with st.container(key="fleet_title"):
        st.markdown("**Состав диагностических проблем**")
    with st.container(height=600,border=False,key="fleet_panel"):
        event = show_fleet_matrix(cells,run["id"])
    st.caption("Красный — ошибки подсистем · Синий — неактуальные данные · "
               "Серый — нет Track · Белый — Track есть, новых эпизодов нет. "
               "Заполнение показывает состав новых эпизодов, а не время простоя.")
    return event,trams
