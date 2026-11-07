from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st
from langgraph.types import Command

from config.settings import allow_host, get_settings
from jmeter.jtl import statistics_table
from tools.jmeter_runner import request_stop
from . import driver

MODE_LABELS = {"local": "Local (JMETER_HOME)", "docker": "Docker", "remote": "분산 (-R)", "bundle_only": "번들만 (외부 실행 후 JTL 업로드)"}


def _progress_panel(monitor: driver.RunMonitor, plan) -> None:
    latest = monitor.latest_summary()
    if latest:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("현재 Step", latest.get("step") or "-")
        c2.metric("TPS", f"{latest['tps']:.1f}")
        c3.metric("Err %", f"{latest['err_pct']:.2f}")
        c4.metric("Active", latest["active"])
        if plan:
            mx = max(s.target_concurrency for s in plan.steps)
            st.progress(min(1.0, latest["active"] / max(1, mx)), text=f"동시 사용자 {latest['active']}/{mx}")
    recent = monitor.recent(12)
    if recent:
        st.dataframe(pd.DataFrame([{k: v for k, v in e.items() if k != "cmd"} for e in recent]), hide_index=True, use_container_width=True, height=240)
    if monitor.node_log:
        st.caption(" · ".join(monitor.node_log[-4:]))


def render(graph) -> None:
    st.header("3. JMeter 실행기 (SC-002C)")
    run_id = st.session_state.get("run_id")
    if not run_id:
        st.info("먼저 Tab1에서 진단을 실행하세요.")
        return
    s = get_settings()
    snap = driver.snapshot(graph, run_id)
    vals = snap.values or {}
    phase = driver.phase_of(snap)
    plan = vals.get("load_plan")
    run_dir = Path(s.runs_dir) / run_id

    monitor: driver.RunMonitor | None = st.session_state.get("monitor")
    if monitor and not monitor.done:
        st.subheader("⏱ 실행 중")

        @st.fragment(run_every="1s")
        def live() -> None:
            m = st.session_state.get("monitor")
            if m is None:
                return
            _progress_panel(m, plan)
            if m.done:
                st.rerun()

        if st.button("⏹ 중지", type="secondary"):
            request_stop(run_dir)
            st.warning("중지 요청을 보냈습니다. JMeter 종료 후 실행 확인 단계로 돌아갑니다.")
        live()
        return
    if monitor and monitor.done:
        if monitor.error:
            st.error(f"그래프 실행 오류: {monitor.error}")
        else:
            st.success("실행 및 분석 그래프가 종료되었습니다.")
        with st.expander("마지막 실행 로그", expanded=False):
            _progress_panel(monitor, plan)
        st.session_state["last_monitor"] = monitor
        st.session_state.pop("monitor", None)
        snap = driver.snapshot(graph, run_id)
        vals = snap.values or {}
        phase = driver.phase_of(snap)

    if phase in ("new", "review_plan", "review_scenario"):
        st.info("Tab2에서 JMX 번들을 생성하면 실행할 수 있습니다.")
        return

    if phase == "confirm_run":
        pf = vals.get("preflight") or []
        spec = vals.get("scenario_spec")
        if pf:
            st.subheader("사전 점검 결과 (이전 시도)")
            st.dataframe(pd.DataFrame([c.model_dump() for c in pf]), hide_index=True, use_container_width=True)
            blocked = [c for c in pf if c.blocking and not c.ok]
            if blocked:
                st.error("차단 항목이 있어 실행이 거부되었습니다. 원인을 해결한 뒤 다시 실행하세요.")
            host_blocked = next((c for c in blocked if c.name == "allowed_host"), None)
            if host_blocked and spec and spec.host:
                st.warning(
                    f"`{spec.host}` 가 부하 허용 목록에 없습니다. "
                    "SC-001에서 진단한 대상이면 아래 버튼으로 추가한 뒤 다시 실행하세요."
                )
                if st.button(f"허용 목록에 {spec.host} 추가", key=f"allow_{run_id}_{spec.host}"):
                    allow_host(spec.host, persist=True)
                    st.success(f"`{spec.host}` 를 허용 목록에 추가했습니다. 다시 [JMeter 실행]을 누르세요.")
                    st.rerun()
            reachable = next((c for c in pf if c.name == "target_reachable"), None)
            if reachable and not reachable.ok:
                st.info(reachable.detail)
                if spec and "localhost" in (reachable.detail or "") and "target.host=localhost" in (reachable.detail or ""):
                    st.caption("아래 속성 오버라이드에 `target.host=localhost` 한 줄을 넣으면 같은 머신의 Tomcat으로 부하를 보냅니다.")
        rr = vals.get("run_result")
        if rr and rr.stopped:
            st.warning(f"이전 실행이 중지되었습니다 (exit={rr.exit_code}).")
        for w in (vals.get("warnings") or [])[-3:]:
            st.caption("⚠ " + w)

        st.subheader("실행 설정")
        modes = list(MODE_LABELS)
        default = "local" if s.jmeter_available else "bundle_only"
        mode = st.radio("실행 모드", modes, index=modes.index(default), format_func=lambda m: MODE_LABELS[m], horizontal=True)
        if mode == "local" and not s.jmeter_available:
            st.error("jmeter 실행 파일을 찾을 수 없습니다. 사이드바에서 JMETER_HOME을 설정하거나 Docker/번들 모드를 사용하세요.")
        if mode == "remote" and not s.remote_hosts:
            st.error("사이드바에서 분산 모드 원격 호스트(-R)를 설정하세요.")
        overrides_txt = st.text_area("속성 오버라이드 (-J, key=value 줄바꿈)", value="", height=70, placeholder="step4.threads=400\ntarget.response_timeout=20000")
        overrides = {}
        for line in overrides_txt.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                overrides[k.strip()] = v.strip()
        c1, c2 = st.columns(2)
        gc_text = c1.text_area("GC 로그 / jstat -gcutil 출력 (옵션)", height=120, key=f"gc_{run_id}")
        gc_file = c1.file_uploader("또는 GC 로그 파일", type=["txt", "log", "csv"], key=f"gcf_{run_id}")
        if gc_file:
            gc_text = gc_file.read().decode("utf-8", errors="replace")
        err_text = c2.text_area("WAS/Web 오류 로그 (옵션: catalina.out, error_log 발췌)", height=120, key=f"err_{run_id}")

        if mode == "bundle_only":
            up = st.file_uploader("외부에서 실행한 result.jtl 업로드", type=["jtl", "csv"], key=f"jtl_{run_id}")
            if st.button("📥 JTL 업로드 후 분석 진행", type="primary", disabled=up is None):
                dest = run_dir / "uploaded.jtl"
                dest.write_bytes(up.read())
                payload = {"run_mode": "bundle_only", "uploaded_jtl_path": str(dest), "gc_input": gc_text or None, "error_log_input": err_text or None}
                with st.status("분석 그래프 실행 중…", expanded=True) as status:
                    driver.resume_sync(graph, run_id, payload, status=status)
                st.rerun()
        else:
            disabled = (mode == "local" and not s.jmeter_available) or (mode == "remote" and not s.remote_hosts)
            if plan:
                st.caption(f"예상 소요 시간 약 {plan.total_duration_sec}s + 리포트 생성. 실행 중에는 '중지' 버튼으로 종료할 수 있습니다.")
            if st.button("▶️ JMeter 실행", type="primary", disabled=disabled):
                payload = {"run_mode": mode, "gc_input": gc_text or None, "error_log_input": err_text or None, "overrides": overrides}
                st.session_state["monitor"] = driver.start_background(graph, run_id, Command(resume=payload))
                st.rerun()
        return

    # done / running phases
    rr = vals.get("run_result")
    if rr:
        st.subheader("실행 결과")
        c1, c2, c3 = st.columns(3)
        c1.metric("exit code", rr.exit_code)
        c2.metric("시작", rr.started_at.strftime("%H:%M:%S"))
        c3.metric("종료", rr.finished_at.strftime("%H:%M:%S") if rr.finished_at else "-")
        log = Path(rr.log_path) if rr.log_path else None
        if log and log.exists():
            with st.expander("jmeter.log (tail)"):
                st.code("\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]))
    stats = vals.get("statistics")
    if stats:
        st.subheader("statistics.json 요약 (Dashboard)")
        st.dataframe(pd.DataFrame(statistics_table(stats)), hide_index=True, use_container_width=True)
    if vals.get("jtl_path"):
        st.caption(f"JTL: `{vals['jtl_path']}` → Tab4에서 분석 결과를 확인하세요.")
    if phase == "running":
        st.info("그래프가 진행 중입니다 (백그라운드).")
