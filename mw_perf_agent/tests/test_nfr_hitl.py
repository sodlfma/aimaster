"""HITL gate before/after evidence (mentor: 게이트 설계 전후를 숫자로).

H1 3게이트 — 사람 승인 전에는 JMeter가 기동하지 않는다.
H2 호스트 강제 — LLM이 제안한 임의 호스트는 review_scenario에 도달하기 전에 사용자 값으로 덮인다.
H3 플랜 편집 — review_plan에서 설정값을 바꾸면 임계치가 재계산된다.
H4 허용 목록 — 비허용 호스트는 preflight가 confirm_run으로 되돌리고 run_jmeter는 0회.
H5 검증 실패 루프 — 불량 JMX는 confirm_run에 도달하지 않고 review_scenario로 재진입한다.
H6 체크포인트 재개 — 프로세스 재생성 후에도 같은 게이트에서 이어진다.
"""
from __future__ import annotations

import time

import pytest
from langgraph.types import Command

from graph.build import build_graph, memory_checkpointer, sqlite_checkpointer
from llm.models import clear_model_override, set_model_override
from tests.nfr_harness import CountingChatModel, fmt_ms
from tests.nfr_results import record

pytestmark = pytest.mark.usefixtures("isolated_settings")


def _drive(graph, cfg, inp):
    nodes: list[str] = []
    for _mode, chunk in graph.stream(inp, cfg, stream_mode=["updates"]):
        if isinstance(chunk, dict):
            nodes.extend(chunk.keys())
    return graph.get_state(cfg), nodes


def _interrupt_kind(snap) -> str | None:
    for t in snap.tasks:
        if t.interrupts:
            return t.interrupts[0].value["kind"]
    return None


def _spy_run_jmeter(monkeypatch):
    from graph.nodes import run as run_mod

    calls = {"n": 0}
    orig = run_mod.run_jmeter

    def spy(state):
        calls["n"] += 1
        return orig(state)

    monkeypatch.setattr("graph.build.run.run_jmeter", spy)
    return calls


def _url_resume(host="localhost", port=8080, path="/api"):
    return {
        "kind": "url",
        "payload": f"http://{host}:{port}{path}",
        "host": host,
        "port": port,
    }


# ---------- H1 3게이트가 JMeter를 막음 ----------
@pytest.mark.nfr
def test_h1_gates_block_jmeter_until_confirm(raw_inputs, no_llm, monkeypatch):
    calls = _spy_run_jmeter(monkeypatch)
    graph = build_graph(memory_checkpointer())
    cfg = {"configurable": {"thread_id": "h1"}}
    kinds: list[str | None] = []
    jmeter_at_gate: list[int] = []
    latencies: list[str] = []

    t0 = time.perf_counter()
    snap, nodes0 = _drive(graph, cfg, {
        "run_id": "h1", "raw_inputs": raw_inputs, "llm_enabled": False,
        "options": {"dry_run": True, "hold_sec": 5},
    })
    latencies.append(fmt_ms(time.perf_counter() - t0))
    kinds.append(_interrupt_kind(snap))
    jmeter_at_gate.append(calls["n"])

    t1 = time.perf_counter()
    snap, _ = _drive(graph, cfg, Command(resume={"scenario_input": _url_resume()}))
    latencies.append(fmt_ms(time.perf_counter() - t1))
    kinds.append(_interrupt_kind(snap))
    jmeter_at_gate.append(calls["n"])

    t2 = time.perf_counter()
    snap, _ = _drive(graph, cfg, Command(resume={"scenario_spec": snap.values["scenario_spec"].model_dump()}))
    latencies.append(fmt_ms(time.perf_counter() - t2))
    kinds.append(_interrupt_kind(snap))
    jmeter_at_gate.append(calls["n"])

    t3 = time.perf_counter()
    snap, _ = _drive(graph, cfg, Command(resume={"run_mode": "local"}))
    confirm_s = time.perf_counter() - t3
    latencies.append(fmt_ms(confirm_s))
    kinds.append(_interrupt_kind(snap) or "END")
    jmeter_at_gate.append(calls["n"])

    assert kinds[:3] == ["review_plan", "review_scenario", "confirm_run"]
    assert jmeter_at_gate == [0, 0, 0, 1]
    assert snap.next == ()
    assert "run_jmeter" not in nodes0

    record(
        "hitl", "H1 실행 전 3게이트 강제 정지",
        metric="사람 승인 횟수별 JMeter 기동",
        before="사람 승인 0회에서 JMeter 기동 가능 (interrupt 없는 직선 실행)",
        after=(
            f"승인 0회={kinds[0]}·기동 {jmeter_at_gate[0]} / "
            f"1회={kinds[1]}·기동 {jmeter_at_gate[1]} / "
            f"2회={kinds[2]}·기동 {jmeter_at_gate[2]} / "
            f"3회={kinds[3]}·기동 {jmeter_at_gate[3]}"
        ),
        delta="무단 부하 실행 100% 차단 (기동은 3번째 승인 후에만 1회)",
        llm_calls="0 (규칙 경로)",
        fallback="해당 없음",
        latency=f"plan {latencies[0]} / scenario {latencies[1]} / confirm {latencies[2]} / run {latencies[3]}",
        interpretation=(
            f"`gate_review_plan` → `gate_review_scenario` → `gate_confirm_run` 순으로 "
            f"{len(kinds) - 1}회 멈춘 뒤에만 Fake JMeter가 기동했습니다. "
            f"첫 interrupt 이전 노드에 run_jmeter는 없고, 승인 0·1·2회에서 기동 횟수는 모두 0입니다."
        ),
    )


