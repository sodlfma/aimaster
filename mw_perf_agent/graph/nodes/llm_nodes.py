"""LLM nodes (LLM-01..04, 06). Each node: build context → invoke_llm →
write state; on LLMUnavailable or after retries fail, fall back to rules and
record the node in `llm_skipped`."""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional, Type

import pandas as pd
from pydantic import BaseModel

from jmeter.render import apply_user_target
from llm.invoke import LLMUnavailable, invoke_llm
from llm.schemas import (
    AnalysisReport,
    Bottleneck,
    DiagnosisReport,
    ExtractedConfig,
    Finding,
    PredictionCompare,
    Recommendation,
)
from model.schemas import LoadPlan, LoadStep, ParsedConfig, ScenarioSpec
from model.thresholds import rule_antipatterns
from ..state import PerfAgentState

_INT_FIELDS = {"tomcat_max_threads", "tomcat_accept_count", "tomcat_port", "dbcp_max_total", "dbcp_max_wait_ms",
               "jvm_xmx_mb", "jvm_xms_mb", "max_request_workers", "threads_per_child"}


def _hosts(state: PerfAgentState) -> list[str]:
    hosts = []
    ti = state.get("target_info")
    if ti and ti.host:
        hosts.append(ti.host)
    si = state.get("scenario_input")
    if si and si.host:
        hosts.append(si.host)
    sp = state.get("scenario_spec")
    if sp and sp.host:
        hosts.append(sp.host)
    return hosts


LLM_MAX_ATTEMPTS = 3
LLM_BACKOFF_SEC = 1.0


def _llm_node(
    name: str,
    prompt: str,
    schema: Optional[Type[BaseModel]],
    build_ctx: Callable[[PerfAgentState], dict],
    to_state: Callable[[PerfAgentState, Any], dict],
    fallback: Callable[[PerfAgentState], dict],
    temperature: float = 0.0,
):
    """Build an LLM node. Provider errors are retried with exponential backoff
    (LLM_MAX_ATTEMPTS); when retries are exhausted or no model is configured the
    rule-based fallback result is written instead and the node is recorded in
    `llm_skipped`, so the graph always continues (spec 2.1 principle 3)."""

    def node(state: PerfAgentState) -> dict:
        if not state.get("llm_enabled", False):
            return {**fallback(state), "llm_skipped": [name], "node_log": [f"{name}: skipped (llm disabled)"]}
        ctx = build_ctx(state)
        last_err: Exception | None = None
        for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
            try:
                out, rec = invoke_llm(name, prompt, schema, ctx, temperature=temperature, mask_hosts=_hosts(state))
                res = to_state(state, out)
                res["llm_calls"] = [rec.as_dict()]
                res["node_log"] = [f"{name}: ok ({rec.latency_ms}ms, ~{rec.approx_input_tokens} tok, attempt {attempt})"]
                return res
            except LLMUnavailable:
                return {**fallback(state), "llm_skipped": [name], "node_log": [f"{name}: skipped (no model)"]}
            except Exception as e:  # noqa: BLE001 — provider/parsing error
                last_err = e
                if attempt < LLM_MAX_ATTEMPTS:
                    time.sleep(LLM_BACKOFF_SEC * (2 ** (attempt - 1)))
        return {
            **fallback(state),
            "llm_skipped": [name],
            "warnings": [f"{name}: LLM 호출이 {LLM_MAX_ATTEMPTS}회 실패하여 규칙 기반 결과를 사용합니다 ({str(last_err)[:120]})"],
            "node_log": [f"{name}: fallback after {LLM_MAX_ATTEMPTS} failed attempts"],
        }

    node.__name__ = name
    node.fallback = fallback  # type: ignore[attr-defined]
    return node


# ---------------- LLM-01 extract_config ----------------
def _ctx_extract(state: PerfAgentState) -> dict:
    pc = state["parsed_config"]
    known = {k: v for k, v in pc.model_dump().items() if v is not None and k not in ("source", "confidence", "evidence")}
    all_fields = ["tomcat_max_threads", "tomcat_accept_count", "tomcat_port", "dbcp_max_total", "dbcp_max_wait_ms",
                  "jvm_xmx_mb", "jvm_xms_mb", "gc_type", "max_request_workers", "threads_per_child", "mpm_module"]
    missing = [f for f in all_fields if f not in known]
    raw_text = "\n\n".join(f"### {k}\n{v}" for k, v in (state.get("raw_inputs") or {}).items() if v)
    return {"known": known, "missing": missing, "raw_text": raw_text[:30000]}


