"""SC-001 deterministic nodes."""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from config.settings import get_settings
from model.load_plan import generate_load_plan
from model.schemas import ParsedConfig, TargetInfo
from model.thresholds import compute_thresholds as _compute, resolve_with_defaults
from parsers import find_evidence_line, parse_all
from ..state import PerfAgentState

_INT_FIELDS = {"tomcat_max_threads", "tomcat_accept_count", "tomcat_port", "dbcp_max_total", "dbcp_max_wait_ms",
               "jvm_xmx_mb", "jvm_xms_mb", "max_request_workers", "threads_per_child"}
_RANGES = {
    "tomcat_max_threads": (1, 10000), "tomcat_accept_count": (0, 100000), "tomcat_port": (1, 65535),
    "dbcp_max_total": (1, 10000), "dbcp_max_wait_ms": (-1, 3600000), "jvm_xmx_mb": (16, 1024 * 1024),
    "jvm_xms_mb": (1, 1024 * 1024), "max_request_workers": (1, 100000), "threads_per_child": (1, 10000),
}


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


def ingest_inputs(state: PerfAgentState) -> dict:
    run_id = state.get("run_id") or new_run_id()
    run_dir = get_settings().run_dir(run_id)
    raw = {k: (v or "") for k, v in (state.get("raw_inputs") or {}).items()}
    (run_dir / "inputs.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    target = state.get("target_info") or TargetInfo()
    return {"run_id": run_id, "raw_inputs": raw, "target_info": target, "node_log": [f"ingest_inputs: run_id={run_id}"]}


def parse_regex(state: PerfAgentState) -> dict:
    cfg, target, warnings = parse_all(state.get("raw_inputs") or {})
    existing = state.get("target_info") or TargetInfo()
    if isinstance(existing, dict):
        existing = TargetInfo.model_validate(existing)
    updates: dict = {}
    if not existing.host and target.host:
        updates["host"] = target.host
    if not existing.apache_home and target.apache_home:
        updates["apache_home"] = target.apache_home
    if not existing.tomcat_home and target.tomcat_home:
        updates["tomcat_home"] = target.tomcat_home
    if not existing.instances and target.instances:
        updates["instances"] = target.instances
    if not existing.private_ips and target.private_ips:
        updates["private_ips"] = target.private_ips
    if updates:
        existing = existing.model_copy(update=updates)
    return {
        "parsed_config": cfg,
        "target_info": existing,
        "warnings": warnings,
        "node_log": [f"parse_regex: confidence={cfg.confidence}, missing={cfg.missing_required()}"],
    }


def merge_validate(state: PerfAgentState) -> dict:
    base: ParsedConfig = state["parsed_config"]
    llm: ParsedConfig | None = state.get("parsed_config_llm")
    raw_text = "\n".join((state.get("raw_inputs") or {}).values())
    data = base.model_dump()
    warnings: list[str] = []
    merged_from_llm = 0
    if llm is not None:
        for field, val in llm.model_dump().items():
            if field in ("source", "confidence", "evidence") or val is None:
                continue
            if data.get(field) is not None:
                continue  # regex wins
            ev = llm.evidence.get(field) or find_evidence_line(raw_text, val)
            if not ev or str(val) not in raw_text:
                warnings.append(f"LLM이 제안한 {field}={val} 은(는) 원문에서 근거를 찾을 수 없어 폐기했습니다.")
                continue
            data[field] = val
            data["evidence"][field] = ev
            merged_from_llm += 1
    # range / logic validation
    for field, (lo, hi) in _RANGES.items():
        v = data.get(field)
        if v is None:
            continue
        if not (lo <= int(v) <= hi):
            warnings.append(f"{field}={v} 이(가) 허용 범위({lo}~{hi})를 벗어나 무시했습니다.")
            data[field] = None
            data["evidence"].pop(field, None)
    if data.get("jvm_xms_mb") and data.get("jvm_xmx_mb") and data["jvm_xms_mb"] > data["jvm_xmx_mb"]:
        warnings.append("Xms > Xmx 입니다. JVM 기동 실패 가능성이 있어 Xms 를 무시했습니다.")
        data["jvm_xms_mb"] = None
    if merged_from_llm:
        data["source"] = "regex+llm" if base.source == "regex" else "llm"
    cfg = ParsedConfig(**data)
    from parsers import compute_confidence
    cfg.confidence = compute_confidence(cfg, [])
    return {"parsed_config": cfg, "warnings": warnings, "node_log": [f"merge_validate: +{merged_from_llm} from llm, confidence={cfg.confidence}"]}


def compute_thresholds(state: PerfAgentState) -> dict:
    cfg = state["parsed_config"]
    resolved, warnings = resolve_with_defaults(cfg)
    thr = _compute(resolved)
    return {"parsed_config": resolved, "thresholds": thr, "warnings": warnings, "node_log": [f"compute_thresholds: {thr.t_db_lock}/{thr.t_queue}/{thr.t_drop}"]}


def build_load_plan(state: PerfAgentState) -> dict:
    opts = state.get("options") or {}
    s = get_settings()
    plan, warnings = generate_load_plan(
        state["thresholds"],
        ramp_scale=float(opts.get("ramp_scale", s.plan_ramp_scale)),
        hold_sec=int(opts.get("hold_sec", s.plan_hold_sec)),
    )
    # Rule-based diagnosis is always available; llm_interpret_diagnosis overrides it when it runs.
    from .llm_nodes import rules_diagnosis_report

    report = rules_diagnosis_report({**state, "load_plan": plan})
    return {
        "load_plan": plan,
        "diagnosis_report": report,
        "warnings": warnings,
        "node_log": [f"build_load_plan: {[st.target_concurrency for st in plan.steps]}"],
    }
