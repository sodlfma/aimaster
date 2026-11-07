from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from config.settings import get_settings
from graph.nodes.diagnose import new_run_id
from model.schemas import ParsedConfig
from tools.ssh_wrapper import fetch_remote_configs
from . import driver

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
INPUT_KEYS = ("web_mon_cron.env", "httpd-mpm.conf", "tomcat.env")
PHASE_ORDER = {"new": 0, "review_plan": 1, "review_scenario": 2, "confirm_run": 3, "running": 4, "done": 5}


def _load_sample() -> None:
    for k in INPUT_KEYS:
        p = FIXTURES / k
        st.session_state[f"in_{k}"] = p.read_text(encoding="utf-8") if p.exists() else ""


def render(graph) -> None:
    st.header("1. 인프라 진단 및 4단계 부하 플랜 산출 (SC-001)")
    s = get_settings()

    with st.expander("입력", expanded=st.session_state.get("run_id") is None):
        c1, c2 = st.columns([3, 1])
        with c2:
            st.button("샘플 불러오기", on_click=_load_sample, use_container_width=True)
            if s.ssh_wrapper_cmd:
                host = st.text_input("SSH 수집 호스트", value="")
                paths = st.text_area("수집 경로 (줄바꿈)", value="shl/tomcat.env\nconf/httpd-mpm.conf", height=80)
                if st.button("SSH 래퍼로 수집", use_container_width=True) and host:
                    try:
                        got = fetch_remote_configs(host, [p for p in paths.splitlines() if p.strip()])
                        for path, content in got.items():
                            key = "tomcat.env" if "tomcat" in path else "httpd-mpm.conf" if "httpd" in path else path
                            st.session_state[f"in_{key}"] = content
                        st.success(f"{len(got)}개 파일 수집")
                    except Exception as e:  # noqa: BLE001
                        st.error(str(e))
            else:
                st.caption("SSH 래퍼는 `SSH_WRAPPER_CMD` 환경변수 설정 시 활성화됩니다.")
        with c1:
            a, b, c = st.columns(3)
            a.text_area("web_mon_cron.env", key="in_web_mon_cron.env", height=180)
            b.text_area("httpd-mpm.conf", key="in_httpd-mpm.conf", height=180)
            c.text_area("tomcat.env / server.xml", key="in_tomcat.env", height=180)
        extra = st.text_area("기타 설정 원문 (옵션: setenv.sh, context.xml 등)", key="in_extra", height=80)

        if st.button("🚀 진단 및 4단계 플랜 산출", type="primary"):
            raw = {k: st.session_state.get(f"in_{k}", "") for k in INPUT_KEYS}
            if extra:
                raw["extra.conf"] = extra
            if not any(raw.values()):
                st.warning("설정 원문을 하나 이상 입력하세요.")
            else:
                run_id = new_run_id()
                st.session_state["run_id"] = run_id
                st.session_state.pop("monitor", None)
                st.session_state["plan_approved"] = False
                inp = {
                    "run_id": run_id,
                    "raw_inputs": raw,
                    "llm_enabled": s.llm_enabled,
                    "options": {"interpret": s.interpret_enabled, "ramp_scale": s.plan_ramp_scale, "hold_sec": s.plan_hold_sec,
                                "bucket_sec": s.bucket_sec, "dry_run": True},
                }
                with st.status("그래프 실행 중 (SC-001)…", expanded=True) as status:
                    driver.drive_sync(graph, run_id, inp, status=status)
                    status.update(label="진단 완료 — 플랜을 검토하세요", state="complete")
                st.rerun()

    run_id = st.session_state.get("run_id")
    if not run_id:
        st.info("설정 원문을 입력하고 진단을 실행하면 임계치와 4단계 플랜이 산출됩니다.")
        return

    snap = driver.snapshot(graph, run_id)
    vals = snap.values or {}
    phase = driver.phase_of(snap)
    if "thresholds" not in vals:
        st.warning("아직 진단 결과가 없습니다.")
        return

    cfg: ParsedConfig = vals["parsed_config"]
    thr = vals["thresholds"]
    plan = vals["load_plan"]

    st.subheader("📋 파싱 결과")
    rows = []
    for f, v in cfg.model_dump().items():
        if f in ("source", "confidence", "evidence"):
            continue
        rows.append({"field": f, "value": v, "evidence": cfg.evidence.get(f, "")})
    df = pd.DataFrame(rows)
    st.caption(f"출처: **{cfg.source}** · confidence: **{cfg.confidence:.2f}**" + ("  ⚠ 기본값 포함" if any("기본값" in e for e in cfg.evidence.values()) else ""))
    edited = st.data_editor(df, hide_index=True, use_container_width=True, disabled=["field", "evidence"], key=f"cfg_editor_{run_id}")
    if phase == "review_plan" and st.button("설정값 수정 반영 → 임계치 재계산"):
        data = cfg.model_dump()
        for _, r in edited.iterrows():
            v = r["value"]
            if pd.isna(v) or v == "" or v is None:
                data[r["field"]] = None
            elif r["field"] in ("mpm_module", "gc_type"):
                data[r["field"]] = str(v)
            else:
                data[r["field"]] = int(float(v))
                data["evidence"][r["field"]] = cfg.evidence.get(r["field"]) or "(사용자 수정)"
        with st.status("재계산 중…", expanded=True) as status:
            driver.update_and_continue(graph, run_id, {"parsed_config": ParsedConfig(**data)}, as_node="merge_validate", status=status)
        st.rerun()

    st.subheader("📊 3계층 병목 임계치")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("DB Lock 임계치", f"{thr.t_db_lock} Conn", help=thr.formulas.get("t_db_lock"))
    m2.metric("WAS Queueing 임계치", f"{thr.t_queue} Threads", help=thr.formulas.get("t_queue"))
    m3.metric("HTTP 503 Drop 임계치", f"{thr.t_drop} Req", help=thr.formulas.get("t_drop"))
    m4.metric("Web 용량 (보조)", f"{thr.web_cap or '-'}", help=thr.formulas.get("web_cap_note") or thr.formulas.get("web_cap"))

    st.subheader("📈 4단계 부하 플랜")
    plan_df = pd.DataFrame([s.model_dump() for s in plan.steps])
    plan_edit = st.data_editor(plan_df, hide_index=True, use_container_width=True, disabled=["name", "expected_symptom"],
                               key=f"plan_editor_{run_id}", column_config={
                                   "target_concurrency": st.column_config.NumberColumn("동시 사용자", min_value=1, step=1),
                                   "ramp_up_sec": st.column_config.NumberColumn("Ramp-up(s)", min_value=1, step=1),
                                   "hold_sec": st.column_config.NumberColumn("Hold(s)", min_value=1, step=1)})
    total = int((plan_edit["ramp_up_sec"] + plan_edit["hold_sec"]).sum())
    st.caption(f"총 소요 시간 약 {total}초 · duration = ramp-up + hold (JMeter Scheduler 기준)")

    rep = vals.get("diagnosis_report")
    if rep:
        with st.expander(f"🧠 진단 해석 리포트 ({'LLM' if rep.source == 'llm' else '규칙 기반 — LLM 생략'})", expanded=False):
            st.markdown(rep.summary)
            if rep.bottleneck_order:
                st.caption("병목 발생 순서: " + " → ".join(rep.bottleneck_order))
            if rep.antipatterns:
                st.markdown("**설정 안티패턴**")
                for a in rep.antipatterns:
                    st.markdown(f"- **{a.title}** `{a.severity}` — {a.detail}" + (f" _(근거: {', '.join(a.evidence)})_" if a.evidence else ""))
            if rep.rule_findings and rep.source == "llm":
                st.markdown("**규칙 엔진 탐지**")
                for f in rep.rule_findings:
                    st.markdown(f"- {f}")
            if rep.checklist:
                st.markdown("**테스트 전 체크리스트**")
                for c in rep.checklist:
                    st.markdown(f"- {c}")
            if rep.recommendations:
                st.markdown("**권고 설정 초안**")
                st.dataframe(pd.DataFrame([r.model_dump() for r in rep.recommendations]), hide_index=True, use_container_width=True)

    if vals.get("warnings"):
        with st.expander(f"⚠ 경고 {len(vals['warnings'])}건"):
            for w in vals["warnings"]:
                st.write("- " + w)

    if phase == "review_plan":
        if st.button("✅ 플랜 승인 → 시나리오 단계로", type="primary"):
            st.session_state["pending_plan"] = {"steps": plan_edit.to_dict(orient="records"), "total_duration_sec": total}
            st.session_state["plan_approved"] = True
            st.success("플랜이 승인되었습니다. [SC-002A] 탭에서 시나리오를 입력하세요.")
    else:
        st.info(f"현재 단계: {phase}. 플랜은 이미 확정되었습니다. 변경하려면 Tab4의 '제안 플랜 반영' 또는 사이드바 체크포인트에서 되돌리세요.")