def _extract_to_state(state: PerfAgentState, out: ExtractedConfig) -> dict:
    data: dict = {"evidence": {}}
    for f in out.fields:
        if f.field not in ParsedConfig.model_fields or f.field in ("source", "confidence", "evidence"):
            continue
        val: Any = f.value
        if f.field in _INT_FIELDS:
            try:
                val = int(float(str(val).strip().rstrip("mMgG")))
            except ValueError:
                continue
        if f.field == "mpm_module" and str(val).lower() not in ("prefork", "worker", "event"):
            continue
        data[f.field] = val
        data["evidence"][f.field] = f.evidence
    data["source"] = "llm"
    try:
        cfg = ParsedConfig(**data)
    except Exception:
        cfg = ParsedConfig(source="llm")
    return {"parsed_config_llm": cfg}


llm_extract_config = _llm_node(
    "llm_extract_config", "extract_config", ExtractedConfig, _ctx_extract, _extract_to_state,
    fallback=lambda state: {"parsed_config_llm": None},
)


# ---------------- LLM-02 interpret_diagnosis ----------------
def rules_diagnosis_report(state: PerfAgentState) -> DiagnosisReport:
    return _rules_report(state)


def _rules_report(state: PerfAgentState) -> DiagnosisReport:
    cfg, thr, plan = state["parsed_config"], state["thresholds"], state["load_plan"]
    findings = rule_antipatterns(cfg, thr)
    order = ["db", "was", "web"] if thr.t_db_lock <= thr.t_queue else ["was", "db", "web"]
    if thr.web_cap is not None and thr.web_cap < thr.t_drop:
        order = ["web"] + [o for o in order if o != "web"]
    summary = (
        f"DB 커넥션 풀은 동시 {thr.t_db_lock}명에서 고갈되고, Tomcat 워커는 {thr.t_queue}명부터 큐잉되며, "
        f"{thr.t_drop}명을 넘기면 accept 큐가 차서 503/연결 거부가 발생할 것으로 예상됩니다. "
        f"병목 순서: {' → '.join(order)}. (규칙 기반 요약, LLM 해석 생략)"
    )
    return DiagnosisReport(
        summary=summary,
        bottleneck_order=order,
        antipatterns=[Finding(title=f.split(":")[0][:60], detail=f, severity="warning") for f in findings],
        checklist=[
            "WAS: jstat -gcutil <pid> 5000 (FGC/GCT 증가 추적)",
            "DBCP: active/idle/wait 카운트 및 maxWait 초과 오류 로그",
            "Tomcat: currentThreadsBusy, acceptCount 관련 Connection refused",
            "Apache: error_log의 MaxRequestWorkers 도달 메시지",
            "OS: netstat -s (listen queue overflow), ss -lnt Recv-Q",
        ],
        recommendations=[],
        rule_findings=findings,
        source="rules",
    )


def _ctx_interpret(state: PerfAgentState) -> dict:
    cfg, thr, plan = state["parsed_config"], state["thresholds"], state["load_plan"]
    return {
        "parsed_config": {k: v for k, v in cfg.model_dump().items() if k != "evidence"},
        "thresholds": thr,
        "load_plan": plan,
        "rule_findings": rule_antipatterns(cfg, thr),
        "warnings": state.get("warnings", []),
    }


def _interpret_to_state(state: PerfAgentState, out: DiagnosisReport) -> dict:
    out.rule_findings = rule_antipatterns(state["parsed_config"], state["thresholds"])
    out.source = "llm"
    return {"diagnosis_report": out}


llm_interpret_diagnosis = _llm_node(
    "llm_interpret_diagnosis", "interpret_diagnosis", DiagnosisReport, _ctx_interpret, _interpret_to_state,
    fallback=lambda state: {"diagnosis_report": _rules_report(state)},
)


# ---------------- LLM-03 design_scenario ----------------
def _ctx_design(state: PerfAgentState) -> dict:
    si = state["scenario_input"]
    extra = ""
    if si.method or si.body:
        extra = f"method={si.method or ''}; body={si.body or ''}"
    return {"kind": si.kind, "payload": si.payload[:40000], "target": {"host": si.host, "port": si.port, "protocol": si.protocol}, "extra": extra}


