"""Стартовый экран парка и проверенные переходы к исследованию."""
from dataclasses import replace
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout
import streamlit as st

from dashboard.styles import apply_styles
from dashboard.screens.fleet import render_fleet
from dashboard.screens.investigation import render_investigation
from tram_pilot.database.connection import DatabaseConfig,load_env_file
from tram_pilot.database.queries import DashboardQueries
from tram_pilot.database.fleet_queries import FleetQueries
from tram_pilot.services.fleet import FleetService
from tram_pilot.services.fleet_navigation import FleetContext,FleetNavigation

st.set_page_config(page_title="Tram Pilot Analytics",layout="wide",
                   initial_sidebar_state="collapsed")
apply_styles()


@st.cache_resource
def connection_pool() -> ConnectionPool:
    """Повторно использует ограниченное число подключений между сеансами Streamlit."""
    config = DatabaseConfig.from_env()
    pool = ConnectionPool(conninfo="", min_size=1, max_size=5, open=True,
        kwargs={"host": config.host, "port": config.port, "dbname": config.dbname,
                "user": config.user, "password": config.password,
                "row_factory": dict_row, "options": "-c timezone=UTC"})
    pool.wait(timeout=5)
    return pool



def main() -> None:
    """Использует опубликованный снимок и отдельное состояние каждого пользователя."""
    load_env_file()
    try:
        with connection_pool().connection() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            run = DashboardQueries(connection).published_run()
            if run is None:
                st.info("Нет опубликованного завершённого анализа")
                return
            queries = FleetQueries(connection)
            if "fleet_navigation" not in st.session_state:
                window = FleetService(queries).default_window(run["id"])
                if window is None:
                    st.info("В опубликованном снимке нет наблюдений Track")
                    return
                st.session_state.fleet_navigation = FleetNavigation(FleetContext(run["id"],window))
            navigation = st.session_state.fleet_navigation
            if navigation.context.analysis_run_id != run["id"]:
                navigation.context = replace(navigation.context,analysis_run_id=run["id"])
                navigation.back()
                st.info("Опубликована новая версия анализа. Матрица обновлена.")
            if navigation.page == "investigation":
                if navigation.loco_number not in queries.vehicles(run["id"]):
                    navigation.back()
                    st.rerun()
                render_investigation(connection,run,navigation)
                return
            event,shown_trams = render_fleet(queries,run,navigation)
            if event is not None:
                # Новый снимок транзакции проверяет публикацию после нажатия,
                # а не доверяет версии из браузера или старому REPEATABLE READ.
                connection.commit()
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                latest = DashboardQueries(connection).published_run()
                current_id = latest["id"] if latest else None
                available = set(queries.vehicles(current_id)) if current_id else set()
                if navigation.open_selection(event,current_id,available & shown_trams):
                    st.rerun()
                else:
                    st.warning("Выбор устарел или трамвай недоступен. Обновите матрицу.")
    except (psycopg.Error,PoolTimeout,OSError,ValueError):
        st.error("База данных недоступна или схема не подготовлена")


if __name__ == "__main__":
    main()
