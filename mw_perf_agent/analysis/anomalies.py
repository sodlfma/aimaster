"""Rule-based anomaly detection over bucket series and GC events (SC-002B 3.4.2)."""
from __future__ import annotations

from typing import Optional

import pandas as pd

from model.schemas import Anomaly


def _merge_ranges(rows: list[tuple[int, int, str]], bucket_ms: int) -> list[tuple[int, int, str]]:
    """Merge adjacent buckets of the same step into one range."""
    merged: list[tuple[int, int, str]] = []
    for start, end, step in sorted(rows):
        if merged and merged[-1][2] == step and start <= merged[-1][1] + bucket_ms:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end), step)
        else:
            merged.append((start, end, step))
    return merged


def detect_anomalies(
    buckets: pd.DataFrame,
    gc: Optional[pd.DataFrame],
    bucket_sec: int = 5,
    baseline_step: Optional[str] = None,
    latency_factor: float = 3.0,
    err_rate_pct: float = 1.0,
    throughput_drop_ratio: float = 0.5,
) -> list[Anomaly]:
    out: list[Anomaly] = []
    if buckets is None or buckets.empty:
        return out
    bucket_ms = bucket_sec * 1000
    steps = list(dict.fromkeys(buckets["step"].tolist()))
    base_step = baseline_step or (steps[0] if steps else None)
    base = buckets[buckets["step"] == base_step]
    base_p99 = float(base["p99_ms"].median()) if not base.empty else float(buckets["p99_ms"].median())
    base_p99 = max(base_p99, 1.0)
    base_tps = float(base["tps"].median()) if not base.empty else float(buckets["tps"].median())

    lat_rows, err_rows, s503_rows, tps_rows = [], [], [], []
    for _, r in buckets.iterrows():
        start, end, step = int(r["bucket_start_ms"]), int(r["bucket_start_ms"]) + bucket_ms, str(r["step"])
        if r["p99_ms"] >= latency_factor * base_p99:
            lat_rows.append((start, end, step))
        if r["err_rate"] >= err_rate_pct:
            err_rows.append((start, end, step))
        if r["http_503"] > 0:
            s503_rows.append((start, end, step))
        if base_tps > 0 and r["tps"] < base_tps * throughput_drop_ratio and r["active"] >= (base["active"].max() if not base.empty else 0):
            tps_rows.append((start, end, step))

    for s, e, step in _merge_ranges(lat_rows, bucket_ms):
        seg = buckets[(buckets["bucket_start_ms"] >= s) & (buckets["bucket_start_ms"] < e)]
        out.append(Anomaly(step=step, start_ms=s, end_ms=e, kind="latency_spike",
                           detail=f"p99 최대 {seg['p99_ms'].max():.0f}ms (베이스라인 p99 {base_p99:.0f}ms의 {seg['p99_ms'].max()/base_p99:.1f}배)"))
    for s, e, step in _merge_ranges(err_rows, bucket_ms):
        seg = buckets[(buckets["bucket_start_ms"] >= s) & (buckets["bucket_start_ms"] < e)]
        out.append(Anomaly(step=step, start_ms=s, end_ms=e, kind="error_rate",
                           detail=f"에러율 최대 {seg['err_rate'].max():.1f}%"))
    for s, e, step in _merge_ranges(s503_rows, bucket_ms):
        seg = buckets[(buckets["bucket_start_ms"] >= s) & (buckets["bucket_start_ms"] < e)]
        out.append(Anomaly(step=step, start_ms=s, end_ms=e, kind="http_503",
                           detail=f"HTTP 503 {int(seg['http_503'].sum())}건"))
    for s, e, step in _merge_ranges(tps_rows, bucket_ms):
        seg = buckets[(buckets["bucket_start_ms"] >= s) & (buckets["bucket_start_ms"] < e)]
        out.append(Anomaly(step=step, start_ms=s, end_ms=e, kind="throughput_drop",
                           detail=f"TPS {seg['tps'].min():.1f} (베이스라인 {base_tps:.1f}의 {seg['tps'].min()/max(base_tps,0.01):.0%}) — 활성 스레드는 유지"))

    if gc is not None and not gc.empty:
        full = gc[gc["kind"] == "full"]
        for _, g in full.iterrows():
            ts = int(g["ts_ms"])
            b = buckets[(buckets["bucket_start_ms"] <= ts) & (buckets["bucket_start_ms"] + bucket_ms > ts)]
            step = str(b["step"].iat[0]) if not b.empty else "(test window 외)"
            out.append(Anomaly(step=step, start_ms=ts, end_ms=ts + int(g["pause_ms"] or 0), kind="full_gc",
                               detail=f"Full GC pause {g['pause_ms']}ms" + (f", Old {g['old_pct']}%" if pd.notna(g.get('old_pct')) else "")))

    out.sort(key=lambda a: a.start_ms)
    return out


def anomalies_overlap_summary(anomalies: list[Anomaly], window_ms: int = 10000) -> list[str]:
    """Human-readable co-occurrence hints (latency spike within window of Full GC, etc.)."""
    notes: list[str] = []
    fgc = [a for a in anomalies if a.kind == "full_gc"]
    for a in anomalies:
        if a.kind in ("latency_spike", "error_rate", "http_503"):
            near = [g for g in fgc if abs(g.start_ms - a.start_ms) <= window_ms or (g.start_ms >= a.start_ms and g.start_ms <= a.end_ms)]
            if near:
                notes.append(f"{a.step}: {a.kind} 구간({a.start_ms}~{a.end_ms})에 Full GC {len(near)}회 동반")
    return notes
