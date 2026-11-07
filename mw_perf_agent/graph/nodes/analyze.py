"""SC-002B deterministic nodes."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pandas as pd

from analysis.anomalies import anomalies_overlap_summary, detect_anomalies
from analysis.gc_parser import gc_summary_text, parse_gc_text
from analysis.stats import aggregate_buckets, buckets_to_csv_text, step_summary
from config.settings import get_settings
from jmeter.jtl import load_jtl
from ..state import PerfAgentState

_ERR_PATTERNS = {
    "dbcp_timeout": re.compile(r"Cannot get a connection|Timeout waiting for idle object|pool exhausted", re.I),
    "apache_maxworkers": re.compile(r"server reached MaxRequestWorkers|MaxClients setting", re.I),
    "tomcat_reject": re.compile(r"Connection refused|RejectedExecutionException|Too many open files", re.I),
    "oom": re.compile(r"OutOfMemoryError|GC overhead limit", re.I),
    "timeout": re.compile(r"SocketTimeoutException|Read timed out", re.I),
}


def aggregate_jtl(state: PerfAgentState) -> dict:
    s = get_settings()
    run_dir = s.run_dir(state["run_id"])
    jtl_path = state.get("jtl_path")
    if not jtl_path or not Path(jtl_path).exists():
        return {"bucket_series_path": None, "step_summary": [], "node_log": ["aggregate_jtl: no jtl"]}
    df = load_jtl(jtl_path)
    opts = state.get("options") or {}
    bucket_sec = int(opts.get("bucket_sec", s.bucket_sec))
    buckets = aggregate_buckets(df, bucket_sec)
    out = run_dir / "buckets.csv"
    buckets.to_csv(out, index=False)
    summary = step_summary(df, state.get("load_plan"))
    (run_dir / "step_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"bucket_series_path": str(out), "step_summary": summary, "node_log": [f"aggregate_jtl: {len(df)} samples → {len(buckets)} buckets"]}


def parse_gc(state: PerfAgentState) -> dict:
    s = get_settings()
    run_dir = s.run_dir(state["run_id"])
    text = state.get("gc_input") or ""
    start_ms = None
    rr = state.get("run_result")
    if rr is not None:
        start_ms = int(rr.started_at.timestamp() * 1000)
    gc = parse_gc_text(text, start_ms=start_ms, interval_ms=5000)
    out = run_dir / "gc.csv"
    gc.to_csv(out, index=False)
    return {"gc_series_path": str(out), "node_log": [f"parse_gc: {len(gc)} rows"]}


def detect_anomalies_node(state: PerfAgentState) -> dict:
    s = get_settings()
    opts = state.get("options") or {}
    bucket_sec = int(opts.get("bucket_sec", s.bucket_sec))
    bp, gp = state.get("bucket_series_path"), state.get("gc_series_path")
    buckets = pd.read_csv(bp) if bp and Path(bp).exists() else pd.DataFrame()
    gc = pd.read_csv(gp) if gp and Path(gp).exists() else pd.DataFrame()
    plan = state.get("load_plan")
    baseline = plan.steps[0].name if plan and plan.steps else None
    anomalies = detect_anomalies(buckets, gc, bucket_sec=bucket_sec, baseline_step=baseline)
    return {"anomalies": anomalies, "node_log": [f"detect_anomalies: {Counter(a.kind for a in anomalies)}"]}


def _error_log_summary(text: str | None) -> str:
    if not text:
        return ""
    counts: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    for line in text.splitlines():
        for name, pat in _ERR_PATTERNS.items():
            if pat.search(line):
                counts[name] = counts.get(name, 0) + 1
                samples.setdefault(name, [])
                if len(samples[name]) < 3:
                    samples[name].append(line.strip()[:200])
    if not counts:
        return "(알려진 오류 패턴 없음)"
    return "\n".join(f"{k}: {v}건\n  " + "\n  ".join(samples[k]) for k, v in counts.items())


def compress_context(state: PerfAgentState) -> dict:
    """Build the compact LLM context (spec 4.5). Masking is applied in invoke_llm."""
    s = get_settings()
    bp, gp = state.get("bucket_series_path"), state.get("gc_series_path")
    buckets = pd.read_csv(bp) if bp and Path(bp).exists() else pd.DataFrame()
    gc = pd.read_csv(gp) if gp and Path(gp).exists() else pd.DataFrame()
    anomalies = state.get("anomalies", [])
    ctx = {
        "step_summary_csv": pd.DataFrame(state.get("step_summary", [])).to_csv(index=False) if state.get("step_summary") else "(no data)",
        "bucket_csv": buckets_to_csv_text(buckets, max_rows=300),
        "gc_summary": gc_summary_text(gc),
        "anomalies": [a.model_dump() for a in anomalies],
        "overlaps": anomalies_overlap_summary(anomalies),
        "error_log_summary": _error_log_summary(state.get("error_log_input")),
        "max_tokens": s.llm_max_context_tokens,
    }
    return {"llm_context": ctx, "node_log": [f"compress_context: ~{sum(len(str(v)) for v in ctx.values())//3} tokens"]}


def persist_report(state: PerfAgentState) -> dict:
    s = get_settings()
    run_dir = s.run_dir(state["run_id"])
    rep = state.get("analysis_report")
    md = render_report_md(state)
    report_path = run_dir / "report.md"
    report_path.write_text(md, encoding="utf-8")

    def dump(v):
        if hasattr(v, "model_dump"):
            return v.model_dump(mode="json")
        if isinstance(v, list):
            return [dump(x) for x in v]
        if isinstance(v, dict):
            return {k: dump(x) for k, x in v.items()}
        return v

    snapshot = {k: dump(v) for k, v in state.items() if k not in ("llm_context", "raw_inputs")}
    (run_dir / "state.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    calls = state.get("llm_calls", [])
    if calls:
        with open(run_dir / "llm_calls.jsonl", "w", encoding="utf-8") as f:
            for c in calls:
                f.write(json.dumps(c, ensure_ascii=False, default=str) + "\n")
    return {"report_path": str(report_path), "node_log": [f"persist_report: {report_path} (source={rep.source if rep else 'none'})"]}


def render_report_md(state: PerfAgentState) -> str:
    rep = state.get("analysis_report")
    plan = state.get("load_plan")
    thr = state.get("thresholds")
    lines = [f"# 성능/GC 사후 분석 리포트 — {state.get('run_id','')}", ""]
    if rep is None:
        lines.append("_분석 리포트가 생성되지 않았습니다._")
        return "\n".join(lines)
    lines += [f"**결론**: {rep.headline}", "",
              f"- 주 병목 계층: **{rep.primary_bottleneck.layer}** (신뢰도 {rep.primary_bottleneck.confidence:.0%})"]
    lines += [f"  - 근거: {e}" for e in rep.primary_bottleneck.evidence]
    lines.append(f"- 분석 출처: {'LLM' if rep.source == 'llm' else '규칙 기반 (LLM 생략)'}")
    if thr:
        lines += ["", f"임계치: T_db_lock={thr.t_db_lock}, T_queue={thr.t_queue}, T_drop={thr.t_drop}"]
    if rep.prediction_vs_actual:
        lines += ["", "## 예측 대비 실측", "", "| Step | 예측 | 실측 | 판정 |", "|---|---|---|---|"]
        lines += [f"| {p.step} | {p.predicted} | {p.actual} | {p.verdict} |" for p in rep.prediction_vs_actual]
    if rep.correlations:
        lines += ["", "## 상관관계"] + [f"- {c}" for c in rep.correlations]
    if rep.recommendations:
        lines += ["", "## 튜닝 권고", "", "| 구성요소 | 파라미터 | 현재 | 권고 | 근거 | 리스크 |", "|---|---|---|---|---|---|"]
        lines += [f"| {r.component} | {r.parameter} | {r.current} | {r.proposed} | {r.rationale} | {r.risk} |" for r in rep.recommendations]
    if rep.next_plan:
        lines += ["", "## 다음 테스트 제안", "", "| Step | 동시 사용자 | Ramp-up | Hold | 기대 증상 |", "|---|---|---|---|---|"]
        lines += [f"| {s.name} | {s.target_concurrency} | {s.ramp_up_sec}s | {s.hold_sec}s | {s.expected_symptom} |" for s in rep.next_plan.steps]
    ss = state.get("step_summary") or []
    if ss:
        lines += ["", "## Step별 집계", "", "| Step | 목표 | 샘플 | TPS | avg | p95 | p99 | Err% | 503 |", "|---|---|---|---|---|---|---|---|---|"]
        lines += [f"| {r['step']} | {r.get('target_concurrency') or ''} | {r['samples']} | {r['tps']} | {r['avg_ms']} | {r['p95_ms']} | {r['p99_ms']} | {r['err_rate']} | {r['http_503']} |" for r in ss]
    an = state.get("anomalies") or []
    if an:
        lines += ["", "## 관심 구간"] + [f"- [{a.kind}] {a.step} {a.start_ms}~{a.end_ms}: {a.detail}" for a in an]
    if plan:
        lines += ["", f"_플랜 총 소요 {plan.total_duration_sec}s, 단계 {len(plan.steps)}개_"]
    return "\n".join(lines)
