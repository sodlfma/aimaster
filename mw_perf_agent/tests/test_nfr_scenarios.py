"""Representative before/after scenarios (mentor: 전/후 비교 표 2~3개).

S1 컨텍스트 압축 — 원본 JTL vs compress_context 후 analyze_run 프롬프트 토큰.
S2 고신뢰 정규식 시 LLM 추출 생략 — 필드 누락(2회 호출) vs 정규식 성공(1회).
S3 LLM 장애 Fallback — 제공자 실패 시 규칙 경로로 게이트까지 완주.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from langgraph.types import Command

from analysis.anomalies import detect_anomalies
from graph.build import build_graph, memory_checkpointer
from graph.nodes.analyze import compress_context
from graph.nodes.llm_nodes import _ctx_analyze
from llm.models import clear_model_override, set_model_override
from llm.registry import render_prompt
from tests.nfr_harness import (
    CountingChatModel,
    FlakyChatModel,
    fmt_int,
    fmt_ms,
    load_and_aggregate,
    reduction_pct,
    standard_config,
    standard_plan,
    standard_thresholds,
    write_jtl,
)
from tests.nfr_results import NFR_JTL_ROWS, NFR_LLM_INPUT_TOKENS, record

pytestmark = pytest.mark.usefixtures("isolated_settings")


def _drive(graph, cfg, inp):
    for _mode, _chunk in graph.stream(inp, cfg, stream_mode=["updates"]):
        pass
    return graph.get_state(cfg)


# ---------- S1 컨텍스트 압축 ----------
@pytest.mark.nfr
def test_s1_compress_context_reduces_tokens(tmp_path, isolated_settings):
    jtl_path = write_jtl(tmp_path / "result.jtl", n_rows=NFR_JTL_ROWS, interval_ms=5)
    raw_bytes = jtl_path.stat().st_size
    raw_tokens = raw_bytes // 3

    df, buckets, summary, load_s, agg_s = load_and_aggregate(jtl_path)
    assert len(df) == NFR_JTL_ROWS

    run_dir = Path(isolated_settings.run_dir("s1-compress"))
    buckets_path = run_dir / "buckets.csv"
    buckets.to_csv(buckets_path, index=False)
    anomalies = detect_anomalies(buckets, None, bucket_sec=5, baseline_step=summary[0]["step"])

    t0 = time.perf_counter()
    compressed = compress_context({
        "run_id": "s1-compress",
        "bucket_series_path": str(buckets_path),
        "gc_series_path": None,
        "step_summary": summary,
        "anomalies": anomalies,
        "error_log_input": None,
    })
    compress_s = time.perf_counter() - t0
    state = {
        "llm_context": compressed["llm_context"],
        "thresholds": standard_thresholds(),
        "load_plan": standard_plan(),
        "parsed_config": standard_config(),
    }
    prompt = render_prompt("analyze_run", **_ctx_analyze(state))
    after_tokens = prompt.approx_tokens()

    assert raw_tokens > NFR_LLM_INPUT_TOKENS, "전제: 원본 JTL은 12k 토큰 가드를 초과해야 압축 효과가 드러난다"
    assert after_tokens <= NFR_LLM_INPUT_TOKENS
    assert after_tokens < raw_tokens / 100  # at least 99% smaller by the heuristic

    record(
        "scenario", "S1 컨텍스트 압축",
        metric="analyze_run 입력 토큰 (chars/3)",
        before=f"{fmt_int(raw_tokens)} tok ({fmt_int(raw_bytes)}B 원본 JTL {fmt_int(NFR_JTL_ROWS)}행)",
        after=f"{fmt_int(after_tokens)} tok (버킷 {len(buckets)}행 + Step 요약 {len(summary)}행)",
        delta=reduction_pct(raw_tokens, after_tokens),
        llm_calls="압축 후 1회 가능 (가드 내) / 원본 직송 시 가드 초과로 truncate",
        fallback="해당 없음 (결정론 압축)",
        latency=f"load {fmt_ms(load_s)} + agg {fmt_ms(agg_s)} + compress {fmt_ms(compress_s)}",
        interpretation=(
            f"원본 JTL을 LLM에 넣으면 약 {fmt_int(raw_tokens)} 토큰으로 12k 상한을 크게 넘습니다. "
            f"`compress_context` + `analyze_run` 프롬프트는 {fmt_int(after_tokens)} 토큰으로 "
            f"{reduction_pct(raw_tokens, after_tokens)}했고, 호출당 입력 가드({fmt_int(NFR_LLM_INPUT_TOKENS)})를 지킵니다."
        ),
        before_tokens=raw_tokens,
        after_tokens=after_tokens,
        n_rows=NFR_JTL_ROWS,
        n_buckets=len(buckets),
    )


# ---------- S2 LLM 추출 생략 ----------
@pytest.mark.nfr
def test_s2_high_confidence_skips_extract_llm(raw_inputs, isolated_settings):
    isolated_settings.llm_enabled = True
    isolated_settings.llm_model = "nfr-fake"

    naive = CountingChatModel()
    set_model_override(naive)
    try:
        graph = build_graph(memory_checkpointer())
        raw_missing = dict(raw_inputs)
        raw_missing["tomcat.env"] = "\n".join(
            line for line in raw_inputs["tomcat.env"].splitlines() if "dbcp" not in line
        )
        t0 = time.perf_counter()
        snap = _drive(graph, {"configurable": {"thread_id": "s2-before"}}, {
            "raw_inputs": raw_missing, "llm_enabled": True, "options": {"dry_run": True, "interpret": True},
        })
        before_s = time.perf_counter() - t0
    finally:
        clear_model_override()

    before_nodes = [c["node"] for c in snap.values.get("llm_calls", [])]
    before_count = naive.invoke_count
    assert "llm_extract_config" in before_nodes
    assert "llm_interpret_diagnosis" in before_nodes
    assert before_count == 2

    optimized = CountingChatModel()
    set_model_override(optimized)
    try:
        graph = build_graph(memory_checkpointer())
        t0 = time.perf_counter()
        snap2 = _drive(graph, {"configurable": {"thread_id": "s2-after"}}, {
            "raw_inputs": raw_inputs, "llm_enabled": True, "options": {"dry_run": True, "interpret": True},
        })
        after_s = time.perf_counter() - t0
    finally:
        clear_model_override()

    after_nodes = [c["node"] for c in snap2.values.get("llm_calls", [])]
    after_count = optimized.invoke_count
    assert "llm_extract_config" not in after_nodes
    assert after_nodes == ["llm_interpret_diagnosis"]
    assert after_count == 1
    assert snap2.values["parsed_config"].confidence >= 0.8
    assert snap2.values["thresholds"].t_db_lock == 50

    record(
        "scenario", "S2 고신뢰 정규식 시 추출 LLM 생략",
        metric="진단 구간 LLM 호출 횟수",
        before=f"{before_count}회 ({', '.join(before_nodes)}) · {fmt_ms(before_s)}",
        after=f"{after_count}회 ({', '.join(after_nodes)}) · {fmt_ms(after_s)}",
        delta=reduction_pct(before_count, after_count),
        llm_calls=f"{before_count} → {after_count}",
        fallback="추출 노드 자체를 라우터가 건너뜀 (llm_skipped 없음)",
        latency=f"전 {fmt_ms(before_s)} / 후 {fmt_ms(after_s)}",
        interpretation=(
            "필수 3필드가 정규식으로 채워지고 confidence≥0.8이면 `after_parse`가 "
            "`llm_extract_config`를 호출하지 않습니다. 같은 설정에서 dbcp 라인만 지운 "
            "대조군은 추출+해석 2회, 원본은 해석 1회입니다. 호출이 줄면 지연·비용이 함께 줄고 "
            "결정론 수치(T_db_lock=50)는 그대로입니다."
        ),
        before_calls=before_count,
        after_calls=after_count,
    )


# ---------- S3 Fallback ----------
@pytest.mark.nfr
def test_s3_llm_failure_falls_back_and_continues(raw_inputs, isolated_settings, monkeypatch):
    from graph.nodes import llm_nodes

    monkeypatch.setattr(llm_nodes, "LLM_BACKOFF_SEC", 0.0)
    isolated_settings.llm_enabled = True
    isolated_settings.llm_model = "nfr-flaky"
    flaky = FlakyChatModel()
    set_model_override(flaky)
    try:
        graph = build_graph(memory_checkpointer())
        t0 = time.perf_counter()
        snap = _drive(graph, {"configurable": {"thread_id": "s3-fallback"}}, {
            "raw_inputs": raw_inputs, "llm_enabled": True, "options": {"dry_run": True, "interpret": True},
        })
        elapsed = time.perf_counter() - t0
        # Continue past the first gate to prove the graph does not die on LLM failure.
        spec_input = {"kind": "url", "payload": "http://localhost:8080/api", "host": "localhost", "port": 8080}
        snap = _drive(graph, {"configurable": {"thread_id": "s3-fallback"}}, Command(resume={"scenario_input": spec_input}))
        gate = None
        for t in snap.tasks:
            if t.interrupts:
                gate = t.interrupts[0].value["kind"]
    finally:
        clear_model_override()

    v = snap.values
    skipped = v.get("llm_skipped") or []
    assert v["diagnosis_report"].source == "rules"
    assert "llm_interpret_diagnosis" in skipped
    assert any("3회 실패" in w for w in v.get("warnings", []))
    assert gate == "review_scenario"
    assert flaky.invoke_count == llm_nodes.LLM_MAX_ATTEMPTS
    assert v["thresholds"].t_drop == 300

    record(
        "scenario", "S3 LLM 장애 시 규칙 Fallback",
        metric="진단 완주 / 호출 결과",
        before="제공자 예외 시 그래프 중단 (미처리 예외)",
        after=f"규칙 리포트로 완주 · source={v['diagnosis_report'].source} · skipped={skipped}",
        delta="실패율 100%여도 사용자 게이트 도달",
        llm_calls=f"시도 {flaky.invoke_count}회, 성공 0회",
        fallback="100% (규칙 기반 DiagnosisReport)",
        latency=fmt_ms(elapsed),
        interpretation=(
            f"모델이 매 호출마다 실패해도 노드는 {llm_nodes.LLM_MAX_ATTEMPTS}회 재시도 후 "
            f"규칙 요약으로 내려가고 `review_scenario` 게이트까지 진행합니다 "
            f"(측정 {fmt_ms(elapsed)}, backoff=0). 임계치 계산은 LLM과 분리되어 T_drop=300이 유지됩니다."
        ),
        attempts=flaky.invoke_count,
        fallback_rate=1.0,
    )
