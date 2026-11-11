from __future__ import annotations

import shlex
from pathlib import Path

import pandas as pd
import streamlit as st

from config.settings import allow_host, get_settings, update_settings
from graph.nodes.diagnose import new_run_id
from model.load_plan import SCENARIO_CATALOG
from model.schemas import ParsedConfig, TargetInfo
from tools.ssh_wrapper import (
    DEFAULT_SSH_PORT,
    DEFAULT_STRICT_HOST_KEY,
    DEFAULT_TIMEOUT_SEC,
    RemoteConfigError,
    build_ssh_argv,
    fetch_from_homes,
    parse_user_host,
)
from . import driver

STRICT_HOST_KEY_LABELS = {
    "accept-new": "최초 접속 허용 (accept-new, 권장)",
    "yes": "엄격 — known_hosts에 있어야 함",
    "no": "검사 안 함 (비권장)",
}

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
INPUT_KEYS = ("web_mon_cron.env", "httpd-mpm.conf", "tomcat.env")


def _persist_uploaded_identity(uploaded) -> str:
    dest_dir = Path(get_settings().runs_dir) / ".ssh-keys"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "uploaded_identity"
    dest.write_bytes(uploaded.getvalue())
    dest.chmod(0o600)
    return str(dest)


def _show_ssh_error(exc: Exception) -> None:
    msg = str(exc)
    if "# hint:" in msg:
        err, _, hint = msg.partition("# hint:")
        err = err.replace("# error:", "").strip() or "SSH 수집 실패"
        st.error(err)
        st.warning(hint.strip())
    else:
        st.error(msg)


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


_SCENARIO_LABELS = {
    "breakpoint": "1. 계층별 임계치 돌파 테스트 (현재 기본 모델)",
    "spike": "2. 스파이크 테스트 (Spike Test)",
    "sla": "3. 목표 SLA 검증 테스트 (Load / SLA Test)",
}


def _init_scenario_state(s) -> None:
    if "diag_scenario_type" not in st.session_state:
        initial = s.plan_scenario_type if s.plan_scenario_type in SCENARIO_CATALOG else "breakpoint"
        st.session_state["diag_scenario_type"] = initial
    if "diag_n_target" not in st.session_state:
        st.session_state["diag_n_target"] = int(s.plan_n_target)
    if "diag_spike_ramp" not in st.session_state:
        st.session_state["diag_spike_ramp"] = min(3, max(1, int(s.plan_spike_ramp_sec)))


def _render_scenario_picker(s) -> None:
    """Choose which SC-001 load shape `build_load_plan` will emit."""
    _init_scenario_state(s)
    st.subheader("부하 시나리오 선택")
    st.caption("진단으로 임계치를 구한 뒤, 여기서 고른 유형의 공식으로 동시 사용자·Ramp-up·Hold를 산출합니다.")
    choice = st.selectbox(
        "시나리오 유형",
        options=list(_SCENARIO_LABELS.keys()),
        format_func=lambda k: _SCENARIO_LABELS[k],
        key="diag_scenario_type",
    )
    info = SCENARIO_CATALOG[choice]
    st.markdown(
        f"**목적** {info['purpose']}  \n"
        f"**부하 패턴** {info['pattern']}  \n"
        f"**기대 검증** {info['verification']}"
    )
    st.dataframe(
        pd.DataFrame([{"산정 공식": line} for line in info["formulas"]]),
        hide_index=True,
        use_container_width=True,
    )
    n_target = int(st.session_state["diag_n_target"])
    spike_ramp = int(st.session_state["diag_spike_ramp"])
    if choice == "sla":
        n_target = int(st.number_input(
            "목표 동시접속자 N_target",
            min_value=1,
            max_value=100000,
            step=10,
            key="diag_n_target",
            help="S1=0.5×N_target, S2=1.0×N_target, S3=1.2×N_target. N_target > T_db_lock 이면 사전 경고를 출력합니다.",
        ))
    elif choice == "spike":
        spike_ramp = int(st.number_input(
            "스파이크 Ramp-up (초)",
            min_value=1,
            max_value=3,
            step=1,
            key="diag_spike_ramp",
            help="급등 구간만 1~3초이며 Hold는 30초로 고정됩니다. 평시·복귀 구간은 사이드바 Ramp-up 배율과 Hold를 사용합니다.",
        ))
    update_settings(
        plan_scenario_type=choice,
        plan_n_target=int(n_target),
        plan_spike_ramp_sec=int(spike_ramp),
    )


