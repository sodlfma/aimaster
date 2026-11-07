"""JTL (CSV) and dashboard statistics.json loaders."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pandas as pd

REQUIRED_COLUMNS = ["timeStamp", "elapsed", "label", "responseCode", "success", "threadName"]
OPTIONAL_COLUMNS = ["grpThreads", "allThreads", "Latency", "Connect", "bytes", "sentBytes", "responseMessage", "failureMessage"]


def load_jtl(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"JTL에 필수 컬럼이 없습니다: {missing}. CSV 형식(jmeter.save.saveservice.output_format=csv)으로 저장했는지 확인하세요.")
    df["timeStamp"] = pd.to_numeric(df["timeStamp"], errors="coerce")
    df = df.dropna(subset=["timeStamp"]).copy()
    df["timeStamp"] = df["timeStamp"].astype("int64")
    df["elapsed"] = pd.to_numeric(df["elapsed"], errors="coerce").fillna(0).astype("int64")
    df["responseCode"] = df["responseCode"].astype(str)
    if df["success"].dtype != bool:
        df["success"] = df["success"].astype(str).str.lower().isin(["true", "1"])
    for col in ("allThreads", "grpThreads", "Latency", "Connect"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")
    df["step"] = df["threadName"].astype(str).map(step_from_thread_name)
    return df.sort_values("timeStamp").reset_index(drop=True)


def step_from_thread_name(name: str) -> str:
    """'Step 3 (WAS Queueing) 1-17' -> 'Step 3 (WAS Queueing)'."""
    name = str(name)
    head, sep, tail = name.rpartition(" ")
    if sep and "-" in tail and tail.replace("-", "").isdigit():
        return head
    return name


def load_statistics(report_dir: Optional[str]) -> Optional[dict]:
    if not report_dir:
        return None
    p = Path(report_dir) / "statistics.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def failure_snapshot(path: str | Path, limit: int = 5) -> dict:
    """Summarise recent JTL failures for the live runner (partial CSV is ok)."""
    p = Path(path)
    empty = {"hint": "", "samples": [], "top_codes": []}
    if not p.exists() or p.stat().st_size < 40:
        return empty
    try:
        df = pd.read_csv(p, on_bad_lines="skip", low_memory=False)
    except (OSError, ValueError, pd.errors.EmptyDataError, pd.errors.ParserError):
        return empty
    if df.empty or "success" not in df.columns:
        return empty
    success = df["success"]
    if success.dtype != bool:
        success = success.astype(str).str.lower().isin(["true", "1"])
    bad = df.loc[~success]
    if bad.empty:
        return empty
    codes = (
        bad["responseCode"].astype(str).value_counts().head(3)
        if "responseCode" in bad.columns else None
    )
    top = [{"code": str(k), "count": int(v)} for k, v in (codes.items() if codes is not None else [])]
    cols = [c for c in ("responseCode", "responseMessage", "failureMessage", "label") if c in bad.columns]
    samples = bad.tail(limit)[cols].fillna("").astype(str).to_dict("records") if cols else []
    parts = [f"{t['code']} × {t['count']}" for t in top]
    msg = ""
    if "responseMessage" in bad.columns:
        last = str(bad.iloc[-1].get("responseMessage") or "")
        if last:
            msg = last.strip()[:160]
    hint = "최근 실패: " + (", ".join(parts) if parts else "unknown")
    if msg:
        hint += f" ({msg})"
    if any("Non HTTP" in str(t["code"]) or "Connect" in str(t["code"]) for t in top) or "Connect" in msg or "timed out" in msg.lower():
        hint += (
            " — 요청이 WAS에 도달하지 않았습니다. 부하기와 대상이 다른 EC2이면 "
            "대상 보안그룹에 부하기 → HTTP 포트 인바운드를 허용하세요. 같은 머신이면 localhost 로 보내야 합니다."
        )
    return {"hint": hint, "samples": samples, "top_codes": top}


def statistics_table(stats: Optional[dict]) -> list[dict]:
    if not stats:
        return []
    rows = []
    for label, s in stats.items():
        rows.append(
            {
                "label": s.get("transaction", label),
                "samples": s.get("sampleCount"),
                "errors": s.get("errorCount"),
                "error_pct": round(float(s.get("errorPct", 0)), 2),
                "avg_ms": round(float(s.get("meanResTime", 0)), 1),
                "p90_ms": round(float(s.get("pct1ResTime", 0)), 1),
                "p95_ms": round(float(s.get("pct2ResTime", 0)), 1),
                "p99_ms": round(float(s.get("pct3ResTime", 0)), 1),
                "tps": round(float(s.get("throughput", 0)), 2),
            }
        )
    return rows
