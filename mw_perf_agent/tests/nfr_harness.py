"""Shared builders for NFR / golden-set measurements (synthetic JTL, fake LLM)."""
from __future__ import annotations

import time
from pathlib import Path

from langchain_core.messages import AIMessage

from jmeter.jtl import load_jtl
from model.load_plan import generate_load_plan
from model.schemas import ParsedConfig, Thresholds
from model.thresholds import compute_thresholds

JTL_COLUMNS = [
    "timeStamp", "elapsed", "label", "responseCode", "responseMessage", "threadName",
    "dataType", "success", "failureMessage", "bytes", "sentBytes", "grpThreads",
    "allThreads", "URL", "Latency", "IdleTime", "Connect",
]
STEPS = [
    "Step 1 (Normal)",
    "Step 2 (DB Saturation)",
    "Step 3 (WAS Queueing)",
    "Step 4 (Stress/Drop)",
]


def standard_config() -> ParsedConfig:
    return ParsedConfig(
        tomcat_max_threads=200, tomcat_accept_count=100, dbcp_max_total=50,
        max_request_workers=400, jvm_xmx_mb=2048, source="regex", confidence=1.0,
    )


def standard_thresholds() -> Thresholds:
    return compute_thresholds(standard_config())


def standard_plan(hold_sec: int = 60):
    return generate_load_plan(standard_thresholds(), hold_sec=hold_sec)[0]


def write_jtl(
    path: Path,
    n_rows: int = 100_000,
    interval_ms: int = 5,
    *,
    step4_503_frac: float = 0.1,
    step2_err_frac: float = 0.0,
    step3_latency_ms: int = 900,
) -> Path:
    """Deterministic CSV JTL large enough for the 10만-행 NFR and compression tests."""
    n_rows = int(n_rows)
    quarter = max(1, n_rows // 4)
    t0 = 1_700_000_000_000
    rows: list[str] = []
    header = ",".join(JTL_COLUMNS)
    for i in range(n_rows):
        step_i = min(i // quarter, 3)
        step = STEPS[step_i]
        elapsed = 50 if step_i < 2 else (step3_latency_ms if step_i == 2 else 120)
        code, success = "200", "true"
        pos_in_step = i - step_i * quarter
        if step_i == 1 and step2_err_frac > 0 and pos_in_step < int(quarter * step2_err_frac):
            code, success = "500", "false"
        if step_i == 3 and pos_in_step >= int(quarter * (1.0 - step4_503_frac)):
            code, success, elapsed = "503", "false", 60
        active = (step_i + 1) * 40
        ts = t0 + i * interval_ms
        rows.append(
            f"{ts},{elapsed},GET /api,{code},OK,{step} 1-{i % 50 + 1},text,{success},,"
            f"120,80,{active},{active},http://was01.corp.local/api,{max(elapsed - 5, 1)},0,1"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def write_symptom_jtl(path: Path) -> Path:
    """600-row fixture: Step2 500s, Step3 latency spike, Step4 503s (quality golden G3)."""
    rows: list[str] = []
    t0 = 1_700_000_000_000
    for i in range(600):
        step = STEPS[i // 150]
        elapsed = 50 if i < 300 else (900 if i < 450 else 60)
        code, success = "200", "true"
        if 160 <= i < 200:
            code, success = "500", "false"
        if i >= 500:
            code, success = "503", "false"
        rows.append(
            f"{t0 + i * 100},{elapsed},GET /api,{code},OK,{step} 1-{i % 50 + 1},text,{success},,"
            f"120,80,{(i // 150 + 1) * 40},{(i // 150 + 1) * 40},http://h/api,{elapsed - 5},0,1"
        )
    path.write_text(",".join(JTL_COLUMNS) + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def load_and_aggregate(path: Path, bucket_sec: int = 5):
    from analysis.stats import aggregate_buckets, step_summary

    t0 = time.perf_counter()
    df = load_jtl(str(path))
    load_s = time.perf_counter() - t0
    t1 = time.perf_counter()
    buckets = aggregate_buckets(df, bucket_sec)
    summary = step_summary(df, standard_plan())
    agg_s = time.perf_counter() - t1
    return df, buckets, summary, load_s, agg_s


class CountingChatModel:
    """Structured-output fake that records every invoke (for call-count before/after)."""

    model_name = "nfr-fake"

    def __init__(self):
        self.calls: list = []
        self.invoke_count = 0

    def with_structured_output(self, schema):
        parent = self

        class _S:
            def invoke(_self, messages):
                parent.invoke_count += 1
                parent.calls.append((schema.__name__ if schema else None, messages))
                from llm.schemas import AnalysisReport, Bottleneck, DiagnosisReport, ExtractedConfig, ExtractedField
                from model.schemas import RequestSpec, ScenarioSpec

                if schema is ExtractedConfig:
                    return ExtractedConfig(fields=[
                        ExtractedField(field="dbcp_max_total", value="80", evidence="-Ddbcp.maxTotal=80"),
                    ])
                if schema is DiagnosisReport:
                    return DiagnosisReport(summary="LLM 요약", bottleneck_order=["db", "was"], source="llm")
                if schema is AnalysisReport:
                    return AnalysisReport(
                        headline="LLM 결론",
                        primary_bottleneck=Bottleneck(layer="was", confidence=0.9, evidence=["e"]),
                    )
                if schema is ScenarioSpec:
                    return ScenarioSpec(
                        name="evil", host="attacker.example.com", port=1, protocol="https",
                        requests=[RequestSpec(label="GET /login", method="GET", path="/login")],
                    )
                raise AssertionError(f"unexpected schema {schema}")

        return _S()

    def invoke(self, messages):
        self.invoke_count += 1
        self.calls.append((None, messages))
        return AIMessage(content="ok")

    def bind_tools(self, tools):
        return self


class FlakyChatModel(CountingChatModel):
    """Every structured-output call fails — exercises retry + rule fallback."""

    def with_structured_output(self, schema):
        self.invoke_count += 1
        raise ConnectionError("provider down")


def messages_text(calls) -> str:
    chunks: list[str] = []
    for _, msgs in calls:
        for m in msgs:
            chunks.append(getattr(m, "content", "") or "")
    return "\n".join(chunks)


def fmt_ms(seconds: float) -> str:
    ms = seconds * 1000
    if ms < 1:
        return f"{ms:.2f}ms"
    if ms < 1000:
        return f"{ms:.1f}ms"
    return f"{seconds:.3f}s"


def fmt_int(n: int) -> str:
    return f"{n:,}"


def reduction_pct(before: float, after: float) -> str:
    if before <= 0:
        return "n/a"
    return f"{(1 - after / before) * 100:.1f}% 감소"
