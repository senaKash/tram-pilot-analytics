"""Единая история с координатным выделением и кликом по корзине Plotly."""
import streamlit as st
from plotly.offline import get_plotlyjs
from dashboard.components.timeline import MAIN_SIGNALS, history_traces
from tram_pilot.services.analytics import SIGNALS, TimeWindow, plot_time

_JAVASCRIPT = get_plotlyjs() + """
export default function({parentElement, data, setTriggerValue}) {
  const plot = parentElement.querySelector("#history-plot");
  let active = true;
  const observer = new ResizeObserver(() => {
    if (active && plot.isConnected) Plotly.Plots.resize(plot);
  });
  Plotly.newPlot(plot, data.traces, data.layout, {
    displayModeBar: false, scrollZoom: false, responsive: true,
    doubleClick: false, showTips: false
  }).then(() => {
    if (!active) return;
    observer.observe(plot);
    plot.on("plotly_relayout", event => {
      const start = event["xaxis.range[0]"], end = event["xaxis.range[1]"];
      if (active && start && end)
        setTriggerValue("selection", {start, end, kind: "range"});
    });
    plot.on("plotly_click", event => {
      const point = event.points[0];
      if (active && point.data.type === "bar" && point.customdata)
        setTriggerValue("selection", {
          start: point.customdata[0], end: point.customdata[1], kind: "bucket"
        });
    });
  });
  return () => { active = false; observer.disconnect(); Plotly.purge(plot); };
}
"""
_component = st.components.v2.component(
    "tram_pilot_history", html='<div id="history-plot"></div>',
    # Общие стили Plotly не пересекают границу Shadow DOM.
    css="""#history-plot {width:100%; height:370px; overflow:hidden;}
    .plotly .main-svg {position:absolute; top:0; left:0; pointer-events:none;}
    .plotly .main-svg .draglayer {pointer-events:all;}
    .plotly .drag {cursor:crosshair;}""", js=_JAVASCRIPT,
)


def show_range_chart(data: dict, window: TimeWindow, extra: list[str]):
    """Возвращает реальное событие диапазона либо корзины с московскими границами."""
    labels = [SIGNALS[key] for key in list(MAIN_SIGNALS)+list(extra)]
    tick_format = "%H:%M:%S" if (window.end-window.start).total_seconds()<300 else "%H:%M<br>%d.%m"
    layout = dict(height=370, dragmode="zoom", barmode="stack",
        paper_bgcolor="#FFFFFF", plot_bgcolor="#FFFFFF",
        margin=dict(l=185,r=12,t=28,b=42),
        font=dict(family="Arial, sans-serif",size=13,color="#171717"),
        legend=dict(orientation="h",y=1.12,x=0,font=dict(size=12)),
        xaxis=dict(type="date",range=[plot_time(window.start),plot_time(window.end)],
            tickformat=tick_format,gridcolor="#D1D5DB",anchor="y2"),
        yaxis=dict(domain=[.68,1],title=dict(text="Новые эпизоды"),
            rangemode="tozero",dtick=1,gridcolor="#D1D5DB",fixedrange=True),
        yaxis2=dict(domain=[0,.49],type="category",categoryorder="array",
            categoryarray=labels[::-1],gridcolor="#EEEEEE",fixedrange=True))
    result = _component(key="history_chart",height=370,width="stretch",
        data=dict(traces=history_traces(data,window,extra),layout=layout),
        on_selection_change=lambda:None)
    return result.selection