# ---------- H2 LLM 호스트 vs 사용자 강제 ----------
@pytest.mark.nfr
def test_h2_gate_overrides_llm_host(raw_inputs, isolated_settings):
    isolated_settings.llm_enabled = True
    isolated_settings.llm_model = "nfr-fake"
    model = CountingChatModel()
    set_model_override(model)
    try:
        graph = build_graph(memory_checkpointer())
        cfg = {"configurable": {"thread_id": "h2"}}
        t0 = time.perf_counter()
        _drive(graph, cfg, {
            "raw_inputs": raw_inputs, "llm_enabled": True,
            "options": {"dry_run": True, "interpret": False},
        })
        snap, _ = _drive(graph, cfg, Command(resume={"scenario_input": {
            "kind": "nl", "payload": "로그인 후 조회",
            "host": "localhost", "port": 8080, "protocol": "http",
        }}))
        elapsed = time.perf_counter() - t0
        spec = snap.values["scenario_spec"]
        llm_host = "attacker.example.com:1/https"
        gated = f"{spec.host}:{spec.port}/{spec.protocol}"
    finally:
        clear_model_override()

    assert _interrupt_kind(snap) == "review_scenario"
    assert gated == "localhost:8080/http"
    assert spec.host != "attacker.example.com"

    record(
        "hitl", "H2 LLM 임의 호스트 무력화",
        metric="시나리오 대상 호스트",
        before=llm_host,
        after=f"{gated} (review_scenario에서 사용자 값 확인)",
        delta="임의 호스트 100% 무력화 (1/1)",
        llm_calls=f"llm_design_scenario 1회 포함, invoke {model.invoke_count}회",
        fallback="호스트/포트/프로토콜은 LLM 출력을 쓰지 않고 scenario_input으로 덮음",
        latency=fmt_ms(elapsed),
        interpretation=(
            "CountingChatModel(및 실측 L4 gpt-5)는 시나리오 호스트로 attacker.example.com을 제안합니다. "
            f"`apply_user_target` 후 게이트에 도착한 값은 {gated} 이고, 사용자는 그 값을 보고 승인합니다."
        ),
    )


