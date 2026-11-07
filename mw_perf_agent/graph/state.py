"""LangGraph state schemas (spec 2.4)."""
from __future__ import annotations

import operator
from typing import Annotated, Literal, Optional, TypedDict

from langgraph.graph.message import add_messages

from llm.schemas import AnalysisReport, DiagnosisReport
from model.schemas import (
    Anomaly,
    BundlePaths,
    LoadPlan,
    ParsedConfig,
    PreflightCheck,
    RunResult,
    ScenarioInput,
    ScenarioSpec,
    TargetInfo,
    Thresholds,
    ValidationResult,
)

RunMode = Literal["local", "docker", "remote", "bundle_only"]


class PerfAgentState(TypedDict, total=False):
    # inputs / options
    run_id: str
    llm_enabled: bool
    options: dict
    raw_inputs: dict[str, str]
    target_info: TargetInfo
    # SC-001
    parsed_config: ParsedConfig
    parsed_config_llm: Optional[ParsedConfig]
    thresholds: Thresholds
    load_plan: LoadPlan
    diagnosis_report: Optional[DiagnosisReport]
    # SC-002A
    scenario_input: ScenarioInput
    scenario_spec: ScenarioSpec
    bundle: BundlePaths
    validation: ValidationResult
    error_explanation: Optional[str]
    # SC-002C
    run_mode: RunMode
    preflight: list[PreflightCheck]
    run_result: Optional[RunResult]
    uploaded_jtl_path: Optional[str]
    jtl_path: Optional[str]
    statistics: Optional[dict]
    gc_input: Optional[str]
    error_log_input: Optional[str]
    # SC-002B
    bucket_series_path: Optional[str]
    step_summary: list[dict]
    gc_series_path: Optional[str]
    anomalies: list[Anomaly]
    llm_context: dict
    analysis_report: Optional[AnalysisReport]
    report_path: Optional[str]
    # accumulating fields
    warnings: Annotated[list[str], operator.add]
    llm_skipped: Annotated[list[str], operator.add]
    llm_calls: Annotated[list[dict], operator.add]
    node_log: Annotated[list[str], operator.add]


class ChatState(TypedDict):
    messages: Annotated[list, add_messages]
