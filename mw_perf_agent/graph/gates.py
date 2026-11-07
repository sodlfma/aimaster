"""Human-in-the-loop gate nodes (spec 2.2.4). Gates only validate the resume
payload and write it to state; they never compute."""
from __future__ import annotations

from langgraph.types import interrupt

from model.schemas import LoadPlan, ParsedConfig, ScenarioInput, ScenarioSpec
from .state import PerfAgentState


def _dump(obj):
    return obj.model_dump() if hasattr(obj, "model_dump") else obj


def gate_review_plan(state: PerfAgentState) -> dict:
    payload = {
        "kind": "review_plan",
        "parsed_config": _dump(state.get("parsed_config")),
        "thresholds": _dump(state.get("thresholds")),
        "load_plan": _dump(state.get("load_plan")),
        "diagnosis_report": _dump(state.get("diagnosis_report")),
        "warnings": state.get("warnings", []),
    }
    edited = interrupt(payload)
    out: dict = {"node_log": ["gate_review_plan: resumed"]}
    if not isinstance(edited, dict):
        edited = {}
    if edited.get("load_plan"):
        plan = LoadPlan.model_validate(edited["load_plan"]).recompute_total()
        out["load_plan"] = plan
    if edited.get("parsed_config"):
        out["parsed_config"] = ParsedConfig.model_validate(edited["parsed_config"])
    if edited.get("scenario_input"):
        out["scenario_input"] = ScenarioInput.model_validate(edited["scenario_input"])
    elif "scenario_input" not in state:
        out["scenario_input"] = ScenarioInput(kind="url", payload="", host="localhost", port=8080)
    return out


def gate_review_scenario(state: PerfAgentState) -> dict:
    spec = state.get("scenario_spec")
    if spec is None:
        si = state.get("scenario_input")
        spec = ScenarioSpec(host=si.host if si else "localhost", port=si.port if si else 8080,
                            protocol=si.protocol if si else "http")
    payload = {
        "kind": "review_scenario",
        "scenario_spec": _dump(spec),
        "validation": _dump(state.get("validation")),
        "error_explanation": state.get("error_explanation"),
    }
    edited = interrupt(payload)
    out: dict = {"node_log": ["gate_review_scenario: resumed"], "error_explanation": None}
    if isinstance(edited, dict) and edited.get("scenario_spec"):
        out["scenario_spec"] = ScenarioSpec.model_validate(edited["scenario_spec"])
    else:
        out["scenario_spec"] = spec
    return out


def gate_confirm_run(state: PerfAgentState) -> dict:
    payload = {
        "kind": "confirm_run",
        "bundle": _dump(state.get("bundle")),
        "validation": _dump(state.get("validation")),
        "preflight": [_dump(c) for c in state.get("preflight", [])],
        "run_result": _dump(state.get("run_result")),
        "load_plan": _dump(state.get("load_plan")),
    }
    choice = interrupt(payload)
    if not isinstance(choice, dict):
        choice = {}
    mode = choice.get("run_mode", "local")
    if mode not in ("local", "docker", "remote", "bundle_only"):
        mode = "local"
    out: dict = {
        "run_mode": mode,
        "node_log": [f"gate_confirm_run: resumed mode={mode}"],
        "preflight": [],
        "run_result": None,
    }
    if choice.get("uploaded_jtl_path"):
        out["uploaded_jtl_path"] = choice["uploaded_jtl_path"]
    if "gc_input" in choice:
        out["gc_input"] = choice.get("gc_input")
    if "error_log_input" in choice:
        out["error_log_input"] = choice.get("error_log_input")
    if choice.get("overrides"):
        opts = dict(state.get("options") or {})
        opts["jmeter_overrides"] = choice["overrides"]
        out["options"] = opts
    return out
