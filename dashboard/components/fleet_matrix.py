"""Изолированная матрица: обычная HTML-кнопка на всю строку трамвая."""
import streamlit as st
from tram_pilot.services.analytics import MOSCOW, local_time
from tram_pilot.services.fleet import FleetCell

_JS = """
export default function({parentElement, data, setTriggerValue}) {
  const root = parentElement.querySelector('#fleet-root');
  const scroll = document.createElement('div');
  scroll.className = 'matrix-scroll';
  scroll.setAttribute('aria-label', 'Матрица парка');
  const header = document.createElement('div');
  header.className = 'matrix-header';
  const columns = '130px repeat(' + data.columns.length + ', minmax(0, 1fr)) 12px';
  header.style.gridTemplateColumns = columns;
  const label = document.createElement('span');
  label.textContent = 'Трамвай';
  header.append(label);
  for (const column of data.columns) {
    const span = document.createElement('span');
    span.textContent = column.label;
    span.title = column.title;
    header.append(span);
  }
  scroll.append(header);
  const tip = document.createElement('div');
  tip.className = 'cell-tooltip';
  tip.setAttribute('role', 'tooltip');
  tip.hidden = true;
  for (const row of data.rows) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'tram-row';
    button.style.gridTemplateColumns = columns;
    button.setAttribute('aria-label', 'Трамвай ' + row.loco_number + '. Открыть исследование');
    const name = document.createElement('span');
    name.className = 'tram-name';
    name.textContent = row.loco_number;
    button.append(name);
    for (const cell of row.cells) {
      const box = document.createElement('span');
      box.className = 'matrix-cell ' + cell.kind;
      box.setAttribute('aria-label', cell.tooltip);
      for (const part of cell.parts) {
        const band = document.createElement('span');
        band.style.height = part.percent + '%';
        band.style.backgroundColor = part.color;
        box.append(band);
      }
      box.addEventListener('pointerenter', () => {
        tip.textContent = cell.tooltip;
        tip.hidden = false;
        const rect = box.getBoundingClientRect();
        tip.style.left = Math.max(8, Math.min(rect.left, innerWidth-tip.offsetWidth-8)) + 'px';
        tip.style.top = Math.max(8, Math.min(rect.bottom+8, innerHeight-tip.offsetHeight-8)) + 'px';
      });
      box.addEventListener('pointerleave', () => {tip.hidden = true;});
      button.append(box);
    }
    const space = document.createElement('span');
    space.className = 'row-space';
    button.append(space);
    button.addEventListener('click', () => {
      tip.hidden = true;
      setTriggerValue('tram', {loco_number: row.loco_number, analysis_run_id: data.analysis_run_id});
    });
    scroll.append(button);
  }
  root.replaceChildren(scroll, tip);
  return () => {root.replaceChildren();};
}
"""
_CSS = """
#fleet-root {height:calc(100dvh - 215px); min-height:300px; font:15px Arial,sans-serif; color:#171717; background:#FFFFFF;}
* {box-sizing:border-box;}
.matrix-scroll {height:100%; overflow-y:auto; overflow-x:hidden; border:1px solid #D1D5DB;}
.matrix-header,.tram-row {display:grid; gap:6px; align-items:center; padding:4px 8px; width:100%;}
.matrix-header {position:sticky; top:0; z-index:1; min-height:48px; background:#FFFFFF; border-bottom:1px solid #D1D5DB; font-size:14px; white-space:pre-line; text-align:center;}
.matrix-header span:first-child {text-align:left;}
.tram-row {min-height:42px; border:0; border-bottom:1px solid #E5E7EB; border-radius:0; background:#FFFFFF; color:#171717; font:inherit; text-align:left; cursor:pointer;}
.tram-row:hover {background:#F0F0F0;}
.tram-row:focus-visible {outline:2px solid #171717; outline-offset:-2px;}
.tram-name {font-size:16px; overflow-wrap:anywhere; font-variant-numeric:tabular-nums;}
.matrix-cell {display:flex; flex-direction:column; height:32px; border:1px solid #D1D5DB; min-width:0;}
.matrix-cell>span {display:block; width:100%; flex-shrink:0;}
.row-space {height:32px;}
.cell-tooltip {position:fixed; z-index:10; white-space:pre-line; max-width:390px; border:1px solid #171717; background:#FFFFFF; color:#171717; padding:10px; font-size:14px; line-height:1.45; pointer-events:none;}
"""
_component = st.components.v2.component("tram_pilot_fleet_matrix",
    html='<div id="fleet-root"></div>',css=_CSS,js=_JS)


def matrix_data(cells: list[FleetCell], run_id: int) -> dict:
    """Передаёт структурированные значения; текст БД не становится HTML-разметкой."""
    columns = sorted({cell.window for cell in cells},key=lambda window:window.start)
    rows: dict[str,list] = {}
    for cell in sorted(cells,key=lambda cell:(cell.loco_number,cell.window.start)):
        rows.setdefault(cell.loco_number,[]).append({
            "kind":cell.kind,"tooltip":cell.tooltip(),
            "parts":[{"color":color,"percent":percent} for color,percent in cell.fill_parts],
        })
    return {"analysis_run_id":run_id,
        "columns":[{"label":w.start.astimezone(MOSCOW).strftime("%H:%M\n%d.%m"),
                    "title":f"{local_time(w.start)} — {local_time(w.end)} (МСК)"} for w in columns],
        "rows":[{"loco_number":tram,"cells":values} for tram,values in rows.items()]}


def show_fleet_matrix(cells: list[FleetCell], run_id: int):
    """Возвращает событие обычного click, включая Enter и Space на кнопке строки."""
    result = _component(key="fleet_matrix",height="stretch",width="stretch",
        data=matrix_data(cells,run_id),on_tram_change=lambda:None)
    return result.tram
