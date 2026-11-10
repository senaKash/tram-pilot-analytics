"""Журнал с выбором записи одним нажатием на любую ячейку строки."""
import pandas as pd
import streamlit as st
from tram_pilot.services.analytics import SIGNALS, MOSCOW

PROBLEMS = {"subsystem_error": "Ошибка", "sensor_freshness": "Данные"}


def show_journal(rows: list[dict], key: str) -> int | None:
    """Выбирает эпизод через штатный single-cell, без столбца чекбоксов."""
    if not rows:
        st.write("Нет эпизодов")
        return None
    frame = pd.DataFrame([{
        "Время": row["first_bad_at"].astimezone(MOSCOW).strftime("%H:%M:%S"),
        "Признак": SIGNALS.get(row["signal"],row["signal"]),
        "Тип": PROBLEMS[row["category"]],
    } for row in rows])
    def choose():
        cells = st.session_state[key].selection.cells
        st.session_state.selected_episode = rows[cells[0][0]]["id"] if cells else None

    selected_id = st.session_state.get("selected_episode")
    styled = frame.style.apply(lambda row: [
        "background-color: #EDEDED; color: #171717"
        if rows[row.name]["id"] == selected_id else "color: #171717"
        for _ in row],axis=1)
    selected = st.dataframe(styled,hide_index=True,height=286,width="stretch",
        row_height=36,column_config={
            "Время":st.column_config.TextColumn(width="small"),
            "Признак":st.column_config.TextColumn(width="medium"),
            "Тип":st.column_config.TextColumn(width="small")},
        key=key,on_select=choose,selection_mode="single-cell")
    cells = selected.selection.cells
    return rows[cells[0][0]]["id"] if cells else None