def _design_to_state(state: PerfAgentState, out: ScenarioSpec) -> dict:
    spec = apply_user_target(out, state["scenario_input"])
    spec.requests = spec.requests[:10]
    return {"scenario_spec": spec}


def _design_fallback(state: PerfAgentState) -> dict:
    si = state["scenario_input"]
    return {"scenario_spec": ScenarioSpec(host=si.host or "localhost", port=si.port or 8080, protocol=si.protocol, think_time_ms=si.think_time_ms),
            "warnings": ["LLM 없이 시나리오를 자동 설계할 수 없어 빈 템플릿을 제공합니다. 요청을 직접 입력하세요."]}


llm_design_scenario = _llm_node(
    "llm_design_scenario", "design_scenario", ScenarioSpec, _ctx_design, _design_to_state, _design_fallback, temperature=0.3,
)


# ---------------- LLM-04 analyze_run ----------------
def _rules_analysis(state: PerfAgentState) -> AnalysisReport:
    ss = state.get("step_summary") or []
    anomalies = state.get("anomalies") or []
    plan: LoadPlan | None = state.get("load_plan")
    thr = state.get("thresholds")
    kinds = {a.kind for a in anomalies}
    layer, conf, evidence = "none", 0.3, []
    if "full_gc" in kinds and "latency_spike" in kinds:
        layer, conf = "jvm", 0.6
        evidence.append("Full GC 이벤트와 지연 급증 구간이 함께 관측됨")
    elif "http_503" in kinds:
        layer, conf = "was", 0.6
        evidence.append("HTTP 503 발생 → accept 큐 포화(T_drop) 징후")
    elif "error_rate" in kinds:
        layer, conf = "db", 0.5
        evidence.append("에러율 상승 구간 존재 (DB 커넥션 대기/타임아웃 가능성)")
    elif "latency_spike" in kinds:
        layer, conf = "was", 0.5
        evidence.append("p99 지연 급증 구간 존재 (스레드 큐잉 가능성)")
    for a in anomalies[:3]:
        evidence.append(f"[{a.kind}] {a.step} {a.start_ms}~{a.end_ms}: {a.detail}")

    pva: list[PredictionCompare] = []
    by_step = {r["step"]: r for r in ss}
    if plan:
        for s in plan.steps:
            r = by_step.get(s.name)
            if not r:
                pva.append(PredictionCompare(step=s.name, predicted=s.expected_symptom, actual="데이터 없음", verdict="unknown"))
                continue
            actual = f"TPS {r['tps']}, p99 {r['p99_ms']}ms, Err {r['err_rate']}%, 503 {r['http_503']}건"
            bad = r["err_rate"] >= 1.0 or r["http_503"] > 0
            if "정상" in s.expected_symptom or "베이스라인" in s.expected_symptom:
                verdict = "match" if not bad else "mismatch"
            elif "503" in s.expected_symptom:
                verdict = "match" if r["http_503"] > 0 else ("partial" if bad else "mismatch")
            elif "DB" in s.expected_symptom:
                verdict = "match" if bad else "mismatch"
            else:
                verdict = "match" if (r["p99_ms"] >= 3 * (ss[0]["p99_ms"] if ss else 1)) else "partial"
            pva.append(PredictionCompare(step=s.name, predicted=s.expected_symptom, actual=actual, verdict=verdict))

    recs: list[Recommendation] = []
    cfg: ParsedConfig | None = state.get("parsed_config")
    if cfg and layer == "jvm":
        recs.append(Recommendation(component="jvm", parameter="-Xmx", current=f"{cfg.jvm_xmx_mb or '?'}m",
                                   proposed=f"{(cfg.jvm_xmx_mb or 1024) * 2}m", rationale="Full GC 빈발 — 힙 여유 확보", risk="OS 메모리 여유 확인 필요"))
    if cfg and layer == "db":
        recs.append(Recommendation(component="dbcp", parameter="maxTotal", current=str(cfg.dbcp_max_total),
                                   proposed=str(int((cfg.dbcp_max_total or 50) * 1.5)), rationale="커넥션 대기 오류", risk="DB max_connections 상한 확인"))
    if cfg and layer == "was":
        recs.append(Recommendation(component="tomcat", parameter="maxThreads", current=str(cfg.tomcat_max_threads),
                                   proposed=str(int((cfg.tomcat_max_threads or 200) * 1.25)), rationale="큐잉/503", risk="DB 커넥션 수와 균형 필요"))

    correlations = anomalies_overlap(anomalies)
    headline = f"규칙 기반 요약: 주 병목 후보는 '{layer}' 계층입니다 (관심 구간 {len(anomalies)}개). LLM 해석은 생략되었습니다." if layer != "none" \
        else f"규칙 기반 요약: 뚜렷한 병목 징후가 없습니다 (관심 구간 {len(anomalies)}개). LLM 해석은 생략되었습니다."
    return AnalysisReport(headline=headline, primary_bottleneck=Bottleneck(layer=layer, confidence=conf, evidence=evidence),
                          prediction_vs_actual=pva, correlations=correlations, recommendations=recs,
                          next_plan=_rules_next_plan(plan, pva, by_step), source="rules")


