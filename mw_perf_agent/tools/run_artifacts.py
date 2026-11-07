"""Read-only LangChain tools for the chat sub-graph. Every tool is bound to one
run directory and refuses to read outside of it."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pandas as pd
from langchain_core.tools import tool

from config.settings import get_settings


class RunArtifacts:
    def __init__(self, run_id: str, runs_dir: Optional[str] = None):
        base = Path(runs_dir or get_settings().runs_dir).resolve()
        self.run_dir = (base / run_id).resolve()
        if base not in self.run_dir.parents:
            raise ValueError("invalid run_id")
        self.run_id = run_id

    def _path(self, name: str) -> Path:
        p = (self.run_dir / name).resolve()
        if self.run_dir not in p.parents and p != self.run_dir:
            raise ValueError("path escapes run directory")
        return p

    def _csv(self, name: str) -> pd.DataFrame:
        p = self._path(name)
        if not p.exists():
            return pd.DataFrame()
        return pd.read_csv(p)

    def _json(self, name: str) -> dict:
        p = self._path(name)
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))

    def tools(self) -> list:
        art = self

        @tool
        def get_bucket_series(step: Optional[str] = None, metric: str = "p99_ms", max_rows: int = 60) -> str:
            """시간 버킷(5초) 시계열을 조회합니다. metric: tps|avg_ms|p95_ms|p99_ms|err_rate|active|http_503. step 을 주면 해당 Step만."""
            df = art._csv("buckets.csv")
            if df.empty:
                return "no bucket data"
            if step:
                df = df[df["step"].astype(str).str.contains(step, regex=False)]
            if metric not in df.columns:
                return f"unknown metric {metric}; columns={list(df.columns)}"
            if len(df) > max_rows:
                df = df.iloc[:: -(-len(df) // max_rows)]
            return df[["bucket_start_ms", "step", metric]].to_csv(index=False)

        @tool
        def get_gc_events(kind: str = "full", max_rows: int = 60) -> str:
            """GC 이벤트를 조회합니다. kind: full|young|sample|all."""
            df = art._csv("gc.csv")
            if df.empty:
                return "no gc data"
            if kind != "all":
                df = df[df["kind"] == kind]
            return df.head(max_rows).to_csv(index=False)

        @tool
        def get_errors_sample(step: Optional[str] = None, response_code: Optional[str] = None, n: int = 5) -> str:
            """JTL에서 실패 샘플(응답코드, 라벨, 시각, 메시지)을 최대 n개 조회합니다. 응답 본문은 포함하지 않습니다."""
            p = art._path("result.jtl")
            if not p.exists():
                return "no jtl"
            df = pd.read_csv(p, low_memory=False)
            df = df[df["success"].astype(str).str.lower() != "true"]
            if step:
                df = df[df["threadName"].astype(str).str.startswith(step)]
            if response_code:
                df = df[df["responseCode"].astype(str) == str(response_code)]
            cols = [c for c in ["timeStamp", "label", "responseCode", "responseMessage", "failureMessage", "elapsed", "threadName"] if c in df.columns]
            return df[cols].head(n).to_csv(index=False) if not df.empty else "no matching errors"

        @tool
        def get_config_value(field: str) -> str:
            """ParsedConfig 의 값과 근거 라인을 조회합니다. field 예: tomcat_max_threads, dbcp_max_total, jvm_xmx_mb."""
            st = art._json("state.json")
            cfg = st.get("parsed_config") or {}
            if field not in cfg:
                return f"unknown field; available={list(cfg.keys())}"
            return json.dumps({"field": field, "value": cfg.get(field), "evidence": (cfg.get("evidence") or {}).get(field)}, ensure_ascii=False)

        @tool
        def get_report_section(name: str = "all") -> str:
            """분석 리포트 섹션을 조회합니다. name: headline|primary_bottleneck|prediction_vs_actual|correlations|recommendations|next_plan|all."""
            st = art._json("state.json")
            rep = st.get("analysis_report") or {}
            if not rep:
                return "no report"
            if name == "all":
                return json.dumps(rep, ensure_ascii=False)[:6000]
            return json.dumps(rep.get(name), ensure_ascii=False)

        return [get_bucket_series, get_gc_events, get_errors_sample, get_config_value, get_report_section]
