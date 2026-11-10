"""Существующее исследование с минимальным входом из матрицы парка."""
from datetime import timedelta, timezone
import streamlit as st
from dashboard.components.journal import show_journal
from dashboard.components.details import show_details
from dashboard.components.range_chart import show_range_chart
from dashboard.components.timeline import EXTRA_SIGNALS
from tram_pilot.database.queries import DashboardQueries
from tram_pilot.database.fleet_queries import FleetQueries
from tram_pilot.services.fleet_navigation import FleetNavigation
from tram_pilot.services.analytics import (AnalyticsService,CATEGORIES,MOSCOW,SIGNALS,
                                         TimeWindow,local_time,parse_plot_range,RangeHistory)

def _period_defaults(bounds):
    first = bounds[0].astimezone(MOSCOW).replace(tzinfo=None, second=0, microsecond=0)
    last = (bounds[1].astimezone(MOSCOW).replace(tzinfo=None, second=0,
            microsecond=0) + timedelta(minutes=1))
    return first, max(first + timedelta(minutes=1), last)


def render_investigation(connection, run: dict, fleet_navigation: FleetNavigation) -> None:
    """Сохраняет график исследования, принимая трамвай и исходный период из парка."""
    queries = DashboardQueries(connection)
    fleet_queries = FleetQueries(connection)
    entry = (run["id"],fleet_navigation.entry)
    if st.session_state.get("investigation_entry") != entry:
        st.session_state.investigation_tram = fleet_navigation.loco_number
        st.session_state.investigation_category = fleet_navigation.context.category or "all"
        st.session_state.period_scope = None
        st.session_state.investigation_entry = entry
        st.session_state.fleet_entry_pending = True
    trams = fleet_queries.vehicles(run["id"])
    if not trams:
        st.info("В опубликованном снимке нет Track")
        return
    selectors = st.columns([1.15, 1, 1.5, 1.35, 1.35, 1.25])
    with selectors[0], st.container(key="brand"):
        st.markdown("**TRAM PILOT**")
        st.caption(f"Исторические · МСК · расчёт №{run['id']}")
    with selectors[1]:
        tram = st.selectbox("Трамвай", trams, key="investigation_tram")
    devices = fleet_queries.devices(run["id"],tram)
    if not devices:
        st.info("Нет наблюдений для выбранного трамвая")
        return
    with selectors[2]:
        device, cabin = st.selectbox("Устройство / кабина", devices,
            format_func=lambda item: f"{item[0]} / {item[1]}")
    with selectors[5]:
        category_key = st.selectbox("Категория", ["all","subsystem_error","sensor_freshness"],
                                format_func=lambda item: "Все категории" if item == "all" else CATEGORIES[item], key="investigation_category")
        category = None if category_key == "all" else category_key
    bounds = queries.bounds(tram, device, cabin)
    if bounds is None:
        bounds = (fleet_navigation.context.window.start,fleet_navigation.context.window.end-timedelta(microseconds=1))
    scope = (run["id"], tram, device, cabin)
    if st.session_state.get("period_scope") != scope:
        if st.session_state.pop("fleet_entry_pending",False):
            start_default = fleet_navigation.context.window.start.astimezone(MOSCOW).replace(tzinfo=None)
            end_default = fleet_navigation.context.window.end.astimezone(MOSCOW).replace(tzinfo=None)
        else:
            start_default, end_default = _period_defaults(bounds)
        st.session_state["period_start"] = start_default
        st.session_state["period_end"] = end_default
        st.session_state["selected_episode"] = None
        st.session_state["journal_page"] = 1
        st.session_state["period_scope"] = scope
    with selectors[3]:
        local_start = st.datetime_input("Период: начало", key="period_start", step=60)
    with selectors[4]:
        local_end = st.datetime_input("Период: конец", key="period_end", step=60)
    if local_start is None or local_end is None:
        st.warning("Укажите начало и конец периода")
        return
    try:
        base = TimeWindow(local_start.replace(tzinfo=MOSCOW).astimezone(timezone.utc),
                          local_end.replace(tzinfo=MOSCOW).astimezone(timezone.utc))
    except ValueError:
        st.warning("Конец периода должен быть позже начала")
        return
    context = (scope,base,category)
    if st.session_state.get("navigation_context") != context:
        st.session_state.navigation = RangeHistory(base)
        st.session_state.navigation_context = context
        st.session_state.selected_episode = None
        st.session_state.journal_page = 1
    navigation = st.session_state.navigation
    controls = st.columns([.8,.65,.85,1.1,4.2])
    with controls[0]:
        st.button("К парку",on_click=fleet_navigation.back)
    with controls[1]:
        st.button("Назад",on_click=navigation.back,disabled=not navigation.previous)
    with controls[2]:
        st.button("Весь период",on_click=navigation.reset,
                  disabled=navigation.current.window==base and not navigation.current.starts_only)
    with controls[3], st.popover("Сигналы"):
        extras = st.multiselect("Дополнительные сигналы",EXTRA_SIGNALS,
            format_func=lambda key:SIGNALS[key],placeholder="Выберите сигналы")
    window = navigation.current.window
    starts_only = navigation.current.starts_only
    with controls[4]:
        st.caption(f"{local_time(window.start)} — {local_time(window.end)} · "
                   + ("Начавшиеся в корзине" if starts_only else "Пересекающие интервал"))
    journal_context = (context,navigation.current)
    if st.session_state.get("journal_context") != journal_context:
        st.session_state.journal_page = 1
        st.session_state.selected_episode = None
        st.session_state.journal_context = journal_context
    left,right = st.columns([.65,.35],gap="small")
    with right:
        journal_panel = st.container(border=True,height=440,key="journal")
    with journal_panel:
        st.markdown("**Журнал проблем**")
        sorting,paging = st.columns([3,1])
        with sorting:
            sort_label = st.selectbox("Сортировка",
                ["Сначала новые","Сначала старые","По последнему наблюдению"],
                label_visibility="collapsed")
        with paging:
            page = st.number_input("Страница",min_value=1,step=1,
                key="journal_page",label_visibility="collapsed")
    sort = {"Сначала новые":"newest","Сначала старые":"oldest",
            "По последнему наблюдению":"last"}[sort_label]
    data = AnalyticsService(queries).context(tram,device,cabin,category,
        window,int(page),sort,starts_only)
    with left, st.container(border=True,height=440,key="history"):
        st.markdown("**История неисправностей**")
        event = show_range_chart(data,window,extras)
        if event is not None:
            target = parse_plot_range(event,base)
            if target is not None:
                before = navigation.current
                navigation.select(target,event.get("kind")=="bucket")
                if navigation.current != before:
                    st.session_state.selected_episode = None
                    st.rerun()
    with journal_panel:
        new_count = sum(row["episodes"] for row in data["histogram"])
        st.caption(f"Новых: {new_count} · В журнале: {data['journal_count']}")
        selected_id = show_journal(data["journal"],
            f"journal_{context}_{navigation.current}_{sort}_{page}")
        if selected_id is not None:
            st.session_state.selected_episode = selected_id
        if st.session_state.selected_episode not in {r["id"] for r in data["journal"]}:
            st.session_state.selected_episode = None
    with st.container(border=True,height=260,key="details"):
        episode_id = st.session_state.selected_episode
        episode = queries.episode(episode_id) if episode_id is not None else None
        segments = queries.episode_segments(episode_id) if episode else []
        show_details(episode,segments,queries)
    if len(data["timeline_states"])>5000:
        st.warning("Шкала ограничена 5000 сериями. Уменьшите период.")
