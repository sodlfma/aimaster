"""Deterministic aggregation of JTL data into time buckets and per-step summaries."""
from __future__ import annotations

from typing import Optional

import pandas as pd

from model.schemas import LoadPlan

BUCKET_COLUMNS = ["bucket_start_ms", "step", "count", "tps", "avg_ms", "p95_ms", "p99_ms", "err_rate", "active", "http_5xx", "http_503"]


def aggregate_buckets(df: pd.DataFrame, bucket_sec: int = 5) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=BUCKET_COLUMNS)
    bucket_ms = bucket_sec * 1000
    d = df.copy()
    d["bucket_start_ms"] = (d["timeStamp"] // bucket_ms) * bucket_ms
    d["is_err"] = ~d["success"]
    d["is_5xx"] = d["responseCode"].str.startswith("5")
    d["is_503"] = d["responseCode"] == "503"
    active_col = "allThreads" if "allThreads" in d.columns else None

    def agg(g: pd.DataFrame) -> pd.Series:
        return pd.Series(
            {
                "step": g["step"].mode().iat[0] if not g["step"].mode().empty else "",
                "count": len(g),
                "tps": round(len(g) / bucket_sec, 2),
                "avg_ms": round(float(g["elapsed"].mean()), 1),
                "p95_ms": round(float(g["elapsed"].quantile(0.95)), 1),
                "p99_ms": round(float(g["elapsed"].quantile(0.99)), 1),
                "err_rate": round(float(g["is_err"].mean()) * 100, 2),
                "active": int(g[active_col].max()) if active_col else 0,
                "http_5xx": int(g["is_5xx"].sum()),
                "http_503": int(g["is_503"].sum()),
            }
        )

    out = d.groupby("bucket_start_ms", sort=True).apply(agg, include_groups=False).reset_index()
    return out[BUCKET_COLUMNS]


def step_summary(df: pd.DataFrame, plan: Optional[LoadPlan] = None) -> list[dict]:
    if df.empty:
        return []
    rows = []
    plan_by_name = {s.name: s for s in plan.steps} if plan else {}
    for step, g in df.groupby("step", sort=False):
        start, end = int(g["timeStamp"].min()), int(g["timeStamp"].max() + g["elapsed"].max())
        dur_s = max(1.0, (end - start) / 1000)
        p = plan_by_name.get(step)
        rows.append(
            {
                "step": step,
                "target_concurrency": p.target_concurrency if p else None,
                "expected_symptom": p.expected_symptom if p else "",
                "start_ms": start,
                "end_ms": end,
                "duration_s": round(dur_s, 1),
                "samples": int(len(g)),
                "tps": round(len(g) / dur_s, 2),
                "avg_ms": round(float(g["elapsed"].mean()), 1),
                "p95_ms": round(float(g["elapsed"].quantile(0.95)), 1),
                "p99_ms": round(float(g["elapsed"].quantile(0.99)), 1),
                "err_rate": round(float((~g["success"]).mean()) * 100, 2),
                "http_503": int((g["responseCode"] == "503").sum()),
                "max_active": int(g["allThreads"].max()) if "allThreads" in g.columns else None,
            }
        )
    rows.sort(key=lambda r: r["start_ms"])
    return rows


def buckets_to_csv_text(buckets: pd.DataFrame, max_rows: int = 300) -> str:
    """Compress the bucket table for the LLM: downsample to max_rows if needed."""
    if buckets.empty:
        return ""
    b = buckets
    if len(b) > max_rows:
        factor = -(-len(b) // max_rows)
        b = b.iloc[::factor]
    cols = ["bucket_start_ms", "step", "tps", "avg_ms", "p95_ms", "p99_ms", "err_rate", "active", "http_503"]
    return b[cols].to_csv(index=False)
