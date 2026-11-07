"""Main StateGraph assembly (spec 2.2 / appendix 10.0)."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

import llm.schemas as llm_schemas
import model.schemas as model_schemas
from .gates import gate_confirm_run, gate_review_plan, gate_review_scenario
from .nodes import analyze, build, diagnose, llm_nodes, run
from .routers import (
    after_confirm,
    after_parse,
    after_plan,
    after_preflight,
    after_run,
    after_validate,
    route_scenario_source,
)
from .state import PerfAgentState

# Transient I/O errors (file system, subprocess start) on deterministic nodes.
IO_RETRY = RetryPolicy(max_attempts=2, backoff_factor=1.5, retry_on=(OSError,))

GATE_NODES = ("gate_review_plan", "gate_review_scenario", "gate_confirm_run")
LLM_NODES = ("llm_extract_config", "llm_interpret_diagnosis", "llm_design_scenario", "llm_explain_error", "llm_analyze_run")


def _serde() -> JsonPlusSerializer:
    """Explicit allow-list of the Pydantic models stored in state (msgpack ext types)."""
    allowed = [("model.schemas", name) for name in model_schemas.__all__] + \
              [("llm.schemas", name) for name in llm_schemas.__all__] + \
              [("datetime", "datetime")]
    return JsonPlusSerializer(allowed_msgpack_modules=allowed)


def memory_checkpointer() -> BaseCheckpointSaver:
    return MemorySaver(serde=_serde())


def sqlite_checkpointer(path: str | Path) -> BaseCheckpointSaver:
    from langgraph.checkpoint.sqlite import SqliteSaver

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return SqliteSaver(conn, serde=_serde())


def build_graph(checkpointer: Optional[BaseCheckpointSaver] = None):
    g = StateGraph(PerfAgentState)

    # SC-001
    g.add_node("ingest_inputs", diagnose.ingest_inputs, retry_policy=IO_RETRY)
    g.add_node("parse_regex", diagnose.parse_regex)
    g.add_node("llm_extract_config", llm_nodes.llm_extract_config)
    g.add_node("merge_validate", diagnose.merge_validate)
    g.add_node("compute_thresholds", diagnose.compute_thresholds)
    g.add_node("build_load_plan", diagnose.build_load_plan)
    g.add_node("llm_interpret_diagnosis", llm_nodes.llm_interpret_diagnosis)
    g.add_node("gate_review_plan", gate_review_plan)
    # SC-002A
    g.add_node("scenario_from_url", build.scenario_from_url)
    g.add_node("llm_design_scenario", llm_nodes.llm_design_scenario)
    g.add_node("gate_review_scenario", gate_review_scenario)
    g.add_node("render_bundle", build.render_bundle, retry_policy=IO_RETRY)
    g.add_node("validate_jmx", build.validate_jmx)
    g.add_node("llm_explain_error", llm_nodes.llm_explain_error)
    g.add_node("gate_confirm_run", gate_confirm_run)
    # SC-002C
    g.add_node("preflight", run.preflight)
    g.add_node("run_jmeter", run.run_jmeter)
    g.add_node("collect_results", run.collect_results, retry_policy=IO_RETRY)
    # SC-002B
    g.add_node("aggregate_jtl", analyze.aggregate_jtl)
    g.add_node("parse_gc", analyze.parse_gc)
    g.add_node("detect_anomalies", analyze.detect_anomalies_node)
    g.add_node("compress_context", analyze.compress_context)
    g.add_node("llm_analyze_run", llm_nodes.llm_analyze_run)
    g.add_node("persist_report", analyze.persist_report, retry_policy=IO_RETRY)

    g.add_edge(START, "ingest_inputs")
    g.add_edge("ingest_inputs", "parse_regex")
    g.add_conditional_edges("parse_regex", after_parse)
    g.add_edge("llm_extract_config", "merge_validate")
    g.add_edge("merge_validate", "compute_thresholds")
    g.add_edge("compute_thresholds", "build_load_plan")
    g.add_conditional_edges("build_load_plan", after_plan)
    g.add_edge("llm_interpret_diagnosis", "gate_review_plan")
    g.add_conditional_edges("gate_review_plan", route_scenario_source)
    g.add_edge("scenario_from_url", "gate_review_scenario")
    g.add_edge("llm_design_scenario", "gate_review_scenario")
    g.add_edge("gate_review_scenario", "render_bundle")
    g.add_edge("render_bundle", "validate_jmx")
    g.add_conditional_edges("validate_jmx", after_validate)
    g.add_edge("llm_explain_error", "gate_review_scenario")
    g.add_conditional_edges("gate_confirm_run", after_confirm)
    g.add_conditional_edges("preflight", after_preflight)
    g.add_conditional_edges("run_jmeter", after_run)
    g.add_edge("collect_results", "aggregate_jtl")
    g.add_edge("collect_results", "parse_gc")  # parallel branch
    g.add_edge(["aggregate_jtl", "parse_gc"], "detect_anomalies")  # join
    g.add_edge("detect_anomalies", "compress_context")
    g.add_edge("compress_context", "llm_analyze_run")
    g.add_edge("llm_analyze_run", "persist_report")
    g.add_edge("persist_report", END)

    return g.compile(checkpointer=checkpointer or memory_checkpointer())


def mermaid(graph) -> str:
    return graph.get_graph().draw_mermaid()
