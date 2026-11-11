"""Domain models shared across graph nodes. All models must be JSON-serializable
so they can be stored by the LangGraph checkpointer."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

__all__ = [
    "TargetInfo", "ParsedConfig", "Thresholds", "LoadScenarioType", "LoadStep", "LoadPlan", "Extractor", "Assertion", "RequestSpec",
    "ScenarioSpec", "ScenarioInput", "BundlePaths", "ValidationResult", "PreflightCheck", "RunResult", "Anomaly",
]

REQUIRED_FIELDS = ("tomcat_max_threads", "tomcat_accept_count", "dbcp_max_total")

DEFAULTS = {
    "tomcat_max_threads": 200,
    "tomcat_accept_count": 100,
    "dbcp_max_total": 50,
}


class TargetInfo(BaseModel):
    host: str = ""
    apache_home: Optional[str] = None
    tomcat_home: Optional[str] = None
    instances: list[str] = Field(default_factory=list)


class ParsedConfig(BaseModel):
    # Apache (httpd-mpm.conf)
    mpm_module: Optional[Literal["prefork", "worker", "event"]] = None
    max_request_workers: Optional[int] = None
    threads_per_child: Optional[int] = None
    # Tomcat (shl/tomcat.env JAVA_OPTS first, server.xml as fallback)
    tomcat_max_threads: Optional[int] = None
    tomcat_accept_count: Optional[int] = None
    tomcat_port: Optional[int] = None
    jvm_xmx_mb: Optional[int] = None
    jvm_xms_mb: Optional[int] = None
    gc_type: Optional[str] = None
    # DB pool
    dbcp_max_total: Optional[int] = None
    dbcp_max_wait_ms: Optional[int] = None
    # Meta
    source: Literal["regex", "llm", "regex+llm", "default"] = "default"
    confidence: float = 0.0
    evidence: dict[str, str] = Field(default_factory=dict)

    def missing_required(self) -> list[str]:
        return [k for k in REQUIRED_FIELDS if getattr(self, k) is None]


class Thresholds(BaseModel):
    t_db_lock: int
    t_queue: int
    t_drop: int
    web_cap: Optional[int] = None
    formulas: dict[str, str] = Field(default_factory=dict)


LoadScenarioType = Literal["breakpoint", "spike", "sla"]


class LoadStep(BaseModel):
    name: str
    target_concurrency: int
    ramp_up_sec: int
    hold_sec: int
    expected_symptom: str = ""

    @property
    def duration_sec(self) -> int:
        return self.ramp_up_sec + self.hold_sec


class LoadPlan(BaseModel):
    steps: list[LoadStep]
    total_duration_sec: int = 0
    # Which SC-001 load shape produced these steps. Defaults keep older checkpoints valid.
    scenario_type: LoadScenarioType = "breakpoint"
    scenario_label: str = ""
    purpose: str = ""
    n_target: Optional[int] = None
    verification_points: list[str] = Field(default_factory=list)

    def recompute_total(self) -> "LoadPlan":
        self.total_duration_sec = sum(s.ramp_up_sec + s.hold_sec for s in self.steps)
        return self


class Extractor(BaseModel):
    name: str
    json_path: str
    default: str = "NOT_FOUND"


class Assertion(BaseModel):
    kind: Literal["response_code", "contains"] = "response_code"
    expected: str = "200"


class RequestSpec(BaseModel):
    label: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: str = "/"
    headers: dict[str, str] = Field(default_factory=dict)
    body: Optional[str] = None
    extract: list[Extractor] = Field(default_factory=list)
    assertions: list[Assertion] = Field(default_factory=list)


class ScenarioSpec(BaseModel):
    name: str = "MW Perf-Agent Scenario"
    protocol: Literal["http", "https"] = "http"
    host: str = "localhost"
    port: int = 8080
    default_headers: dict[str, str] = Field(default_factory=dict)
    requests: list[RequestSpec] = Field(default_factory=list)
    think_time_ms: int = 0


class ScenarioInput(BaseModel):
    kind: Literal["url", "curl", "har", "openapi", "nl"] = "url"
    payload: str = ""
    method: Optional[str] = None
    body: Optional[str] = None
    headers: dict[str, str] = Field(default_factory=dict)
    host: str = ""
    port: int = 80
    protocol: Literal["http", "https"] = "http"
    think_time_ms: int = 0


class BundlePaths(BaseModel):
    jmx: str
    properties: str
    run_sh: str
    plan_json: str
    readme: str
    zip: Optional[str] = None


class ValidationResult(BaseModel):
    ok: bool
    checks: list[dict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class PreflightCheck(BaseModel):
    name: str
    ok: bool
    detail: str = ""
    blocking: bool = True


class RunResult(BaseModel):
    run_id: str
    jmx_path: str
    jtl_path: str
    report_dir: Optional[str] = None
    log_path: Optional[str] = None
    exit_code: int = 0
    stopped: bool = False
    started_at: datetime
    finished_at: Optional[datetime] = None


class Anomaly(BaseModel):
    step: str
    start_ms: int
    end_ms: int
    kind: Literal["latency_spike", "error_rate", "full_gc", "http_503", "throughput_drop"]
    detail: str = ""
