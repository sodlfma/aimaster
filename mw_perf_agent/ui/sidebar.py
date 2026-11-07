from __future__ import annotations

import streamlit as st

from config.settings import allow_host, get_settings, update_settings
from graph.build import mermaid
from llm.models import llm_available
from . import driver


def render_sidebar(graph) -> None:
    s = get_settings()
    st.sidebar.header("⚙️ 설정")

    with st.sidebar.expander("LLM", expanded=True):
        model = st.text_input("모델 (provider:model)", value=s.llm_model, placeholder="openai:gpt-4o / anthropic:claude-3-5-sonnet-latest",
                              help="LangChain init_chat_model 형식. 사내 OpenAI 호환 API는 openai:<model> + Base URL")
        base_url = st.text_input("Base URL (옵션)", value=s.llm_base_url or "")
        api_key = st.text_input("API Key", value=s.llm_api_key or "", type="password")
        enabled = st.toggle("LLM 사용", value=s.llm_enabled and bool(model))
        interpret = st.toggle("LLM 해석 리포트 (LLM-02)", value=s.interpret_enabled)
        mask = st.toggle("외부 전송 시 호스트/IP 마스킹", value=s.mask_enabled)
        update_settings(llm_model=model.strip(), llm_base_url=base_url.strip() or None, llm_api_key=api_key.strip() or None,
                        llm_enabled=enabled and bool(model.strip()), interpret_enabled=interpret, mask_enabled=mask)
        if get_settings().llm_enabled:
            st.caption("상태: " + ("✅ 모델 초기화 가능" if llm_available() else "⚠️ 모델 초기화 실패 (패키지/키 확인) → 규칙 기반 Fallback"))
        else:
            st.caption("상태: 오프라인 모드 (규칙 기반 Fallback)")

    with st.sidebar.expander("허용 호스트 (부하 대상)", expanded=False):
        st.caption("JMeter 실행 전 preflight가 이 목록만 허용합니다. SC-001에서 SSH 진단한 호스트는 해당 Run에서 자동 허용됩니다.")
        current = "\n".join(s.allowed_hosts()) or "(localhost / 127.0.0.1 만 기본 허용)"
        st.code(current, language="text")
        extra = st.text_input("호스트 추가", value="", placeholder="3.35.11.139 또는 *.dev.example.com", key="allow_host_add")
        if st.button("허용 목록에 추가", key="allow_host_btn") and extra.strip():
            allow_host(extra.strip(), persist=True)
            st.rerun()

    with st.sidebar.expander("JMeter", expanded=False):
        jh = st.text_input("JMETER_HOME", value=s.jmeter_home or "")
        jb = st.text_input("jmeter 실행 파일 (옵션)", value=s.jmeter_bin_override or "")
        docker_img = st.text_input("Docker 이미지", value=s.docker_image)
        remote = st.text_input("분산 모드 원격 호스트 (-R)", value=s.remote_hosts, placeholder="host1,host2")
        update_settings(jmeter_home=jh.strip() or None, jmeter_bin_override=jb.strip() or None, docker_image=docker_img.strip(), remote_hosts=remote.strip())
        st.caption(f"감지된 실행 파일: `{get_settings().jmeter_bin or '없음'}`")

    with st.sidebar.expander("플랜/분석 기본값", expanded=False):
        ramp = st.slider("Ramp-up 배율", 0.5, 3.0, float(s.plan_ramp_scale), 0.5)
        hold = st.number_input("Hold (초)", 10, 3600, int(s.plan_hold_sec), 10)
        bucket = st.number_input("분석 버킷 (초)", 1, 60, int(s.bucket_sec), 1)
        update_settings(plan_ramp_scale=ramp, plan_hold_sec=int(hold), bucket_sec=int(bucket))

    st.sidebar.divider()
    st.sidebar.subheader("🧵 Run")
    runs = driver.list_runs()
    current = st.session_state.get("run_id")
    options = ["(새 Run)"] + runs
    idx = options.index(current) if current in options else 0
    choice = st.sidebar.selectbox("Run 선택 (thread_id)", options, index=idx)
    if choice == "(새 Run)":
        if current is not None and st.sidebar.button("새 Run 시작"):
            st.session_state.pop("run_id", None)
            st.session_state.pop("monitor", None)
            st.session_state.pop("plan_approved", None)
            st.rerun()
    elif choice != current:
        st.session_state["run_id"] = choice
        st.session_state.pop("monitor", None)
        st.session_state["plan_approved"] = True
        st.rerun()

    if current:
        snap = driver.snapshot(graph, current)
        st.sidebar.caption(f"phase: **{driver.phase_of(snap)}** · next: {', '.join(snap.next) or '(end)'}")
        with st.sidebar.expander("체크포인트 타임라인", expanded=False):
            hist = driver.history(graph, current)
            if hist:
                st.dataframe(hist, hide_index=True, height=220)
                cid = st.selectbox("체크포인트", [h["checkpoint_id"] for h in hist], format_func=lambda c: c[-12:])
                if st.button("이 시점부터 재실행 (fork)"):
                    with st.status("재실행 중...", expanded=True) as status:
                        driver.drive_sync(graph, current, None, status=status, checkpoint_id=cid)
                    st.rerun()
        vals = snap.values or {}
        if vals.get("llm_calls"):
            tok = sum(c.get("approx_input_tokens", 0) for c in vals["llm_calls"])
            st.sidebar.caption(f"LLM 호출 {len(vals['llm_calls'])}회 · 입력 ≈{tok:,} tok")
        if vals.get("llm_skipped"):
            st.sidebar.caption("LLM 생략 노드: " + ", ".join(vals["llm_skipped"]))

    with st.sidebar.expander("그래프 토폴로지 (Mermaid)", expanded=False):
        st.code(mermaid(graph), language="text")
