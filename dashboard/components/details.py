"""Полноширинная панель эпизода: сводка, сегменты и ограниченные исходные записи."""
import json
from html import escape
import pandas as pd
import streamlit as st
from tram_pilot.services.analytics import CATEGORIES, SIGNALS, CLOSE_REASONS, local_time


def show_details(episode: dict | None, segments: list[dict], queries) -> None:
    """Читает не более 500 исходных наблюдений выбранного сегмента."""
    if episode is None:
        st.write("Выберите запись в журнале для просмотра подробностей")
        return
    general, composition, source = st.columns([.30,.34,.36],gap="medium")
    with general:
        lines = [
            f"{SIGNALS[episode['signal']]} · {CATEGORIES[episode['category']]}",
            f"Трамвай: {episode['loco_number']}",
            f"Устройство: {episode['device_id']} / {episode['cabin_type']}",
            f"Первое: {local_time(episode['first_bad_at'])}",
            f"Последнее: {local_time(episode['last_bad_at'])}",
            f"Снимков: {episode['bad_count']} · Сегментов: {episode['segment_count']}",
            f"Завершение: {CLOSE_REASONS.get(episode['close_reason'],episode['close_reason'])}",
            f"Ненаблюдаемые промежутки: {episode['bridged_gap_seconds']:.1f} с",
        ]
        # Одна сводка избегает межвиджетных отступов, не уменьшая размер текста.
        st.markdown('<div style="font-size:15px;line-height:21px">'
            '<strong>Подробности эпизода</strong>'
            + ''.join('<div>'+escape(line)+'</div>' for line in lines)+'</div>',
            unsafe_allow_html=True)
    short_reasons = {"observed_clearance":"Признак снят","telemetry_gap":"Нет снимков",
                     "unknown_state":"Неизвестно","sample_end":"Конец выборки"}
    with composition:
        st.markdown("**Диагностические сегменты**")
        previous = None
        rows = []
        for segment in segments:
            gap = (segment["first_bad_at"]-previous).total_seconds() if previous else 0
            rows.append({"Начало":local_time(segment["first_bad_at"])[11:],
                "Последнее":local_time(segment["last_bad_at"])[11:],
                "Снимков":segment["bad_count"],"Пропуск, с":gap,
                "Завершение":short_reasons.get(segment["close_reason"],segment["close_reason"])})
            previous = segment["last_bad_at"]
        with st.container(height=380,key="segments_area",border=False):
            st.dataframe(pd.DataFrame(rows),hide_index=True,height="stretch",width="stretch",row_height=36,key="episode_segments_table",
                column_config={"Начало":st.column_config.TextColumn(width=75),
                    "Последнее":st.column_config.TextColumn(width=75),
                    "Снимков":st.column_config.NumberColumn(width=60),
                    "Пропуск, с":st.column_config.NumberColumn(width=75),
                    "Завершение":st.column_config.TextColumn(width=140)})

    with source:
        st.markdown("**Исходные наблюдения Track**")
        chosen = st.selectbox("Сегмент",options=range(len(segments)),
            format_func=lambda i:f"Сегмент {segments[i]['ordinal']}",key=f"source_{episode['id']}",
            label_visibility="collapsed")
        records = queries.segment_sources(segments[chosen]["id"]) if chosen is not None else []
        rows = [{"Track ID":r["track_record_id"],"Время":local_time(r["observed_at"])[11:],
            "Поле":episode["signal"],
            "Значение":json.dumps(r["raw_value"],ensure_ascii=False) if r["value_present"] else "отсутствует",
            "Файл":r["source_file"],"Контекст":json.dumps(r["context"],ensure_ascii=False)}
            for r in records]
        with st.container(height=340,key="source_area",border=False):
            st.dataframe(pd.DataFrame(rows),hide_index=True,height="stretch",width="stretch",row_height=36,key="source_records_table")

        if len(records)==500:
            st.caption("Показаны первые 500 наблюдений")
