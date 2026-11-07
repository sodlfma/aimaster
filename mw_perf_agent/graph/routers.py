"""Conditional-edge functions (spec 2.2.3). Pure functions of state."""
from __future__ import annotations

from typing import Literal

from .state import PerfAgentState

CONFIDENCE_THRESHOLD = 0.8


def after_parse(state: PerfAgentState) -> Literal["llm_extract_config", "compute_thresholds"]:
    pc = state["parsed_config"]
    needs_llm = bool(pc.missing_required()) or pc.confidence < CONFIDENCE_THRESHOLD
    if needs_llm and state.get("llm_enabled"):
        return "llm_extract_config"
    return "compute_thresholds"


def after_plan(state: PerfAgentState) -> Literal["llm_interpret_diagnosis", "gate_review_plan"]:
    opts = state.get("options") or {}
    if state.get("llm_enabled") and opts.get("interpret", True):
        return "llm_interpret_diagnosis"
    return "gate_review_plan"


def route_scenario_source(state: PerfAgentState) -> Literal["scenario_from_url", "llm_design_scenario", "gate_review_scenario"]:
    kind = state["scenario_input"].kind
    if kind == "url":
        return "scenario_from_url"
    if state.get("llm_enabled"):
        return "llm_design_scenario"
    # No LLM: user fills an empty template at the review gate.
    return "gate_review_scenario"


def after_validate(state: PerfAgentState) -> Literal["gate_confirm_run", "llm_explain_error", "gate_review_scenario"]:
    if state["validation"].ok:
        return "gate_confirm_run"
    return "llm_explain_error" if state.get("llm_enabled") else "gate_review_scenario"


def after_confirm(state: PerfAgentState) -> Literal["preflight", "collect_results"]:
    return "collect_results" if state.get("run_mode") == "bundle_only" else "preflight"


def after_preflight(state: PerfAgentState) -> Literal["run_jmeter", "gate_confirm_run"]:
    blocked = any(c.blocking and not c.ok for c in state.get("preflight", []))
    return "gate_confirm_run" if blocked else "run_jmeter"


def after_run(state: PerfAgentState) -> Literal["collect_results", "gate_confirm_run"]:
    rr = state.get("run_result")
    if rr is None or rr.stopped:
        return "gate_confirm_run"
    return "collect_results"
