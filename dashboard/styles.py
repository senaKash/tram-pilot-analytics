"""Общие точечные стили MONO для существующих экранов."""
import streamlit as st


def apply_styles() -> None:
    """Применяет оформление без изменения логики компонентов."""
    st.markdown("""<style>
    [data-testid="stHeader"] {display:none;}
    .block-container {padding:.55rem 1rem; max-width:none;}
    [data-testid="stVerticalBlock"] {gap:.35rem;}
    .st-key-history, .st-key-journal, .st-key-details {background:#FFFFFF; border-color:#D1D5DB;}
    [data-testid="stLayoutWrapper"]:has(> .st-key-details) {height:calc(100dvh - 595px) !important; flex:0 0 calc(100dvh - 595px) !important;}
    .st-key-details {min-height:235px;}
    [data-testid="stLayoutWrapper"]:has(> .st-key-segments_area) {height:clamp(145px, calc(100dvh - 700px), 380px) !important; flex:0 0 clamp(145px, calc(100dvh - 700px), 380px) !important;}
    [data-testid="stLayoutWrapper"]:has(> .st-key-source_area) {height:clamp(120px, calc(100dvh - 740px), 340px) !important; flex:0 0 clamp(120px, calc(100dvh - 740px), 340px) !important;}
    button, input, [data-baseweb="select"] > div {border-radius:0 !important; box-shadow:none !important;}
    p {font-size:15px; font-variant-numeric:tabular-nums;}
    [data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] *, small {color:#525252 !important; font-size:13px; opacity:1;}
    [data-testid="stWidgetLabel"] p {font-size:14px; color:#171717;}
    [data-testid="stElementContainer"] strong {font-size:16px;}
    .st-key-brand strong {font-size:18px;}
    [data-testid="stLayoutWrapper"]:has(> .st-key-fleet_panel) {height:calc(100dvh - 215px) !important; flex:0 0 calc(100dvh - 215px) !important;}
    .st-key-fleet_panel {min-height:300px;}
    .st-key-fleet_title strong {font-size:18px;}
    </style>""", unsafe_allow_html=True)
