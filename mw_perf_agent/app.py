"""Streamlit entrypoint. Run from this directory: `streamlit run app.py`."""
from __future__ import annotations

import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

import streamlit as st  # noqa: E402

from ui import driver, sidebar, tab_analyze, tab_builder, tab_diagnose, tab_runner  # noqa: E402

st.set_page_config(page_title="MW Perf Agent", page_icon="📊", layout="wide")
st.title("📊 미들웨어 성능 진단 · JMeter 부하 테스트 에이전트")
st.caption("LangGraph StateGraph 기반 · 규칙 엔진 + LLM 협업 · 모든 상태는 체크포인트(SQLite)에 영속화됩니다.")

graph = driver.get_graph()
sidebar.render_sidebar(graph)

run_id = st.session_state.get("run_id")
if run_id:
    phase = driver.phase_of(driver.snapshot(graph, run_id))
    st.info(f"현재 Run: `{run_id}` · 단계: **{phase}**", icon="🧭")

t1, t2, t3, t4 = st.tabs(["① 진단 (SC-001)", "② 스크립트 빌더 (SC-002A)", "③ 실행기 (SC-002C)", "④ 사후 분석 (SC-002B)"])
with t1:
    tab_diagnose.render(graph)
with t2:
    tab_builder.render(graph)
with t3:
    tab_runner.render(graph)
with t4:
    tab_analyze.render(graph)
