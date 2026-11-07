"""Graph-level tests: routers, interrupt gates, offline closed loop with the fake JMeter,
LLM nodes with an injected structured-output model, and checkpoint persistence."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langgraph.types import Command

from graph import routers
from graph.build import build_graph, memory_checkpointer, sqlite_checkpointer
from llm.models import clear_model_override, set_model_override
from llm.schemas import AnalysisReport, Bottleneck, DiagnosisReport, ExtractedConfig, ExtractedField
from model.schemas import ParsedConfig, PreflightCheck, RunResult, ScenarioInput, ValidationResult

pytestmark = pytest.mark.usefixtures("isolated_settings")


# ---------------- routers ----------------
def test_routers_offline_paths():
    full = ParsedConfig(tomcat_max_threads=1, tomcat_accept_count=1, dbcp_max_total=1, confidence=1.0)
    partial = ParsedConfig(tomcat_max_threads=1, confidence=0.3)
    assert routers.after_parse({"parsed_config": full, "llm_enabled": True}) == "compute_thresholds"
    assert routers.after_parse({"parsed_config": partial, "llm_enabled": True}) == "llm_extract_config"
    assert routers.after_parse({"parsed_config": partial, "llm_enabled": False}) == "compute_thresholds"
    assert routers.after_plan({"llm_enabled": True, "options": {"interpret": False}}) == "gate_review_plan"
    assert routers.after_plan({"llm_enabled": True}) == "llm_interpret_diagnosis"
    assert routers.route_scenario_source({"scenario_input": ScenarioInput(kind="url"), "llm_enabled": False}) == "scenario_from_url"
    assert routers.route_scenario_source({"scenario_input": ScenarioInput(kind="curl"), "llm_enabled": True}) == "llm_design_scenario"
    assert routers.route_scenario_source({"scenario_input": ScenarioInput(kind="nl"), "llm_enabled": False}) == "gate_review_scenario"
    assert routers.after_validate({"validation": ValidationResult(ok=False), "llm_enabled": True}) == "llm_explain_error"
    assert routers.after_validate({"validation": ValidationResult(ok=False), "llm_enabled": False}) == "gate_review_scenario"
    assert routers.after_confirm({"run_mode": "bundle_only"}) == "collect_results"
    assert routers.after_preflight({"preflight": [PreflightCheck(name="x", ok=False, blocking=True)]}) == "gate_confirm_run"
    assert routers.after_preflight({"preflight": [PreflightCheck(name="x", ok=False, blocking=False)]}) == "run_jmeter"
    rr = RunResult(run_id="r", jmx_path="a", jtl_path="b", stopped=True, started_at="2024-01-01T00:00:00")
    assert routers.after_run({"run_result": rr}) == "gate_confirm_run"


# ---------------- helpers ----------------
def _drive(graph, cfg, inp):
    custom = []
    for mode, chunk in graph.stream(inp, cfg, stream_mode=["updates", "custom"]):
        if mode == "custom":
            custom.append(chunk)
    return graph.get_state(cfg), custom


def _interrupt_kind(snap):
    for t in snap.tasks:
        if t.interrupts:
            return t.interrupts[0].value["kind"]
    return None


# ---------------- offline closed loop ----------------
def test_full_offline_loop_with_fake_jmeter(raw_inputs, no_llm, isolated_settings):
    graph = build_graph(memory_checkpointer())
    cfg = {"configurable": {"thread_id": "t-e2e"}}

    snap, _ = _drive(graph, cfg, {"run_id": "e2e", "raw_inputs": raw_inputs, "llm_enabled": False,
                                  "options": {"dry_run": True, "hold_sec": 5}})
    assert _interrupt_kind(snap) == "review_plan"
    v = snap.values
    assert v["thresholds"].t_drop == 300 and len(v["load_plan"].steps) == 4
    assert v["diagnosis_report"].source == "rules"
    assert "llm_interpret_diagnosis" not in v.get("llm_skipped", [])  # router skipped the node entirely

    snap, _ = _drive(graph, cfg, Command(resume={"scenario_input": {
        "kind": "url", "payload": "http://localhost:8080/api/v1/test", "method": "POST", "body": '{"a":1}', "host": "localhost", "port": 8080}}))
    assert _interrupt_kind(snap) == "review_scenario"
    spec = snap.values["scenario_spec"]
    assert spec.host == "localhost" and spec.requests[0].method == "POST"

    snap, _ = _drive(graph, cfg, Command(resume={"scenario_spec": spec.model_dump()}))
    assert _interrupt_kind(snap) == "confirm_run"
    v = snap.values
    assert v["validation"].ok, v["validation"].errors
    assert all(Path(p).exists() for p in (v["bundle"].jmx, v["bundle"].properties, v["bundle"].run_sh, v["bundle"].zip))
    assert v["bundle"].jmx.startswith(isolated_settings.runs_dir)

    snap, custom = _drive(graph, cfg, Command(resume={"run_mode": "local", "gc_input": None}))
    assert snap.next == ()  # reached END
    v = snap.values
    assert all(c.ok for c in v["preflight"] if c.blocking), [(c.name, c.detail) for c in v["preflight"]]
    assert v["run_result"].exit_code == 0 and not v["run_result"].stopped
    assert any(e.get("event") == "summary" for e in custom), "summariser progress should be streamed on the custom channel"
    assert Path(v["jtl_path"]).exists()
    steps = [s["step"] for s in v["step_summary"]]
    assert len(steps) == 4 and steps[0].startswith("Step 1")
    kinds = {a.kind for a in v["anomalies"]}
    assert {"error_rate", "latency_spike", "http_503"} <= kinds
    rep = v["analysis_report"]
    assert rep.source == "rules" and rep.primary_bottleneck.layer != "none"
    assert rep.next_plan is not None and len(rep.next_plan.steps) == 4
    report = Path(v["report_path"]).read_text(encoding="utf-8")
    assert "Step" in report and rep.headline in report
    run_dir = Path(isolated_settings.runs_dir) / "e2e"
    assert (run_dir / "state.json").exists() and (run_dir / "buckets.csv").exists()
    assert "llm_analyze_run" in v["llm_skipped"]

    # Chat sub-graph: read-only tools over runs/<run_id>/ plus a model that answers after one tool call.
    from graph.chat_agent import build_chat_agent, chat_config
    from langchain_core.messages import HumanMessage
    from tools.run_artifacts import RunArtifacts

    tools = {t.name: t for t in RunArtifacts("e2e").tools()}
    assert "Step 4" in tools["get_bucket_series"].invoke({"step": "Step 4 (Stress/Drop)", "metric": "http_503"})
    assert "503" in tools["get_errors_sample"].invoke({"response_code": "503", "n": 2})
    assert json.loads(tools["get_config_value"].invoke({"field": "tomcat_max_threads"}))["value"] == 200

    class ToolCallingModel:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            if any(getattr(m, "type", "") == "tool" for m in messages):
                return AIMessage(content="Step 4의 503은 accept 큐 포화입니다.")
            return AIMessage(content="", tool_calls=[{"name": "get_bucket_series", "args": {"step": "Step 4 (Stress/Drop)", "metric": "http_503"}, "id": "c1"}])

    chat = build_chat_agent("e2e", checkpointer=memory_checkpointer(), model=ToolCallingModel())
    out = chat.invoke({"messages": [HumanMessage(content="Step 4 503 원인?")]}, chat_config("e2e"))
    assert out["messages"][-1].content.startswith("Step 4의 503")
    assert any(getattr(m, "type", "") == "tool" for m in out["messages"])

    # Re-plan from the analysis (Tab4 "제안 플랜 반영"): update as build_load_plan → back to review gate.
    graph.update_state(cfg, {"load_plan": rep.next_plan}, as_node="build_load_plan")
    snap, _ = _drive(graph, cfg, None)
    assert _interrupt_kind(snap) == "review_plan"
    assert snap.values["load_plan"].steps[0].target_concurrency == rep.next_plan.steps[0].target_concurrency


def test_review_plan_edit_recomputes_thresholds(raw_inputs, no_llm):
    graph = build_graph(memory_checkpointer())
    cfg = {"configurable": {"thread_id": "t-edit"}}
    snap, _ = _drive(graph, cfg, {"raw_inputs": raw_inputs, "llm_enabled": False, "options": {"dry_run": True}})
    assert snap.values["thresholds"].t_queue == 200
    edited = snap.values["parsed_config"].model_copy(update={"tomcat_max_threads": 400})
    graph.update_state(cfg, {"parsed_config": edited}, as_node="merge_validate")
    snap, _ = _drive(graph, cfg, None)
    assert _interrupt_kind(snap) == "review_plan"
    assert snap.values["thresholds"].t_queue == 400 and snap.values["thresholds"].t_drop == 500


def test_bundle_only_mode_analyzes_uploaded_jtl(raw_inputs, no_llm, isolated_settings, tmp_path):
    graph = build_graph(memory_checkpointer())
    cfg = {"configurable": {"thread_id": "t-upload"}}
    _drive(graph, cfg, {"run_id": "up", "raw_inputs": raw_inputs, "llm_enabled": False, "options": {"dry_run": False}})
    snap, _ = _drive(graph, cfg, Command(resume={"scenario_input": {"kind": "url", "payload": "http://localhost:8080/x"}}))
    snap, _ = _drive(graph, cfg, Command(resume={"scenario_spec": snap.values["scenario_spec"].model_dump()}))
    assert _interrupt_kind(snap) == "confirm_run"

    # Produce a JTL "externally" with the fake runner, then upload it.
    import subprocess, sys
    ext = tmp_path / "ext"
    ext.mkdir()
    subprocess.run([sys.executable, str(isolated_settings.jmeter_bin), "-n", "-t", snap.values["bundle"].jmx, "-l", str(ext / "r.jtl"),
                    "-q", snap.values["bundle"].properties], check=True, capture_output=True)
    snap, _ = _drive(graph, cfg, Command(resume={"run_mode": "bundle_only", "uploaded_jtl_path": str(ext / "r.jtl")}))
    assert snap.next == ()
    assert snap.values.get("run_result") is None
    assert snap.values["jtl_path"] == str(Path(isolated_settings.runs_dir) / "up" / "result.jtl")  # copied into the run dir
    assert snap.values["analysis_report"].headline


def test_preflight_blocks_disallowed_host(raw_inputs, no_llm):
    graph = build_graph(memory_checkpointer())
    cfg = {"configurable": {"thread_id": "t-block"}}
    _drive(graph, cfg, {"raw_inputs": raw_inputs, "llm_enabled": False, "options": {"dry_run": False}})
    snap, _ = _drive(graph, cfg, Command(resume={"scenario_input": {"kind": "url", "payload": "http://prod.example.com/api"}}))
    snap, _ = _drive(graph, cfg, Command(resume={"scenario_spec": snap.values["scenario_spec"].model_dump()}))
    snap, _ = _drive(graph, cfg, Command(resume={"run_mode": "local"}))
    assert _interrupt_kind(snap) == "confirm_run"  # bounced back by after_preflight
    blocked = [c for c in snap.values["preflight"] if c.blocking and not c.ok]
    assert blocked and "allowed" in blocked[0].name.lower() or "host" in blocked[0].name.lower()
    assert snap.values.get("run_result") is None


def test_preflight_allows_diagnosed_host(raw_inputs, no_llm, monkeypatch):
    from model.schemas import TargetInfo

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("tools.preflight.socket.create_connection", lambda *a, **k: _Conn())
    monkeypatch.setattr("tools.preflight.http_status", lambda *a, **k: (200, '{"ok":true}'))
    graph = build_graph(memory_checkpointer())
    cfg = {"configurable": {"thread_id": "t-diag-allow"}}
    _drive(graph, cfg, {
        "raw_inputs": raw_inputs, "llm_enabled": False, "options": {"dry_run": False},
        "target_info": TargetInfo(host="ec2-user@3.35.11.139"),
    })
    snap, _ = _drive(graph, cfg, Command(resume={"scenario_input": {"kind": "url", "payload": "http://3.35.11.139:8080/api"}}))
    snap, _ = _drive(graph, cfg, Command(resume={"scenario_spec": snap.values["scenario_spec"].model_dump()}))
    snap, _ = _drive(graph, cfg, Command(resume={"run_mode": "local"}))
    assert snap.next == ()
    allowed = [c for c in snap.values["preflight"] if c.name == "allowed_host"]
    assert allowed and allowed[0].ok
    assert snap.values.get("run_result") is not None


def test_sqlite_checkpointer_survives_graph_rebuild(raw_inputs, no_llm, tmp_path):
    db = tmp_path / "ckpt.sqlite"
    cfg = {"configurable": {"thread_id": "t-persist"}}
    g1 = build_graph(sqlite_checkpointer(str(db)))
    _drive(g1, cfg, {"raw_inputs": raw_inputs, "llm_enabled": False, "options": {"dry_run": False}})
    g2 = build_graph(sqlite_checkpointer(str(db)))
    snap = g2.get_state(cfg)
    assert _interrupt_kind(snap) == "review_plan"
    assert snap.values["load_plan"].steps[3].target_concurrency == 360
    assert len(list(g2.get_state_history(cfg))) > 3


# ---------------- LLM nodes with a fake structured-output model ----------------
class _Structured:
    def __init__(self, schema, calls):
        self.schema, self.calls = schema, calls

    def invoke(self, messages):
        self.calls.append((self.schema.__name__ if self.schema else None, messages))
        if self.schema is ExtractedConfig:
            return ExtractedConfig(fields=[ExtractedField(field="dbcp_max_total", value="80", evidence="-Ddbcp.maxTotal=80"),
                                           ExtractedField(field="source", value="hack", evidence="x")])
        if self.schema is DiagnosisReport:
            return DiagnosisReport(summary="LLM 요약", bottleneck_order=["db", "was"], source="rules")
        if self.schema is AnalysisReport:
            return AnalysisReport(headline="LLM 결론", primary_bottleneck=Bottleneck(layer="db", confidence=0.9, evidence=["e"]))
        raise AssertionError(f"unexpected schema {self.schema}")


class FakeChatModel:
    model_name = "fake"

    def __init__(self):
        self.calls: list = []

    def with_structured_output(self, schema):
        return _Structured(schema, self.calls)

    def invoke(self, messages):
        self.calls.append((None, messages))
        return AIMessage(content="JMX 오류 설명")

    def bind_tools(self, tools):
        return self


class FlakyModel(FakeChatModel):
    def with_structured_output(self, schema):
        raise ConnectionError("provider down")


@pytest.fixture
def fake_model():
    m = FakeChatModel()
    set_model_override(m)
    yield m
    clear_model_override()


def test_llm_nodes_run_and_are_masked(fake_model, raw_inputs, isolated_settings):
    # Drop dbcp from the raw input so the regex parser needs LLM-01.
    raw = dict(raw_inputs)
    raw["tomcat.env"] = "\n".join(l for l in raw["tomcat.env"].splitlines() if "dbcp" not in l)
    isolated_settings.llm_enabled = True
    isolated_settings.llm_model = "fake:model"
    graph = build_graph(memory_checkpointer())
    cfg = {"configurable": {"thread_id": "t-llm"}}
    snap, _ = _drive(graph, cfg, {"raw_inputs": raw, "llm_enabled": True, "options": {"dry_run": False}})
    v = snap.values
    assert _interrupt_kind(snap) == "review_plan"
    assert v["parsed_config_llm"].dbcp_max_total == 80
    assert v["parsed_config"].dbcp_max_total == 80 and v["parsed_config"].source == "regex+llm"
    assert v["thresholds"].t_db_lock == 80
    assert v["diagnosis_report"].summary == "LLM 요약" and v["diagnosis_report"].source == "llm"
    assert [c["node"] for c in v["llm_calls"]] == ["llm_extract_config", "llm_interpret_diagnosis"]
    assert all(c["prompt_version"] for c in v["llm_calls"])
    # Host masking: the real hostname from web_mon_cron.env must never reach the model.
    for _, msgs in fake_model.calls:
        for m in msgs:
            assert "was01.corp.local" not in m.content and "db01.corp.local" not in m.content


def test_llm_failure_falls_back_to_rules(raw_inputs, isolated_settings, monkeypatch):
    from graph.nodes import llm_nodes
    monkeypatch.setattr(llm_nodes, "LLM_BACKOFF_SEC", 0.0)
    set_model_override(FlakyModel())
    try:
        isolated_settings.llm_enabled = True
        graph = build_graph(memory_checkpointer())
        cfg = {"configurable": {"thread_id": "t-flaky"}}
        snap, _ = _drive(graph, cfg, {"raw_inputs": raw_inputs, "llm_enabled": True, "options": {"dry_run": False}})
        v = snap.values
        assert _interrupt_kind(snap) == "review_plan"
        assert v["diagnosis_report"].source == "rules"
        assert "llm_interpret_diagnosis" in v["llm_skipped"]
        assert any("3회 실패" in w for w in v["warnings"])
    finally:
        clear_model_override()


def test_llm_scenario_design_cannot_change_target(fake_model, raw_inputs, isolated_settings):
    from model.schemas import RequestSpec, ScenarioSpec

    class DesignModel(FakeChatModel):
        def with_structured_output(self, schema):
            if schema is ScenarioSpec:
                class _S:
                    def invoke(_self, messages):
                        return ScenarioSpec(name="evil", host="attacker.example.com", port=1, protocol="https",
                                            requests=[RequestSpec(label="GET /login", method="GET", path="/login")])
                return _S()
            return super().with_structured_output(schema)

    set_model_override(DesignModel())
    isolated_settings.llm_enabled = True
    graph = build_graph(memory_checkpointer())
    cfg = {"configurable": {"thread_id": "t-design"}}
    _drive(graph, cfg, {"raw_inputs": raw_inputs, "llm_enabled": True, "options": {"dry_run": False, "interpret": False}})
    snap, _ = _drive(graph, cfg, Command(resume={"scenario_input": {"kind": "nl", "payload": "로그인 후 조회", "host": "localhost", "port": 8080, "protocol": "http"}}))
    assert _interrupt_kind(snap) == "review_scenario"
    spec = snap.values["scenario_spec"]
    assert (spec.host, spec.port, spec.protocol) == ("localhost", 8080, "http")
    assert spec.requests[0].path == "/login"


def test_state_json_has_no_secrets(raw_inputs, no_llm, isolated_settings):
    isolated_settings.llm_api_key = "sk-SECRET-123"
    graph = build_graph(memory_checkpointer())
    cfg = {"configurable": {"thread_id": "t-secret"}}
    _drive(graph, cfg, {"run_id": "sec", "raw_inputs": raw_inputs, "llm_enabled": False, "options": {"dry_run": False}})
    snap, _ = _drive(graph, cfg, Command(resume={"scenario_input": {"kind": "url", "payload": "http://localhost:8080/"}}))
    snap, _ = _drive(graph, cfg, Command(resume={"scenario_spec": snap.values["scenario_spec"].model_dump()}))
    snap, _ = _drive(graph, cfg, Command(resume={"run_mode": "local"}))
    dumped = json.dumps(snap.values, default=str)
    assert "sk-SECRET-123" not in dumped
    assert "sk-SECRET-123" not in (Path(isolated_settings.runs_dir) / "sec" / "state.json").read_text(encoding="utf-8")
