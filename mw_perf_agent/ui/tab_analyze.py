from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from config.settings import get_settings
from graph.chat_agent import build_chat_agent, chat_config
from graph.nodes.analyze import render_report_md
from llm.models import llm_available
from . import driver


def _charts(bucket_path: str | None, gc_path: str | None, anomalies) -> None:
    if not bucket_path or not Path(bucket_path).exists():
        return
    b = pd.read_csv(bucket_path)
    if b.empty:
        return
    b["t"] = pd.to_datetime(b["bucket_start_ms"], unit="ms")
    b = b.set_index("t")
    c1, c2 = st.columns(2)
    c1.caption("TPS / Active threads")
    c1.line_chart(b[["tps", "active"]])
    c2.caption("p99 / avg (ms)")
    c2.line_chart(b[["p99_ms", "avg_ms"]])
    c3, c4 = st.columns(2)
    c3.caption("Error rate (%) / HTTP 503 count")
    c3.line_chart(b[["err_rate", "http_503"]])
    if gc_path and Path(gc_path).exists():
        g = pd.read_csv(gc_path)
        if not g.empty:
            g["t"] = pd.to_datetime(g["ts_ms"], unit="ms")
            c4.caption("GC: Full GC pause (ms) / Old gen %")
            c4.line_chart(g.set_index("t")[[c for c in ("pause_ms", "old_pct") if c in g.columns]])
    steps = b.groupby("step")["bucket_start_ms"].agg(["min", "max"]).reset_index()
    st.caption("Step 경계: " + " | ".join(f"{r['step']}: {pd.to_datetime(r['min'], unit='ms').strftime('%H:%M:%S')}~{pd.to_datetime(r['max'], unit='ms').strftime('%H:%M:%S')}" for _, r in steps.iterrows()))


def _chat(graph, run_id: str) -> None:
    st.subheader("💬 후속 질의 (chat_agent 서브그래프)")
    if not get_settings().llm_enabled or not llm_available():
        st.info("LLM이 설정되면 리포트 기반 후속 질문을 할 수 있습니다 (읽기 전용 도구로 runs/<run_id>/ 데이터를 조회).")
        return
    key = f"chat_agent_{run_id}"
    if key not in st.session_state:
        st.session_state[key] = build_chat_agent(run_id, checkpointer=graph.checkpointer)
    agent = st.session_state[key]
    if agent is None:
        st.warning("채팅 에이전트를 초기화하지 못했습니다.")
        return
    cfg = chat_config(run_id)
    hist = agent.get_state(cfg).values.get("messages", []) if agent.get_state(cfg).values else []
    for m in hist:
        if isinstance(m, HumanMessage):
            st.chat_message("user").write(m.content)
        elif isinstance(m, AIMessage):
            if m.tool_calls:
                with st.chat_message("assistant"):
                    with st.expander("🔧 도구 호출", expanded=False):
                        for tc in m.tool_calls:
                            st.code(f"{tc['name']}({tc['args']})")
                    if m.content:
                        st.write(m.content)
            elif m.content:
                st.chat_message("assistant").write(m.content)
        elif isinstance(m, ToolMessage):
            with st.chat_message("assistant"):
                with st.expander(f"📄 {m.name} 결과", expanded=False):
                    st.code(str(m.content)[:3000])
    q = st.chat_input("예: Step 3에서 p99가 튄 이유가 GC인지 커넥션 대기인지 구분 가능해?")
    if q:
        with st.spinner("분석 중…"):
            agent.invoke({"messages": [HumanMessage(content=q)]}, cfg)
        st.rerun()


def render(graph) -> None:
    st.header("4. 성능 및 GC 사후 교차 분석 (SC-002B)")
    run_id = st.session_state.get("run_id")
    if not run_id:
        st.info("먼저 Tab1에서 진단을 실행하세요.")
        return
    snap = driver.snapshot(graph, run_id)
    vals = snap.values or {}
    phase = driver.phase_of(snap)
    rep = vals.get("analysis_report")
    if not rep:
        st.info(f"아직 분석 결과가 없습니다 (현재 단계: {phase}). Tab3에서 실행하거나 JTL을 업로드하세요.")
        return

    st.markdown(f"### {rep.headline}")
    c1, c2, c3 = st.columns(3)
    c1.metric("주 병목 계층", rep.primary_bottleneck.layer)
    c2.metric("신뢰도", f"{rep.primary_bottleneck.confidence:.0%}")
    c3.metric("분석 출처", "LLM" if rep.source == "llm" else "규칙 기반")
    for e in rep.primary_bottleneck.evidence:
        st.caption("• " + e)

    ss = vals.get("step_summary") or []
    if ss:
        st.subheader("Step별 집계")
        st.dataframe(pd.DataFrame(ss), hide_index=True, use_container_width=True)

    st.subheader("시계열")
    _charts(vals.get("bucket_series_path"), vals.get("gc_series_path"), vals.get("anomalies"))

    an = vals.get("anomalies") or []
    if an:
        with st.expander(f"관심 구간 {len(an)}개", expanded=False):
            st.dataframe(pd.DataFrame([a.model_dump() for a in an]), hide_index=True, use_container_width=True)

    st.subheader("리포트")
    st.markdown(render_report_md(vals))
    rp = vals.get("report_path")
    if rp and Path(rp).exists():
        st.download_button("⬇️ report.md 다운로드", data=Path(rp).read_bytes(), file_name=f"report_{run_id}.md", mime="text/markdown")

    if rep.next_plan and rep.next_plan.steps:
        st.subheader("다음 테스트 제안")
        st.dataframe(pd.DataFrame([s.model_dump() for s in rep.next_plan.steps]), hide_index=True, use_container_width=True)
        if st.button("↩️ 제안 플랜을 Tab1/Tab2에 반영 (같은 Run에서 재실행)", type="primary"):
            with st.status("플랜 반영 및 재진단…", expanded=True) as status:
                driver.update_and_continue(graph, run_id, {"load_plan": rep.next_plan}, as_node="build_load_plan", status=status)
            st.session_state["plan_approved"] = False
            st.session_state.pop("pending_plan", None)
            st.success("플랜이 반영되었습니다. Tab1에서 검토·승인 후 Tab2부터 다시 진행하세요.")

    st.divider()
    _chat(graph, run_id)
