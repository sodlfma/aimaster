"""Structured-output schemas for LLM nodes."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from model.schemas import LoadPlan

__all__ = ["Finding", "Recommendation", "DiagnosisReport", "ExtractedField", "ExtractedConfig", "Bottleneck",
           "PredictionCompare", "AnalysisReport"]


class Finding(BaseModel):
    title: str = Field(description="짧은 제목")
    detail: str = Field(description="무엇이 왜 문제인지 1~3문장")
    severity: Literal["info", "warning", "critical"] = "warning"
    evidence: list[str] = Field(default_factory=list, description="근거가 되는 설정값/수치")


class Recommendation(BaseModel):
    component: Literal["jvm", "tomcat", "dbcp", "apache", "app", "infra", "test"] = "tomcat"
    parameter: str = Field(description="예: maxThreads, -Xmx, maxTotal")
    current: str = ""
    proposed: str = ""
    rationale: str = ""
    risk: str = ""


class DiagnosisReport(BaseModel):
    summary: str = Field(description="두괄식 요약 2~4문장")
    bottleneck_order: list[str] = Field(default_factory=list, description="부하 증가 시 병목이 나타나는 계층 순서")
    antipatterns: list[Finding] = Field(default_factory=list)
    checklist: list[str] = Field(default_factory=list, description="테스트 전 모니터링 체크리스트")
    recommendations: list[Recommendation] = Field(default_factory=list)
    rule_findings: list[str] = Field(default_factory=list)
    source: Literal["llm", "rules"] = "rules"


class ExtractedField(BaseModel):
    field: str
    value: str
    evidence: str = Field(description="원문에 실제로 존재하는 라인(그대로 복사)")


class ExtractedConfig(BaseModel):
    fields: list[ExtractedField] = Field(default_factory=list)
    notes: str = ""


class Bottleneck(BaseModel):
    layer: Literal["db", "was", "web", "jvm", "app", "none"] = "none"
    confidence: float = 0.0
    evidence: list[str] = Field(default_factory=list)


class PredictionCompare(BaseModel):
    step: str
    predicted: str
    actual: str
    verdict: Literal["match", "partial", "mismatch", "unknown"] = "unknown"


class AnalysisReport(BaseModel):
    headline: str = Field(description="두괄식 결론 1~2문장")
    primary_bottleneck: Bottleneck = Field(default_factory=Bottleneck)
    prediction_vs_actual: list[PredictionCompare] = Field(default_factory=list)
    correlations: list[str] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    next_plan: Optional[LoadPlan] = None
    source: Literal["llm", "rules"] = "rules"
