"""Подготовка согласованных частей истории без расчёта диагностических эпизодов."""
from collections import defaultdict
from datetime import timedelta
from tram_pilot.services.analytics import SIGNALS, TimeWindow, plot_time

MAIN_SIGNALS = ("m_danger_obj_status", "m_trafficlight_status",
                "m_speed_limit_status", "ubloxGps", "leftImage", "dbwFbTram")
EXTRA_SIGNALS = ("multiImages", "minsEth", "roadModel", "tramPlannerService")
STATES = {
    "clear": ("Признак снят", "#525252", "solid", 2),
    "off": ("Отключено", "#525252", "dashdot", 3),
    "unknown": ("Неизвестно", "#A3A3A3", "dot", 3),
    "conflict": ("Противоречие снимков", "#171717", "dash", 3),
    "gap": ("Нет наблюдений", "#A3A3A3", "dash", 1),
}


def history_traces(data: dict, window: TimeWindow, extra: list[str]) -> list[dict]:
    """Строит корзины и наблюдаемые границы; не заполняет разрывы исправностью."""
    traces = []
    seconds = data["bucket_seconds"]
    for category, name, color in (
        ("subsystem_error", "Ошибки подсистем", "#D12D38"),
        ("sensor_freshness", "Актуальность данных", "#2563EB"),
    ):
        rows = [row for row in data["histogram"] if row["category"] == category]
        if not rows:
            continue
        centers, widths, ranges = [], [], []
        for row in rows:
            first = max(window.start, row["bucket"])
            last = min(window.end, row["bucket"] + timedelta(seconds=seconds))
            centers.append(plot_time(first + (last - first) / 2))
            widths.append((last-first).total_seconds()*1000*.85)
            ranges.append([plot_time(first), plot_time(last)])
        traces.append(dict(type="bar", name=name, x=centers,
            y=[r["episodes"] for r in rows], width=widths, customdata=ranges,
            marker=dict(color=color),
            hovertemplate="%{customdata[0]} — %{customdata[1]}<br>Новых: %{y}<extra>"+name+"</extra>"))
    signals = list(MAIN_SIGNALS) + [key for key in extra if key in EXTRA_SIGNALS]
    grouped = defaultdict(lambda: dict(x=[], y=[], text=[]))

    def add(state, signal, first, last, text):
        if signal not in signals:
            return
        values = grouped[state]
        values["x"].extend([plot_time(max(first, window.start)),
                            plot_time(min(last, window.end)), None])
        values["y"].extend([SIGNALS[signal], SIGNALS[signal], None])
        values["text"].extend([text, text, None])

    by_signal = defaultdict(list)
    for row in data["timeline_states"][:5000]:
        by_signal[row["signal"]].append(row)
        if row["state"] != "bad":
            add(row["state"], row["signal"], row["first_at"], row["last_at"],
                f"{row['detail']} · {row['snapshots']} снимков<br>"
                f"{plot_time(row['first_at'])} — {plot_time(row['last_at'])}")
    # Пунктир показывает отсутствие снимков, а не непрерывную ошибку эпизода.
    for signal in (signals if len(data["timeline_states"]) <= 5000 else []):
        previous = window.start
        for row in by_signal[signal]:
            if (row["first_at"] - previous).total_seconds() > 30:
                add("gap", signal, previous, row["first_at"], "Нет наблюдений")
            previous = max(previous, row["last_at"])
        if (window.end - previous).total_seconds() > 30:
            add("gap", signal, previous, window.end, "Нет наблюдений")
    for row in data["timeline_segments"]:
        state = "bad_error" if row["category"] == "subsystem_error" else "bad_sensor"
        add(state, row["signal"], row["first_bad_at"], row["last_bad_at"],
            "Проблемный сегмент<br>"+plot_time(row["first_bad_at"])+" — "+plot_time(row["last_bad_at"]))
    styles = {**STATES, "bad_error": ("Проблема подсистемы", "#D12D38", "solid", 6),
              "bad_sensor": ("Неактуальные данные", "#2563EB", "solid", 6)}
    for state, values in grouped.items():
        label, color, dash, width = styles[state]
        traces.append(dict(type="scatter", mode="lines+markers", yaxis="y2",
            x=values["x"], y=values["y"], text=values["text"], name=label,
            showlegend=not state.startswith("bad"),
            line=dict(color=color, width=width, dash=dash),
            marker=dict(color=color, size=7, symbol="line-ns"),
            hovertemplate=label+" · %{y}<br>%{text}<extra></extra>"))
    return traces
