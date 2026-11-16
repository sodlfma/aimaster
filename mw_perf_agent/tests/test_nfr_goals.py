"""NFR goals from spec §8, each row: 목표 / 테스트 조건 / 측정값 / 결과 / 해석."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from langgraph.types import Command

from analysis.anomalies import detect_anomalies
from graph.build import build_graph, memory_checkpointer, sqlite_checkpointer
from graph.nodes.analyze import compress_context
from graph.nodes.llm_nodes import _ctx_analyze
from llm.invoke import invoke_llm
from llm.models import clear_model_override, set_model_override
from llm.registry import render_prompt
from tests.nfr_harness import (
    CountingChatModel,
    fmt_int,
    fmt_ms,
    load_and_aggregate,
    messages_text,
    standard_config,
    standard_plan,
    standard_thresholds,
    write_jtl,
)
from tests.nfr_results import (
    NFR_CHECKPOINT_MS,
    NFR_JTL_AGG_SEC,
    NFR_JTL_ROWS,
    NFR_LLM_INPUT_TOKENS,
    record,
)

pytestmark = pytest.mark.usefixtures("isolated_settings")


def _drive(graph, cfg, inp):
    for _mode, _chunk in graph.stream(inp, cfg, stream_mode=["updates"]):
        pass
    return graph.get_state(cfg)


@pytest.mark.nfr
def test_nfr_jtl_100k_aggregates_under_5s(tmp_path):
    path = write_jtl(tmp_path / "nfr.jtl", n_rows=NFR_JTL_ROWS, interval_ms=5)
    df, buckets, summary, load_s, agg_s = load_and_aggregate(path)
    total = load_s + agg_s
    passed = total < NFR_JTL_AGG_SEC
    record(
        "nfr", "jtl_aggregate",
        goal=f"JTL {fmt_int(NFR_JTL_ROWS)}행 집계 {NFR_JTL_AGG_SEC:.0f}초 이내",
        condition=f"합성 CSV JTL {fmt_int(NFR_JTL_ROWS)}행, pandas aggregate_buckets(5s) + step_summary",
        measured=f"load {fmt_ms(load_s)} + agg {fmt_ms(agg_s)} = {fmt_ms(total)} · 버킷 {len(buckets)} · Step {len(summary)}",
        passed=passed,
        interpretation="명세 §8 성능 항목. 원본 DataFrame은 상태에 넣지 않고 buckets.csv 경로만 남깁니다.",
    )
    assert len(df) == NFR_JTL_ROWS
    assert passed, f"집계 {total:.3f}s ≥ {NFR_JTL_AGG_SEC}s"


@pytest.mark.nfr
def test_nfr_llm_input_under_12k_tokens(tmp_path, isolated_settings):
    path = write_jtl(tmp_path / "nfr.jtl", n_rows=NFR_JTL_ROWS, interval_ms=5)
    df, buckets, summary, *_ = load_and_aggregate(path)
    run_dir = Path(isolated_settings.run_dir("nfr-12k"))
    bp = run_dir / "buckets.csv"
    buckets.to_csv(bp, index=False)
    anomalies = detect_anomalies(buckets, None, bucket_sec=5, baseline_step=summary[0]["step"])
    ctx = compress_context({
        "run_id": "nfr-12k",
        "bucket_series_path": str(bp),
        "step_summary": summary,
        "anomalies": anomalies,
    })["llm_context"]
    prompt = render_prompt("analyze_run", **_ctx_analyze({
        "llm_context": ctx,
        "thresholds": standard_thresholds(),
        "load_plan": standard_plan(),
        "parsed_config": standard_config(),
    }))
    tokens = prompt.approx_tokens()
    passed = tokens <= NFR_LLM_INPUT_TOKENS
    record(
        "nfr", "llm_input_tokens",
        goal=f"LLM 호출 1회당 입력 ≤ {fmt_int(NFR_LLM_INPUT_TOKENS)} 토큰",
        condition=f"{fmt_int(len(df))}행 JTL → compress_context → analyze_run.j2 렌더, 근사 len//3",
        measured=f"{fmt_int(tokens)} tok (system {fmt_int(len(prompt.system)//3)} + user {fmt_int(len(prompt.user)//3)})",
        passed=passed,
        interpretation="비용 가드. 초과 시 invoke_llm이 user 구간을 잘라 상한을 강제합니다.",
    )
    assert passed


@pytest.mark.nfr
def test_nfr_checkpoint_put_under_100ms(raw_inputs, no_llm, tmp_path):
    saver = sqlite_checkpointer(str(tmp_path / "ckpt.sqlite"))
    times_ms: list[float] = []
    orig = saver.put

    def timed_put(*args, **kwargs):
        t0 = time.perf_counter()
        out = orig(*args, **kwargs)
        times_ms.append((time.perf_counter() - t0) * 1000)
        return out

    saver.put = timed_put  # type: ignore[method-assign]
    graph = build_graph(saver)
    _drive(graph, {"configurable": {"thread_id": "nfr-ckpt"}}, {
        "raw_inputs": raw_inputs, "llm_enabled": False, "options": {"dry_run": True},
    })
    assert times_ms, "체크포인트 put이 한 번도 호출되지 않음"
    p50 = sorted(times_ms)[len(times_ms) // 2]
    pmax = max(times_ms)
    passed = p50 <= NFR_CHECKPOINT_MS
    record(
        "nfr", "checkpoint_put",
        goal=f"체크포인트 1회 저장 ≤ {NFR_CHECKPOINT_MS:.0f}ms (대용량 제외)",
        condition=f"SqliteSaver, 진단→review_plan (JTL 없음). put {len(times_ms)}회",
        measured=f"median {p50:.1f}ms / max {pmax:.1f}ms / n={len(times_ms)}",
        passed=passed,
        interpretation="상태에 DataFrame을 넣지 않는 설계가 저장 시간을 묶습니다. median을 통과 기준으로 씁니다.",
    )
    assert passed, f"checkpoint median {p50:.1f}ms > {NFR_CHECKPOINT_MS}ms"


@pytest.mark.nfr
def test_nfr_offline_closed_loop(raw_inputs, no_llm, isolated_settings):
    graph = build_graph(memory_checkpointer())
    cfg = {"configurable": {"thread_id": "nfr-offline"}}
    t0 = time.perf_counter()
    snap = _drive(graph, cfg, {"run_id": "nfr-off", "raw_inputs": raw_inputs, "llm_enabled": False,
                               "options": {"dry_run": True, "hold_sec": 5}})
    snap = _drive(graph, cfg, Command(resume={"scenario_input": {
        "kind": "url", "payload": "http://localhost:8080/api", "host": "localhost", "port": 8080}}))
    snap = _drive(graph, cfg, Command(resume={"scenario_spec": snap.values["scenario_spec"].model_dump()}))
    snap = _drive(graph, cfg, Command(resume={"run_mode": "local"}))
    elapsed = time.perf_counter() - t0
    v = snap.values
    passed = snap.next == () and v["analysis_report"].source == "rules" and "llm_analyze_run" in (v.get("llm_skipped") or [])
    record(
        "nfr", "offline_loop",
        goal="LLM 키 없이 규칙 Fallback으로 그래프 완주",
        condition="Fake JMeter + llm_enabled=False, 3개 게이트 resume",
        measured=f"END 도달={snap.next == ()} · source={v['analysis_report'].source} · skipped={v.get('llm_skipped')} · {fmt_ms(elapsed)}",
        passed=passed,
        interpretation="오프라인 동작 NFR. 분석 리포트는 규칙 요약이며 LLM 호출 기록은 비어 있습니다.",
    )
    assert passed
    assert not v.get("llm_calls")


@pytest.mark.nfr
def test_nfr_masking_and_target_and_secrets(raw_inputs, isolated_settings):
    isolated_settings.llm_enabled = True
    isolated_settings.llm_model = "nfr-fake"
    isolated_settings.llm_api_key = "sk-SECRET-NFR"
    isolated_settings.mask_enabled = True
    model = CountingChatModel()
    set_model_override(model)
    try:
        graph = build_graph(memory_checkpointer())
        cfg = {"configurable": {"thread_id": "nfr-sec"}}
        snap = _drive(graph, cfg, {"run_id": "nfr-sec", "raw_inputs": raw_inputs, "llm_enabled": True,
                                   "options": {"dry_run": True, "interpret": True}})
        body = messages_text(model.calls)
        hosts_leaked = any(h in body for h in ("was01.corp.local", "db01.corp.local"))
        dumped = json.dumps(snap.values, default=str)
        secrets_in_state = "sk-SECRET-NFR" in dumped
    finally:
        clear_model_override()

    passed = (not hosts_leaked) and (not secrets_in_state) and bool(model.calls)
    record(
        "nfr", "masking_secrets",
        goal="외부 LLM 전송 시 호스트 마스킹, 비밀값은 체크포인트에 없음",
        condition="mask_enabled=True, Fake 모델 invoke 메시지 + state JSON 검사",
        measured=f"calls={model.invoke_count} · host_leaked={hosts_leaked} · secret_in_state={secrets_in_state}",
        passed=passed,
        interpretation="Masker가 HOST_n 토큰으로 치환하고 Settings.llm_api_key는 그래프 상태에 들어가지 않습니다.",
    )
    assert passed


@pytest.mark.nfr
def test_nfr_allowlist_blocks_unknown_host(raw_inputs, no_llm):
    graph = build_graph(memory_checkpointer())
    cfg = {"configurable": {"thread_id": "nfr-allow"}}
    _drive(graph, cfg, {"raw_inputs": raw_inputs, "llm_enabled": False, "options": {"dry_run": False}})
    snap = _drive(graph, cfg, Command(resume={"scenario_input": {
        "kind": "url", "payload": "http://prod.example.com/api", "host": "prod.example.com", "port": 80}}))
    snap = _drive(graph, cfg, Command(resume={"scenario_spec": snap.values["scenario_spec"].model_dump()}))
    snap = _drive(graph, cfg, Command(resume={"run_mode": "local"}))
    blocked = [c for c in snap.values.get("preflight", []) if c.blocking and not c.ok]
    passed = bool(blocked) and snap.values.get("run_result") is None
    record(
        "nfr", "allowlist",
        goal="허용 목록에 없는 호스트로의 부하 실행 차단",
        condition="scenario host=prod.example.com, allowed=localhost/127.0.0.1/*.corp.local",
        measured=f"blocking failures={[(c.name, c.detail[:80]) for c in blocked]} · run_result={snap.values.get('run_result')}",
        passed=passed,
        interpretation="preflight allowed_host가 차단하면 after_preflight가 confirm_run으로 되돌립니다.",
    )
    assert passed


@pytest.mark.nfr
def test_nfr_invoke_truncates_over_budget(isolated_settings):
    """Hard cap in invoke_llm: oversized user payload is truncated rather than sent whole."""
    isolated_settings.llm_enabled = True
    isolated_settings.llm_model = "nfr-fake"
    isolated_settings.llm_max_context_tokens = 200  # tiny budget so truncation is forced
    model = CountingChatModel()
    set_model_override(model)
    try:
        huge = "was01.corp.local " + ("x" * 5000)
        out, rec = invoke_llm(
            "llm_extract_config", "extract_config", None,
            {"known": {}, "missing": ["dbcp_max_total"], "raw_text": huge},
        )
    finally:
        clear_model_override()
        isolated_settings.llm_max_context_tokens = 12000

    sent = messages_text(model.calls)
    passed = "...(truncated by context budget)" in sent and rec.ok
    record(
        "nfr", "token_hard_cap",
        goal="호출당 입력 토큰 상한 초과 시 전송본 truncate",
        condition="LLM_MAX_CONTEXT_TOKENS=200, extract_config raw_text 5k+ chars",
        measured=f"truncated_marker={passed} · approx_input_tokens={rec.approx_input_tokens} · sent_chars={len(sent)}",
        passed=passed,
        interpretation="compress 이후에도 프롬프트가 커지면 invoke_llm이 user 구간을 잘라 가드를 지킵니다.",
    )
    assert passed
    assert "was01.corp.local" not in sent  # still masked
