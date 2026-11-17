"""Live LLM API golden set. Skipped unless RUN_LIVE_LLM=1 (or --run-live) and credentials exist.

Never logs or records the API key. Outbound payloads are checked only for host leakage.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from analysis.anomalies import detect_anomalies
from analysis.stats import aggregate_buckets, step_summary
from graph.nodes.analyze import compress_context
from graph.nodes.diagnose import merge_validate
from graph.nodes.llm_nodes import llm_analyze_run, llm_design_scenario, llm_extract_config, llm_interpret_diagnosis
from jmeter.jtl import load_jtl
from llm.models import _CACHE, clear_model_override, get_model, set_model_override
from model.load_plan import generate_load_plan
from model.schemas import ParsedConfig, ScenarioInput, Thresholds
from model.thresholds import compute_thresholds
from parsers import parse_all
from tests.nfr_harness import standard_config, write_symptom_jtl
from tests.nfr_results import NFR_LLM_INPUT_TOKENS, record

KNOWN_LAYERS = {"db", "was", "web", "jvm", "app", "none"}
SECRET_HOSTS = ("was01.corp.local", "db01.corp.local")


def _live_enabled() -> bool:
    flag = os.environ.get("RUN_LIVE_LLM", "").strip().lower()
    return flag in ("1", "true", "yes")


def _creds() -> tuple[str, str]:
    model = (os.environ.get("LLM_MODEL") or "").strip()
    key = (os.environ.get("LLM_API_KEY") or os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    return model, key


pytestmark = pytest.mark.live


class SpyModel:
    """Delegates to the real chat model while capturing outbound message text."""

    def __init__(self, inner):
        self.inner = inner
        self.payloads: list = []
        for attr in ("model_name", "model", "model_id", "deployment_name"):
            if getattr(inner, attr, None):
                setattr(self, attr, getattr(inner, attr))
                break
        else:
            self.model_name = inner.__class__.__name__

    def with_structured_output(self, schema, **kwargs):
        inner_s = self.inner.with_structured_output(schema, **kwargs)
        payloads = self.payloads

        class _S:
            def invoke(_self, messages, **kw):
                payloads.append(messages)
                return inner_s.invoke(messages, **kw)

        return _S()

    def invoke(self, messages, **kw):
        self.payloads.append(messages)
        return self.inner.invoke(messages, **kw)

    def bind_tools(self, tools):
        return self.inner.bind_tools(tools)


def _payload_text(spy: SpyModel) -> str:
    chunks: list[str] = []
    for msgs in spy.payloads:
        for m in msgs:
            chunks.append(getattr(m, "content", "") or "")
    return "\n".join(chunks)


def _row(case_id: str, item: str, expected, actual, passed: bool, **extra) -> None:
    record("live", f"{case_id}:{item}", case_id=case_id, item=item,
           expected=expected, actual=actual, passed=passed, **extra)
    assert passed, f"{case_id} {item}: expected {expected!r}, got {actual!r}"


def _call_meta(state: dict) -> dict:
    calls = state.get("llm_calls") or []
    rec = calls[-1] if calls else {}
    return {
        "latency_ms": rec.get("latency_ms"),
        "approx_input_tokens": rec.get("approx_input_tokens"),
        "provider_input_tokens": rec.get("provider_input_tokens"),
        "provider_output_tokens": rec.get("provider_output_tokens"),
        "model": rec.get("model"),
        "ok": rec.get("ok"),
        "node": rec.get("node"),
    }


@pytest.fixture
def live_settings(tmp_path, monkeypatch):
    if not _live_enabled():
        pytest.skip("Set RUN_LIVE_LLM=1 (or pytest --run-live) to call the real API")
    model, key = _creds()
    if not model or not key:
        pytest.skip("LLM_MODEL and LLM_API_KEY are required for live tests")

    from config.settings import Settings, reset_settings

    allowed = tmp_path / "allowed_hosts.txt"
    allowed.write_text("localhost\n127.0.0.1\n*.corp.local\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_JMETER_FAST", "1")
    clear_model_override()
    _CACHE.clear()
    s = Settings(
        llm_model=model,
        llm_api_key=key,
        llm_base_url=os.environ.get("LLM_BASE_URL") or None,
        llm_api_version=os.environ.get("LLM_API_VERSION") or None,
        llm_enabled=True,
        mask_enabled=True,
        llm_timeout_sec=int(os.environ.get("LLM_TIMEOUT_SEC", "90")),
        jmeter_bin_override=str(Path(__file__).resolve().parent / "fixtures" / "fake_jmeter.py"),
        runs_dir=str(tmp_path / "runs"),
        allowed_hosts_file=str(allowed),
        ssh_password="",
        ssh_wrapper_cmd="",
    )
    reset_settings(s)
    inner = get_model()
    if inner is None:
        from llm.models import llm_init_error
        pytest.skip(f"live model init failed: {llm_init_error() or 'unknown'}")
    spy = SpyModel(inner)
    set_model_override(spy)
    try:
        yield s, spy
    finally:
        clear_model_override()
        _CACHE.clear()
        reset_settings(Settings())


def _assert_no_host_leak(spy: SpyModel, case_id: str) -> None:
    text = _payload_text(spy)
    leaked = [h for h in SECRET_HOSTS if h in text]
    _row(case_id, "아웃바운드 호스트 마스킹", "was01/db01.corp.local 없음",
         "유출 없음" if not leaked else f"유출 {leaked}", not leaked)


# ---------- L1 interpret ----------
@pytest.mark.live
def test_l1_live_interpret_diagnosis(raw_inputs, live_settings):
    _settings, spy = live_settings
    cfg, _target, _w = parse_all(raw_inputs)
    thr = compute_thresholds(cfg)
    plan, _ = generate_load_plan(thr, hold_sec=60)
    state = {
        "llm_enabled": True,
        "parsed_config": cfg,
        "thresholds": thr,
        "load_plan": plan,
        "warnings": [],
        "target_info": _target,
    }
    out = llm_interpret_diagnosis(state)
    skipped = out.get("llm_skipped") or []
    _row("L1", "LLM 호출 성공", "llm_skipped 없음", str(skipped), "llm_interpret_diagnosis" not in skipped)
    report = out["diagnosis_report"]
    layers = [x.lower() for x in report.bottleneck_order]
    _row("L1", "source", "llm", report.source, report.source == "llm")
    _row("L1", "병목 순서 계층 집합", f"⊆ {sorted(KNOWN_LAYERS)}", str(layers),
         bool(layers) and all(x in KNOWN_LAYERS for x in layers))
    _row("L1", "요약에 임계치 수치", "50 또는 200 또는 300", report.summary[:180],
         any(n in report.summary for n in ("50", "200", "300")))
    _row("L1", "체크리스트 비어 있지 않음", "≥1", str(len(report.checklist)), len(report.checklist) >= 1)
    _assert_no_host_leak(spy, "L1")
    meta = _call_meta(out)
    record("live_metric", "L1 interpret_diagnosis", **meta, passed=True,
           interpretation="실모델 진단 리포트가 스키마를 통과하고 임계치 수치를 인용함")


# ---------- L2 extract from a comment regex skips ----------
@pytest.mark.live
def test_l2_live_extract_commented_dbcp(raw_inputs, live_settings):
    _settings, spy = live_settings
    raw = dict(raw_inputs)
    lines = []
    for line in raw["tomcat.env"].splitlines():
        if "dbcp.maxTotal" in line or "dbcp.maxWait" in line:
            continue
        lines.append(line)
    # Single-quoted XML: regex wants optional `"`, so this line is invisible to
    # parse_tomcat_env but is still in the source the LLM (and merge_validate) see.
    lines.append('<Resource name="jdbc/app" type="javax.sql.DataSource" maxTotal=\'73\'/>')
    raw["tomcat.env"] = "\n".join(lines) + "\n"
    cfg, _target, _w = parse_all(raw)
    assert cfg.dbcp_max_total is None
    state = {"llm_enabled": True, "parsed_config": cfg, "raw_inputs": raw, "target_info": _target}
    spy.payloads.clear()
    out = llm_extract_config(state)
    skipped = out.get("llm_skipped") or []
    _row("L2", "LLM 호출 성공", "llm_skipped 없음", str(skipped), "llm_extract_config" not in skipped)
    llm_cfg = out.get("parsed_config_llm")
    _row("L2", "ExtractedConfig 반환", "parsed_config_llm 존재", str(llm_cfg), llm_cfg is not None)
    merged = merge_validate({**state, "parsed_config_llm": llm_cfg})
    got = merged["parsed_config"].dbcp_max_total
    _row("L2", "정규식이 놓친 maxTotal='73' 추출 후 병합", "73", got, got == 73)
    _assert_no_host_leak(spy, "L2")
    record("live_metric", "L2 extract_config", **_call_meta(out), passed=got == 73,
           interpretation="정규식이 놓친 XML maxTotal='73'을 LLM이 채우고 merge_validate가 원문 근거로 수용")


# ---------- L3 analyze compressed JTL ----------
@pytest.mark.live
def test_l3_live_analyze_run(tmp_path, live_settings):
    settings, spy = live_settings
    path = write_symptom_jtl(tmp_path / "live.jtl")
    df = load_jtl(str(path))
    buckets = aggregate_buckets(df, bucket_sec=5)
    plan, _ = generate_load_plan(Thresholds(t_db_lock=50, t_queue=200, t_drop=300), hold_sec=5)
    summary = step_summary(df, plan)
    anomalies = detect_anomalies(buckets, None, bucket_sec=5, baseline_step="Step 1 (Normal)")
    run_dir = Path(settings.runs_dir) / "live-l3"
    run_dir.mkdir(parents=True, exist_ok=True)
    bp = run_dir / "buckets.csv"
    buckets.to_csv(bp, index=False)
    cfg = standard_config()
    thr = compute_thresholds(cfg)
    compressed = compress_context({
        "run_id": "live-l3",
        "bucket_series_path": str(bp),
        "step_summary": summary,
        "anomalies": anomalies,
    })
    state = {
        "llm_enabled": True,
        "run_id": "live-l3",
        "llm_context": compressed["llm_context"],
        "thresholds": thr,
        "load_plan": plan,
        "parsed_config": cfg,
        "step_summary": summary,
        "anomalies": anomalies,
        "target_info": None,
    }
    spy.payloads.clear()
    out = llm_analyze_run(state)
    skipped = out.get("llm_skipped") or []
    _row("L3", "LLM 호출 성공", "llm_skipped 없음", str(skipped), "llm_analyze_run" not in skipped)
    report = out["analysis_report"]
    layer = report.primary_bottleneck.layer
    _row("L3", "source", "llm", report.source, report.source == "llm")
    _row("L3", "주 병목 계층", "was (503 구간) 또는 db", layer,
         layer in {"was", "db"})
    _row("L3", "근거 evidence", "≥1줄", str(report.primary_bottleneck.evidence),
         len(report.primary_bottleneck.evidence) >= 1)
    meta = _call_meta(out)
    tokens = meta.get("provider_input_tokens") or meta.get("approx_input_tokens") or 0
    _row("L3", "입력 토큰 ≤ 12k 가드", f"≤ {NFR_LLM_INPUT_TOKENS}", tokens,
         int(tokens) <= NFR_LLM_INPUT_TOKENS)
    _assert_no_host_leak(spy, "L3")
    record("live_metric", "L3 analyze_run", **meta, passed=True,
           interpretation="압축된 JTL 컨텍스트만 보내고 실모델이 503 기반 병목을 구조화 출력함")


# ---------- L4 design scenario target lock ----------
@pytest.mark.live
def test_l4_live_design_scenario_keeps_user_target(live_settings):
    _settings, spy = live_settings
    si = ScenarioInput(
        kind="nl",
        payload="로그인 GET /login 후 상품 목록 GET /api/items 를 호출하는 시나리오",
        host="localhost",
        port=8080,
        protocol="http",
    )
    spy.payloads.clear()
    out = llm_design_scenario({"llm_enabled": True, "scenario_input": si})
    skipped = out.get("llm_skipped") or []
    _row("L4", "LLM 호출 성공", "llm_skipped 없음", str(skipped), "llm_design_scenario" not in skipped)
    spec = out["scenario_spec"]
    _row("L4", "호스트/포트/프로토콜 강제", "localhost:8080/http",
         f"{spec.host}:{spec.port}/{spec.protocol}",
         (spec.host, spec.port, spec.protocol) == ("localhost", 8080, "http"))
    _row("L4", "요청 1개 이상", "≥1", str(len(spec.requests)), len(spec.requests) >= 1)
    if spec.requests:
        paths = [r.path for r in spec.requests]
        joined = " ".join(paths)
        _row("L4", "경로가 로그인 또는 items 성격", "/login 또는 /api/items 유사", joined,
             bool(re.search(r"login|items|/api", joined, re.I)))
    _assert_no_host_leak(spy, "L4")
    record("live_metric", "L4 design_scenario", **_call_meta(out), passed=True,
           interpretation="실모델이 시나리오를 설계해도 대상 호스트는 사용자 값으로 덮임")
