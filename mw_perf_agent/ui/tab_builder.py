from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from config.settings import get_settings
from model.schemas import Assertion, RequestSpec, ScenarioSpec
from . import driver

KIND_LABELS = {
    "url": "URL 직접 입력",
    "curl": "curl 명령 (LLM)",
    "har": "HAR 파일 (LLM)",
    "openapi": "OpenAPI 스펙 + 자연어 (LLM)",
    "nl": "자연어 시나리오 설명 (LLM)",
}


def _scenario_input_form(run_id: str) -> dict | None:
    s = get_settings()
    kinds = list(KIND_LABELS)
    kind = st.radio("입력 소스", kinds, format_func=lambda k: KIND_LABELS[k], horizontal=True,
                    help="URL 외 소스는 LLM(llm_design_scenario)이 ScenarioSpec을 설계합니다. LLM이 없으면 빈 템플릿을 직접 채웁니다.")
    c1, c2, c3 = st.columns([3, 1, 1])
    host = c1.text_input("대상 호스트 (LLM이 바꿀 수 없음)", value="localhost")
    port = c2.number_input("포트", 1, 65535, 8080)
    protocol = c3.selectbox("프로토콜", ["http", "https"])
    payload, method, body, headers = "", None, None, {}
    if kind == "url":
        payload = st.text_input("API URL", value=f"http://{host}:{port}/api/v1/test")
        a, b = st.columns([1, 3])
        method = a.selectbox("HTTP Method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
        body = b.text_area("Request Body (JSON)", height=80) or None
    elif kind == "har":
        up = st.file_uploader("HAR 파일", type=["har", "json"])
        payload = up.read().decode("utf-8", errors="replace") if up else ""
        if payload:
            try:
                entries = json.loads(payload).get("log", {}).get("entries", [])
                slim = [{"method": e["request"]["method"], "url": e["request"]["url"],
                         "postData": (e["request"].get("postData") or {}).get("text", "")[:500]} for e in entries[:200]]
                payload = json.dumps(slim, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                pass
    else:
        ph = {"curl": "curl -X POST http://host/api/login -H 'Content-Type: application/json' -d '{...}'\ncurl http://host/api/orders",
              "openapi": "<OpenAPI JSON/YAML 붙여넣기>\n\n시나리오: 로그인 후 주문 목록 조회",
              "nl": "로그인 API 호출 후 받은 토큰으로 상품 검색 → 장바구니 담기 → 주문 생성"}[kind]
        payload = st.text_area("입력", height=160, placeholder=ph)
    hdr = st.text_input("공통 헤더 (key: value; 세미콜론 구분)", value="", placeholder="Accept: application/json; X-Env: perf")
    for part in hdr.split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            headers[k.strip()] = v.strip()
    think = st.number_input("Think time (ms)", 0, 60000, 0, 100)
    if kind != "url" and not s.llm_enabled:
        st.warning("LLM이 비활성화되어 있어 자동 설계가 불가합니다. 다음 단계에서 빈 템플릿을 직접 채우게 됩니다.")
    return {"kind": kind, "payload": payload, "method": method, "body": body, "headers": headers,
            "host": host.strip(), "port": int(port), "protocol": protocol, "think_time_ms": int(think)}


def _spec_editor(spec: ScenarioSpec, run_id: str) -> ScenarioSpec:
    c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
    name = c1.text_input("시나리오 이름", value=spec.name, key=f"sp_name_{run_id}")
    host = c2.text_input("호스트", value=spec.host, key=f"sp_host_{run_id}")
    port = c3.number_input("포트", 1, 65535, spec.port, key=f"sp_port_{run_id}")
    protocol = c4.selectbox("프로토콜", ["http", "https"], index=0 if spec.protocol == "http" else 1, key=f"sp_proto_{run_id}")
    dh = st.text_area("공통 헤더 (JSON)", value=json.dumps(spec.default_headers, ensure_ascii=False), height=60, key=f"sp_dh_{run_id}")
    think = st.number_input("Think time (ms)", 0, 60000, spec.think_time_ms, 100, key=f"sp_think_{run_id}")
    rows = [{"label": r.label, "method": r.method, "path": r.path, "body": r.body or "",
             "headers": json.dumps(r.headers, ensure_ascii=False),
             "extract": json.dumps([e.model_dump() for e in r.extract], ensure_ascii=False),
             "assert_code": r.assertions[0].expected if r.assertions else "200"} for r in spec.requests]
    if not rows:
        rows = [{"label": "GET /", "method": "GET", "path": "/", "body": "", "headers": "{}", "extract": "[]", "assert_code": "200"}]
    df = st.data_editor(pd.DataFrame(rows), num_rows="dynamic", hide_index=True, use_container_width=True, key=f"sp_req_{run_id}",
                        column_config={"method": st.column_config.SelectboxColumn("method", options=["GET", "POST", "PUT", "PATCH", "DELETE"])})
    reqs: list[RequestSpec] = []
    for _, r in df.iterrows():
        if not str(r.get("path") or "").strip():
            continue
        try:
            headers = json.loads(r.get("headers") or "{}")
        except json.JSONDecodeError:
            headers = {}
        try:
            extract = json.loads(r.get("extract") or "[]")
        except json.JSONDecodeError:
            extract = []
        body = str(r.get("body") or "") or None
        if body and not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = "application/json"
        reqs.append(RequestSpec(label=str(r.get("label") or f"{r['method']} {r['path']}"), method=str(r["method"]), path=str(r["path"]),
                                headers=headers, body=body, extract=extract,
                                assertions=[Assertion(kind="response_code", expected=str(r.get("assert_code") or "200"))]))
    try:
        default_headers = json.loads(dh or "{}")
    except json.JSONDecodeError:
        default_headers = spec.default_headers
    return ScenarioSpec(name=name, protocol=protocol, host=host.strip() or spec.host, port=int(port), default_headers=default_headers,
                        requests=reqs, think_time_ms=int(think))


def render(graph) -> None:
    st.header("2. JMeter 스크립트 빌더 (SC-002A)")
    run_id = st.session_state.get("run_id")
    if not run_id:
        st.info("먼저 Tab1에서 진단을 실행하세요.")
        return
    snap = driver.snapshot(graph, run_id)
    vals = snap.values or {}
    phase = driver.phase_of(snap)

    if phase == "review_plan":
        if not st.session_state.get("plan_approved"):
            st.warning("Tab1에서 플랜을 승인하면 시나리오를 입력할 수 있습니다.")
            return
        st.subheader("시나리오 입력")
        si = _scenario_input_form(run_id)
        if st.button("🧩 시나리오 생성", type="primary"):
            if si["kind"] == "url" and not si["payload"]:
                st.warning("URL을 입력하세요.")
            elif si["kind"] != "url" and not si["payload"]:
                st.warning("입력을 채우세요.")
            else:
                payload = {"scenario_input": si}
                if st.session_state.get("pending_plan"):
                    payload["load_plan"] = st.session_state["pending_plan"]
                with st.status("시나리오 설계 중…", expanded=True) as status:
                    driver.resume_sync(graph, run_id, payload, status=status)
                st.rerun()
        return

    if phase == "review_scenario":
        intr = driver.current_interrupt(snap) or {}
        if intr.get("error_explanation"):
            st.error(intr["error_explanation"])
        v = vals.get("validation")
        if v and not v.ok:
            st.dataframe(pd.DataFrame(v.checks), hide_index=True, use_container_width=True)
        st.subheader("시나리오 검토/편집 (ScenarioSpec)")
        spec = vals.get("scenario_spec") or ScenarioSpec.model_validate(intr.get("scenario_spec") or {})
        edited = _spec_editor(spec, run_id)
        if st.button("📦 JMX 번들 생성", type="primary"):
            if not edited.requests:
                st.warning("요청을 1개 이상 입력하세요.")
            else:
                with st.status("렌더링 및 검증 중…", expanded=True) as status:
                    driver.resume_sync(graph, run_id, {"scenario_spec": edited.model_dump()}, status=status)
                st.rerun()
        return

    bundle = vals.get("bundle")
    if not bundle:
        st.info(f"현재 단계: {phase}")
        return
    v = vals.get("validation")
    st.subheader("✅ 검증 결과")
    if v:
        st.dataframe(pd.DataFrame(v.checks), hide_index=True, use_container_width=True)
    spec = vals.get("scenario_spec")
    plan = vals.get("load_plan")
    if spec and plan:
        st.caption(f"{spec.protocol}://{spec.host}:{spec.port} · 요청 {len(spec.requests)}개 · ThreadGroup {len(plan.steps)}개 직렬 · 총 {plan.total_duration_sec}s")
    t1, t2, t3 = st.tabs(["plan.jmx", "user.properties", "run.sh"])
    for tab, path, lang in ((t1, bundle.jmx, "xml"), (t2, bundle.properties, "properties"), (t3, bundle.run_sh, "bash")):
        with tab:
            p = Path(path)
            if p.exists():
                st.code(p.read_text(encoding="utf-8")[:20000], language=lang)
    if bundle.zip and Path(bundle.zip).exists():
        st.download_button("⬇️ 실행 번들(zip) 다운로드", data=Path(bundle.zip).read_bytes(), file_name=Path(bundle.zip).name, mime="application/zip")
    c1, c2 = st.columns(2)
    if Path(bundle.jmx).exists():
        c1.download_button("plan.jmx", data=Path(bundle.jmx).read_bytes(), file_name="plan.jmx", mime="text/xml")
    if Path(bundle.properties).exists():
        c2.download_button("user.properties", data=Path(bundle.properties).read_bytes(), file_name="user.properties", mime="text/plain")
    if phase == "confirm_run":
        st.success("번들이 준비되었습니다. [SC-002C] 탭에서 실행하세요.")
