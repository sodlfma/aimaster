from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from config.settings import get_settings, update_settings
from graph.nodes.diagnose import new_run_id
from model.schemas import ParsedConfig, TargetInfo
from tools.ssh_wrapper import (
    DEFAULT_SSH_WRAPPER_CMD,
    DEFAULT_TIMEOUT_SEC,
    RemoteConfigError,
    effective_ssh_wrapper_cmd,
    fetch_from_homes,
)
from . import driver

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
INPUT_KEYS = ("web_mon_cron.env", "httpd-mpm.conf", "tomcat.env")


def _sample_raw(host: str, apache_path: str, tomcat_path: str) -> dict[str, str]:
    raw = {}
    for k in INPUT_KEYS:
        p = FIXTURES / k
        raw[k] = p.read_text(encoding="utf-8") if p.exists() else ""
    raw["web_mon_cron.env"] = (
        f"HOST={host or 'was01.corp.local'}\n"
        f"APACHE_HOME={apache_path or '/sw/apache'}\n"
        f"TOMCAT_HOME={tomcat_path or '/sw/tomcat'}\n"
        'INSTANCES="inst01 inst02"\n'
    )
    return raw


def _run_diagnosis(graph, raw: dict[str, str], source: str, collected: dict[str, str] | None, target: TargetInfo) -> None:
    if not any(v.strip() for v in raw.values() if v):
        st.error("수집된 설정 원문이 비어 있습니다.")
        return
    s = get_settings()
    run_id = new_run_id()
    st.session_state["run_id"] = run_id
    st.session_state.pop("monitor", None)
    st.session_state["plan_approved"] = False
    st.session_state["collect_source"] = source
    st.session_state["collected_files"] = collected or {k: v for k, v in raw.items() if v}
    inp = {
        "run_id": run_id,
        "raw_inputs": raw,
        "target_info": target,
        "llm_enabled": s.llm_enabled,
        "options": {
            "interpret": s.interpret_enabled,
            "ramp_scale": s.plan_ramp_scale,
            "hold_sec": s.plan_hold_sec,
            "bucket_sec": s.bucket_sec,
            "dry_run": True,
        },
    }
    with st.status("그래프 실행 중 (SC-001)…", expanded=True) as status:
        driver.drive_sync(graph, run_id, inp, status=status)
        status.update(label="진단 완료 — 플랜을 검토하세요", state="complete")
    st.rerun()


def render(graph) -> None:
    st.header("1. 인프라 진단 및 4단계 부하 플랜 산출 (SC-001)")
    st.caption("설정 원문(tomcat.env, httpd-mpm.conf)은 붙여넣지 않습니다. 호스트와 경로만 주면 SSH 래퍼가 서버에서 읽어 옵니다.")

    col1, col2, col3 = st.columns(3)
    with col1:
        host = st.text_input("대상 호스트", placeholder="was01.corp.local", key="diag_host")
    with col2:
        apache_path = st.text_input("Apache 홈 또는 httpd-mpm.conf 경로", value="/sw/apache", key="diag_apache")
    with col3:
        tomcat_path = st.text_input("Tomcat 홈 또는 tomcat.env 경로", value="/sw/tomcat", key="diag_tomcat")

    if "diag_wrapper" not in st.session_state:
        st.session_state["diag_wrapper"] = effective_ssh_wrapper_cmd() or DEFAULT_SSH_WRAPPER_CMD
    with st.expander("SSH 래퍼 설정"):
        st.markdown(
            "사내 SSH 쉘 실행 프로그램을 `subprocess`로 호출합니다. "
            "`{host}`와 `{path}`가 치환되며, 디렉터리만 주면 사내 표준 상대경로 "
            "`shl/tomcat.env`, `conf/httpd-mpm.conf` 를 붙입니다."
        )
        wrapper = st.text_input(
            "SSH 래퍼 명령 템플릿",
            key="diag_wrapper",
            help="환경변수 SSH_WRAPPER_CMD 또는 예: /opt/tools/sshrun {host} cat {path}",
        )
        timeout = st.number_input("수집 타임아웃(초)", min_value=5, max_value=120, value=DEFAULT_TIMEOUT_SEC, step=5)
        update_settings(ssh_wrapper_cmd=wrapper.strip())

    run_ssh, run_mock = st.columns(2)
    ssh_clicked = run_ssh.button("SSH로 설정 수집 및 진단", type="primary", use_container_width=True)
    mock_clicked = run_mock.button("샘플(Mock)로 미리보기", use_container_width=True)

    host_s = (host or "").strip()
    apache_s = (apache_path or "").strip() or "/sw/apache"
    tomcat_s = (tomcat_path or "").strip() or "/sw/tomcat"
    target = TargetInfo(host=host_s, apache_home=apache_s, tomcat_home=tomcat_s)

    if ssh_clicked:
        if not host_s:
            st.error("대상 호스트를 입력하세요.")
        else:
            try:
                with st.spinner(f"{host_s} 에서 tomcat.env / httpd-mpm.conf 수집 중…"):
                    configs = fetch_from_homes(
                        host_s,
                        apache_s,
                        tomcat_s,
                        ssh_wrapper_cmd=wrapper,
                        timeout=int(timeout),
                    )
                _run_diagnosis(
                    graph,
                    configs.to_raw_inputs(),
                    f"SSH (`{host_s}`)",
                    collected=configs.files,
                    target=target,
                )
            except (RemoteConfigError, ValueError) as exc:
                st.error(str(exc))
                if isinstance(exc, RemoteConfigError) and exc.configs and exc.configs.errors:
                    with st.expander("수집 실패 상세"):
                        for path, err in exc.configs.errors.items():
                            st.write(f"- `{path}`: {err}")

    elif mock_clicked:
        raw = _sample_raw(host_s, apache_s, tomcat_s)
        mock_target = TargetInfo(
            host=host_s or "was01.corp.local",
            apache_home=apache_s,
            tomcat_home=tomcat_s,
        )
        _run_diagnosis(graph, raw, "샘플(Mock)", collected=raw, target=mock_target)

    collected = st.session_state.get("collected_files") or {}
    if collected:
        source = st.session_state.get("collect_source", "수집")
        with st.expander("수집된 설정 원문", expanded=st.session_state.get("run_id") is None):
            st.caption(f"{source} · host=`{host_s or '(없음)'}` · apache=`{apache_s}` · tomcat=`{tomcat_s}`")
            for path, content in collected.items():
                st.markdown(f"**`{path}`**")
                st.code((content or "").strip() or "(empty)", language="bash")

    run_id = st.session_state.get("run_id")
    if not run_id:
        st.info("호스트와 경로를 입력한 뒤 SSH로 설정을 수집하면 임계치와 4단계 플랜이 산출됩니다.")
        return

    snap = driver.snapshot(graph, run_id)
    vals = snap.values or {}
    phase = driver.phase_of(snap)
    if "thresholds" not in vals:
        st.warning("아직 진단 결과가 없습니다.")
        return

    source = st.session_state.get("collect_source")
    if source:
        st.success(f"{source} 수집 후 부하 플랜 산출이 완료되었습니다. 다음 탭으로 이동하세요.")

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