def _plan_options(s) -> dict:
    return {
        "interpret": s.interpret_enabled,
        "ramp_scale": s.plan_ramp_scale,
        "hold_sec": s.plan_hold_sec,
        "bucket_sec": s.bucket_sec,
        "dry_run": True,
        "scenario_type": s.plan_scenario_type,
        "n_target": int(s.plan_n_target),
        "spike_ramp_sec": int(s.plan_spike_ramp_sec),
    }


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
        "options": _plan_options(s),
    }
    with st.status("그래프 실행 중 (SC-001)…", expanded=True) as status:
        driver.drive_sync(graph, run_id, inp, status=status)
        status.update(label="진단 완료 — 플랜을 검토하세요", state="complete")
    st.rerun()


def render(graph) -> None:
    st.header("1. 인프라 진단 및 부하 플랜 산출 (SC-001)")
    st.caption("설정 원문(tomcat.env, httpd-mpm.conf)은 붙여넣지 않습니다. SSH 사용자·비밀번호 또는 키로 서버에서 읽어 옵니다. 부하 형태는 아래 세 가지 시나리오 중 하나를 고릅니다.")

    s = get_settings()
    col1, col2, col3 = st.columns(3)
    with col1:
        host = st.text_input("대상 호스트", placeholder="was01.corp.local 또는 ec2-user@10.0.0.1", key="diag_host")
    with col2:
        apache_path = st.text_input("Apache 홈 또는 httpd-mpm.conf 경로", value="/sw/apache", key="diag_apache")
    with col3:
        tomcat_path = st.text_input("Tomcat 홈 또는 tomcat.env 경로", value="/sw/tomcat", key="diag_tomcat")

    if "diag_wrapper" not in st.session_state:
        st.session_state["diag_wrapper"] = s.ssh_wrapper_cmd or ""
    if "diag_ssh_user" not in st.session_state:
        st.session_state["diag_ssh_user"] = s.ssh_user
    if "diag_ssh_identity" not in st.session_state:
        st.session_state["diag_ssh_identity"] = s.ssh_identity_file
    if "diag_ssh_strict" not in st.session_state:
        st.session_state["diag_ssh_strict"] = (
            s.ssh_strict_host_key if s.ssh_strict_host_key in STRICT_HOST_KEY_LABELS else DEFAULT_STRICT_HOST_KEY
        )
    if "diag_ssh_port" not in st.session_state:
        st.session_state["diag_ssh_port"] = int(s.ssh_port or DEFAULT_SSH_PORT)
    if "diag_ssh_timeout" not in st.session_state:
        st.session_state["diag_ssh_timeout"] = DEFAULT_TIMEOUT_SEC
    if "diag_ssh_password" not in st.session_state:
        st.session_state["diag_ssh_password"] = s.ssh_password or ""

    with st.expander("SSH 접속 (사용자 · 비밀번호 · 키)", expanded=True):
        st.markdown(
            "비밀번호는 명령줄에 넣지 않고 OpenSSH `SSH_ASKPASS`로 전달합니다. "
            "EC2/RHEL WAS처럼 `Permission denied (publickey, gssapi-…)` 가 나오면 "
            "**서버가 비밀번호를 받지 않는 것**이므로 해당 계정용 개인키(.pem)를 넣으세요. "
            "`Host key verification failed` 는 호스트 키 정책을 **최초 접속 허용**으로 두면 등록됩니다."
        )
        u1, u2, u3 = st.columns(3)
        with u1:
            ssh_user = st.text_input(
                "SSH 사용자",
                key="diag_ssh_user",
                placeholder="ec2-user / wasadm",
                help="환경변수 SSH_USER. 비우면 이 프로세스를 실행 중인 OS 사용자, 또는 호스트 칸의 user@host",
            )
        with u2:
            ssh_password = st.text_input(
                "SSH 비밀번호",
                key="diag_ssh_password",
                type="password",
                help="환경변수 SSH_PASSWORD. 그래프 체크포인트에는 저장되지 않습니다.",
            )
        with u3:
            ssh_port = st.number_input("SSH 포트", min_value=1, max_value=65535, step=1, key="diag_ssh_port")
        strict_choice = st.selectbox(
            "호스트 키 정책",
            options=list(STRICT_HOST_KEY_LABELS.keys()),
            format_func=lambda k: STRICT_HOST_KEY_LABELS[k],
            key="diag_ssh_strict",
            help="accept-new: 처음 보는 호스트는 known_hosts에 추가하고, 키가 바뀐 호스트는 거부합니다.",
        )
        ssh_identity = st.text_input(
            "개인키 파일 경로",
            key="diag_ssh_identity",
            placeholder="~/.ssh/id_ed25519 또는 /home/ec2-user/.ssh/key.pem",
            help="환경변수 SSH_IDENTITY_FILE. 비우면 ssh-agent / 기본 키를 사용합니다.",
        )
        uploaded = st.file_uploader("개인키 업로드 (선택, 이 서버의 임시 파일로 저장)", key="diag_ssh_key_upload")
        identity_path = ssh_identity.strip()
        if uploaded is not None:
            identity_path = _persist_uploaded_identity(uploaded)
            st.caption(f"업로드한 키를 `{identity_path}` 에 저장했습니다 (권한 600).")
        wrapper = st.text_input(
            "사내 SSH 래퍼 명령 템플릿 (비우면 OpenSSH 직접 호출)",
            key="diag_wrapper",
            help="고급: SSH_WRAPPER_CMD. 예: /opt/tools/sshrun {host} cat {path}  — {user} {port} {identity} 도 치환됩니다.",
        )
        timeout = st.number_input("수집 타임아웃(초)", min_value=5, max_value=120, step=5, key="diag_ssh_timeout")
        update_settings(
            ssh_wrapper_cmd=wrapper.strip(),
            ssh_user=ssh_user.strip(),
            ssh_port=int(ssh_port),
            ssh_identity_file=identity_path,
            ssh_password=ssh_password,
            ssh_strict_host_key=strict_choice,
        )
        try:
            preview = build_ssh_argv(
                (host or "").strip() or "example-host",
                "/sw/tomcat/shl/tomcat.env",
                template=wrapper,
                must_exist_identity=False,
            )
            extra = " · 비밀번호: ASKPASS로 전달 (명령줄에 없음)" if ssh_password else ""
            st.caption("실행 명령 미리보기: `" + " ".join(shlex.quote(p) for p in preview) + "`" + extra)
        except Exception as extra_exc:
            st.caption(f"명령 미리보기 불가: {extra_exc}")

    _render_scenario_picker(s)

    run_ssh, run_mock = st.columns(2)
    ssh_clicked = run_ssh.button("SSH로 설정 수집 및 진단", type="primary", use_container_width=True)
    mock_clicked = run_mock.button("샘플(Mock)로 미리보기", use_container_width=True)

    host_s = (host or "").strip()
    apache_s = (apache_path or "").strip() or "/sw/apache"
    tomcat_s = (tomcat_path or "").strip() or "/sw/tomcat"
    host_ok = ""
    if host_s:
        try:
            host_ok, _ = parse_user_host(host_s)
        except ValueError:
            host_ok = host_s

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
                allow_host(configs.host or host_ok, persist=False)
                _run_diagnosis(
                    graph,
                    configs.to_raw_inputs(),
                    f"SSH (`{host_s}`)",
                    collected=configs.files,
                    target=TargetInfo(host=configs.host or host_ok, apache_home=apache_s, tomcat_home=tomcat_s),
                )
            except (RemoteConfigError, ValueError) as exc:
                _show_ssh_error(exc)
                if isinstance(exc, RemoteConfigError) and exc.configs and exc.configs.errors:
                    with st.expander("수집 실패 상세"):
                        for path, err in exc.configs.errors.items():
                            st.write(f"- `{path}`: {err}")

    elif mock_clicked:
        raw = _sample_raw(host_s, apache_s, tomcat_s)
        mock_host = host_ok or host_s or "was01.corp.local"
        mock_target = TargetInfo(
            host=mock_host,
            apache_home=apache_s,
            tomcat_home=tomcat_s,
        )
        allow_host(mock_host, persist=False)
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
        st.info("호스트와 경로를 입력하고 시나리오를 고른 뒤 SSH로 설정을 수집하면 임계치와 부하 플랜이 산출됩니다.")
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
        opts = dict(vals.get("options") or {})
        opts.update(_plan_options(s))
        with st.status("재계산 중…", expanded=True) as status:
            driver.update_and_continue(
                graph, run_id,
                {"parsed_config": ParsedConfig(**data), "options": opts},
                as_node="merge_validate",
                status=status,
            )
        st.rerun()

    st.subheader("📊 3계층 병목 임계치")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("DB Lock 임계치", f"{thr.t_db_lock} Conn", help=thr.formulas.get("t_db_lock"))
    m2.metric("WAS Queueing 임계치", f"{thr.t_queue} Threads", help=thr.formulas.get("t_queue"))
    m3.metric("HTTP 503 Drop 임계치", f"{thr.t_drop} Req", help=thr.formulas.get("t_drop"))
    m4.metric("Web 용량 (보조)", f"{thr.web_cap or '-'}", help=thr.formulas.get("web_cap_note") or thr.formulas.get("web_cap"))

    plan_title = plan.scenario_label or "부하 플랜"
    st.subheader(f"📈 {plan_title} ({len(plan.steps)}단계)")
    if plan.purpose:
        st.caption(f"목적: {plan.purpose}")
    if plan.verification_points:
        st.caption("기대 검증: " + " ".join(plan.verification_points))
    if plan.scenario_type == "sla" and plan.n_target:
        st.caption(f"N_target = {plan.n_target}")
    seen_warnings: list[str] = []
    for warning in vals.get("warnings") or []:
        text = str(warning)
        if text in seen_warnings:
            continue
        seen_warnings.append(text)
        if text.startswith("사전 경고"):
            st.warning(text)
    plan_df = pd.DataFrame([step.model_dump() for step in plan.steps])
    plan_sig = "-".join(str(step.target_concurrency) for step in plan.steps)
    plan_edit = st.data_editor(
        plan_df,
        hide_index=True,
        use_container_width=True,
        disabled=["name", "expected_symptom"],
        key=f"plan_editor_{run_id}_{plan.scenario_type}_{plan.n_target}_{plan_sig}",
        column_config={
            "target_concurrency": st.column_config.NumberColumn("동시 사용자", min_value=1, step=1),
            "ramp_up_sec": st.column_config.NumberColumn("Ramp-up(s)", min_value=1, step=1),
            "hold_sec": st.column_config.NumberColumn("Hold(s)", min_value=1, step=1),
        },
    )
    total = int((plan_edit["ramp_up_sec"] + plan_edit["hold_sec"]).sum())
    st.caption(f"총 소요 시간 약 {total}초 · duration = ramp-up + hold (JMeter Scheduler 기준)")
    if phase == "review_plan":
        chosen = s.plan_scenario_type
        stale = chosen != plan.scenario_type
        if chosen == "sla" and int(s.plan_n_target) != int(plan.n_target or 0):
            stale = True
        if chosen == "spike" and len(plan.steps) > 1 and plan.steps[1].ramp_up_sec != int(s.plan_spike_ramp_sec):
            stale = True
        if stale:
            st.info("위쪽 시나리오 선택이 현재 플랜과 다릅니다. 재계산하면 표의 동시 사용자·Ramp-up·Hold가 새 공식으로 바뀝니다.")
        if st.button("선택한 시나리오로 플랜 재계산"):
            opts = dict(vals.get("options") or {})
            opts.update(_plan_options(s))
            with st.status("플랜 재계산 중…", expanded=True) as status:
                driver.update_and_continue(
                    graph, run_id, {"options": opts}, as_node="compute_thresholds", status=status,
                )
            st.rerun()

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

    if seen_warnings:
        with st.expander(f"⚠ 경고 {len(seen_warnings)}건"):
            for w in seen_warnings:
                st.write("- " + w)

    if phase == "review_plan":
        if st.button("✅ 플랜 승인 → 시나리오 단계로", type="primary"):
            approved = plan.model_dump()
            approved["steps"] = plan_edit.to_dict(orient="records")
            approved["total_duration_sec"] = total
            st.session_state["pending_plan"] = approved
            st.session_state["plan_approved"] = True
            st.success("플랜이 승인되었습니다. [SC-002A] 탭에서 시나리오를 입력하세요.")
    else:
        st.info(f"현재 단계: {phase}. 플랜은 이미 확정되었습니다. 변경하려면 Tab4의 '제안 플랜 반영' 또는 사이드바 체크포인트에서 되돌리세요.")