# ---------- H3 플랜 게이트 편집 ----------
@pytest.mark.nfr
def test_h3_review_plan_edit_recomputes_thresholds(raw_inputs, no_llm):
    graph = build_graph(memory_checkpointer())
    cfg = {"configurable": {"thread_id": "h3"}}
    snap, _ = _drive(graph, cfg, {
        "raw_inputs": raw_inputs, "llm_enabled": False, "options": {"dry_run": True},
    })
    before_thr = snap.values["thresholds"]
    before_conc = [s.target_concurrency for s in snap.values["load_plan"].steps]
    assert (before_thr.t_queue, before_thr.t_drop) == (200, 300)

    edited = snap.values["parsed_config"].model_copy(update={"tomcat_max_threads": 400})
    graph.update_state(cfg, {"parsed_config": edited}, as_node="merge_validate")
    t0 = time.perf_counter()
    snap, _ = _drive(graph, cfg, None)
    elapsed = time.perf_counter() - t0
    after_thr = snap.values["thresholds"]
    after_conc = [s.target_concurrency for s in snap.values["load_plan"].steps]

    assert _interrupt_kind(snap) == "review_plan"
    assert (after_thr.t_queue, after_thr.t_drop) == (400, 500)

    record(
        "hitl", "H3 플랜 게이트에서 설정 수정",
        metric="T_queue / T_drop / Step 동시 사용자",
        before=f"{before_thr.t_queue} / {before_thr.t_drop} / {before_conc}",
        after=f"{after_thr.t_queue} / {after_thr.t_drop} / {after_conc}",
        delta=(
            f"T_queue {before_thr.t_queue}→{after_thr.t_queue} "
            f"(+{(after_thr.t_queue / before_thr.t_queue - 1) * 100:.0f}%) · "
            f"T_drop {before_thr.t_drop}→{after_thr.t_drop} "
            f"(+{(after_thr.t_drop / before_thr.t_drop - 1) * 100:.0f}%)"
        ),
        llm_calls="0 (결정론 재계산)",
        fallback="게이트는 검증만, UI가 merge_validate부터 재실행",
        latency=fmt_ms(elapsed),
        interpretation=(
            "review_plan에서 tomcat_max_threads를 200→400으로 바꾸면 "
            f"T_queue {before_thr.t_queue}→{after_thr.t_queue}, "
            f"T_drop {before_thr.t_drop}→{after_thr.t_drop} 로 재계산되고 "
            "같은 게이트로 돌아와 사용자가 새 플랜을 다시 봅니다."
        ),
    )


# ---------- H4 허용 목록 차단 ----------
@pytest.mark.nfr
def test_h4_allowlist_returns_to_confirm_without_jmeter(raw_inputs, no_llm, monkeypatch):
    calls = _spy_run_jmeter(monkeypatch)
    graph = build_graph(memory_checkpointer())
    cfg = {"configurable": {"thread_id": "h4"}}
    _drive(graph, cfg, {
        "raw_inputs": raw_inputs, "llm_enabled": False, "options": {"dry_run": False},
    })
    snap, _ = _drive(graph, cfg, Command(resume={"scenario_input": _url_resume("prod.example.com", 80)}))
    snap, _ = _drive(graph, cfg, Command(resume={"scenario_spec": snap.values["scenario_spec"].model_dump()}))
    t0 = time.perf_counter()
    snap, _ = _drive(graph, cfg, Command(resume={"run_mode": "local"}))
    elapsed = time.perf_counter() - t0
    blocked = [c for c in snap.values.get("preflight", []) if c.blocking and not c.ok]

    assert _interrupt_kind(snap) == "confirm_run"
    assert calls["n"] == 0
    assert snap.values.get("run_result") is None
    assert blocked

    record(
        "hitl", "H4 비허용 호스트 실행 차단",
        metric="confirm_run 이후 JMeter 기동 / run_result",
        before="허용 목록 없이면 run_jmeter 진입 (기동 1회)",
        after=(
            f"confirm_run 복귀 · jmeter={calls['n']} · run_result={snap.values.get('run_result')} · "
            f"blocking={[(c.name, c.detail[:60]) for c in blocked]}"
        ),
        delta="차단율 100% (1/1) · 기동 0회",
        llm_calls="0",
        fallback="after_preflight가 차단 항목이면 gate_confirm_run으로 되돌림",
        latency=fmt_ms(elapsed),
        interpretation=(
            "prod.example.com 은 allowed_hosts에 없습니다. HITL confirm 후에도 "
            "preflight가 막고 같은 게이트로 돌아오므로 사용자는 호스트를 고치거나 실행을 포기합니다."
        ),
    )