def _rules_next_plan(plan: LoadPlan | None, pva: list[PredictionCompare], by_step: dict) -> LoadPlan | None:
    """Narrow the next test around the first step whose symptoms appeared, or push
    the ceiling up when nothing broke. Users review/edit it at gate_review_plan."""
    if not plan or not plan.steps:
        return None
    steps = plan.steps
    first_bad = next((i for i, s in enumerate(steps)
                      if (r := by_step.get(s.name)) and (r["err_rate"] >= 1.0 or r["http_503"] > 0)), None)
    if first_bad is None:
        top = steps[-1].target_concurrency
        targets = [int(top * f) for f in (1.0, 1.2, 1.4, 1.6)]
    else:
        lo = steps[first_bad - 1].target_concurrency if first_bad > 0 else max(1, steps[first_bad].target_concurrency // 2)
        hi = steps[first_bad].target_concurrency
        targets = [lo, int(lo + (hi - lo) * 0.33), int(lo + (hi - lo) * 0.66), hi]
    out: list[LoadStep] = []
    prev = 0
    for s, t in zip(steps, targets):
        t = max(prev + 1, t)
        out.append(LoadStep(name=s.name, target_concurrency=t, ramp_up_sec=s.ramp_up_sec, hold_sec=s.hold_sec,
                            expected_symptom=("상한 탐색" if first_bad is None else "임계 지점 정밀 탐색")))
        prev = t
    return LoadPlan(steps=out).recompute_total()


def anomalies_overlap(anomalies) -> list[str]:
    from analysis.anomalies import anomalies_overlap_summary
    return anomalies_overlap_summary(anomalies)


def _ctx_analyze(state: PerfAgentState) -> dict:
    ctx = dict(state.get("llm_context") or {})
    cfg = state.get("parsed_config")
    ctx.update({
        "thresholds": state["thresholds"],
        "load_plan": state["load_plan"],
        "parsed_config": {k: v for k, v in cfg.model_dump().items() if k != "evidence"} if cfg else {},
    })
    ctx.setdefault("error_log_summary", "")
    return ctx


def _analyze_to_state(state: PerfAgentState, out: AnalysisReport) -> dict:
    out.source = "llm"
    if out.next_plan:
        out.next_plan.recompute_total()
    return {"analysis_report": out}


llm_analyze_run = _llm_node(
    "llm_analyze_run", "analyze_run", AnalysisReport, _ctx_analyze, _analyze_to_state,
    fallback=lambda state: {"analysis_report": _rules_analysis(state)},
)


# ---------------- LLM-06 explain_error ----------------
def _ctx_explain(state: PerfAgentState) -> dict:
    v = state.get("validation")
    errors = list(v.errors) if v else ["unknown error"]
    spec = state.get("scenario_spec")
    return {"stage": "validate_jmx", "errors": errors, "context": json.dumps(spec.model_dump(), ensure_ascii=False)[:3000] if spec else ""}


def _explain_fallback(state: PerfAgentState) -> dict:
    v = state.get("validation")
    errs = "\n".join(f"- {e}" for e in (v.errors if v else []))
    return {"error_explanation": f"JMX 검증에 실패했습니다. 시나리오를 수정한 뒤 다시 생성하세요.\n{errs}"}


llm_explain_error = _llm_node(
    "llm_explain_error", "explain_error", None, _ctx_explain,
    lambda state, out: {"error_explanation": str(out)}, _explain_fallback,
)