# ---------- H5 검증 실패 → 시나리오 게이트 재진입 ----------
@pytest.mark.nfr
def test_h5_invalid_jmx_returns_to_review_scenario(raw_inputs, no_llm):
    graph = build_graph(memory_checkpointer())
    cfg = {"configurable": {"thread_id": "h5"}}
    _drive(graph, cfg, {
        "raw_inputs": raw_inputs, "llm_enabled": False, "options": {"dry_run": True},
    })
    snap, _ = _drive(graph, cfg, Command(resume={"scenario_input": _url_resume()}))
    good = snap.values["scenario_spec"].model_dump()
    bad = dict(good)
    bad["requests"] = []

    t0 = time.perf_counter()
    snap, _ = _drive(graph, cfg, Command(resume={"scenario_spec": bad}))
    fail_s = time.perf_counter() - t0
    fail_kind = _interrupt_kind(snap)
    validation = snap.values["validation"]

    t1 = time.perf_counter()
    snap, _ = _drive(graph, cfg, Command(resume={"scenario_spec": good}))
    fix_s = time.perf_counter() - t1
    ok_kind = _interrupt_kind(snap)

    assert fail_kind == "review_scenario"
    assert validation.ok is False
    assert ok_kind == "confirm_run"
    assert snap.values["validation"].ok is True

    record(
        "hitl", "H5 불량 JMX 재검토 루프",
        metric="검증 실패 시 다음 게이트 / 수정 후 게이트",
        before="validation.ok=False여도 실행 확인으로 진행 가능",
        after=f"실패→{fail_kind} (ok={validation.ok}) / 수정→{ok_kind} (ok={snap.values['validation'].ok})",
        delta="불량 번들로 confirm_run 도달 0건, 수정 후 1건",
        llm_calls="0 (llm_enabled=False, explain_error 생략)",
        fallback="after_validate가 실패 시 gate_review_scenario로 복귀",
        latency=f"실패 재진입 {fmt_ms(fail_s)} / 수정 후 {fmt_ms(fix_s)}",
        interpretation=(
            "요청이 비어 있는 ScenarioSpec은 HTTPSamplerProxy가 없어 JMX 구조 검증에 실패합니다. "
            "그래프는 confirm_run에 가지 않고 review_scenario로 돌아가고, "
            "같은 게이트에서 올바른 spec을 resume하면 그때 confirm_run에 도달합니다."
        ),
    )


# ---------- H6 체크포인트에서 게이트 재개 ----------
@pytest.mark.nfr
def test_h6_checkpoint_resumes_same_gate(raw_inputs, no_llm, tmp_path):
    db = tmp_path / "hitl.sqlite"
    cfg = {"configurable": {"thread_id": "h6"}}

    g1 = build_graph(sqlite_checkpointer(str(db)))
    t0 = time.perf_counter()
    snap, _ = _drive(g1, cfg, {
        "raw_inputs": raw_inputs, "llm_enabled": False, "options": {"dry_run": True},
    })
    diagnose_s = time.perf_counter() - t0
    conc = snap.values["load_plan"].steps[3].target_concurrency
    assert _interrupt_kind(snap) == "review_plan"

    t1 = time.perf_counter()
    g2 = build_graph(sqlite_checkpointer(str(db)))
    snap2 = g2.get_state(cfg)
    resume_s = time.perf_counter() - t1
    history = list(g2.get_state_history(cfg))

    assert _interrupt_kind(snap2) == "review_plan"
    assert snap2.values["load_plan"].steps[3].target_concurrency == conc
    assert len(history) > 3

    record(
        "hitl", "H6 재시작 후 같은 게이트 재개",
        metric="진단 재실행 여부 / 게이트·Step4 보존",
        before=f"프로세스 재생성 시 진단부터 다시 · {fmt_ms(diagnose_s)} (review_plan까지, 상태 소실)",
        after=(
            f"SqliteSaver get_state {fmt_ms(resume_s)} · 게이트={_interrupt_kind(snap2)} · "
            f"Step4 동시 사용자={conc} · history={len(history)}"
        ),
        delta="진단 재실행 0회 · 게이트와 Step4 수치 100% 보존",
        llm_calls="0",
        fallback="게이트 payload는 체크포인트에 있어 interrupt를 다시 걸 필요 없음",
        latency=f"진단 {fmt_ms(diagnose_s)} / 재개 {fmt_ms(resume_s)}",
        interpretation=(
            "오프라인 진단은 이미 수 ms라 재개 시간 단축은 노이즈입니다. HITL 성과는 "
            f"그래프를 다시 만들어도 review_plan에서 이어지고 Step4 동시 사용자 {conc} 이 "
            f"그대로인 점입니다 (history {len(history)}개). LLM 경로라면 이 재개가 호출을 생략합니다."
        ),
    )
