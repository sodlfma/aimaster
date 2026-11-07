"""Parse GC inputs into a uniform event table.

Supported formats:
1. jstat -gcutil output, optionally prefixed by a wall-clock timestamp
   (epoch ms or ISO-8601) by the collection wrapper. Without a timestamp, rows
   are aligned to `start_ms` with `interval_ms` spacing.
2. Unified JVM GC log lines (-Xlog:gc*) such as
   [2024-05-01T10:00:00.123+0900][info][gc] GC(12) Pause Full (G1 Compaction Pause) 512M->300M(1024M) 345.678ms
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

import pandas as pd

GC_COLUMNS = ["ts_ms", "kind", "pause_ms", "fgc", "ygc", "old_pct", "eden_pct", "gct_s"]

_ISO = re.compile(r"\[?(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)(Z|[+-]\d{2}:?\d{2})?\]?")
_EPOCH = re.compile(r"^\s*(\d{13})\b")
_UNIFIED_PAUSE = re.compile(r"GC\((\d+)\)\s+Pause\s+(Young|Full|Remark|Cleanup|Mixed)[^\d]*?.*?(\d+(?:\.\d+)?)ms")
_JSTAT_HEADER = re.compile(r"\bS0\b.*\bE\b.*\bO\b.*\bYGC\b.*\bFGC\b")


def _parse_iso(s: str, tz: Optional[str]) -> Optional[int]:
    try:
        txt = s + (tz or "")
        if tz and ":" not in tz and tz != "Z":
            txt = s + tz[:3] + ":" + tz[3:]
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def parse_gc_text(text: str, start_ms: Optional[int] = None, interval_ms: int = 5000) -> pd.DataFrame:
    if not text or not text.strip():
        return pd.DataFrame(columns=GC_COLUMNS)
    lines = text.splitlines()
    rows: list[dict] = []

    # --- unified GC log ---
    for line in lines:
        m = _UNIFIED_PAUSE.search(line)
        if not m:
            continue
        ts = None
        im = _ISO.search(line)
        if im:
            ts = _parse_iso(im.group(1), im.group(2))
        rows.append({"ts_ms": ts, "kind": m.group(2).lower(), "pause_ms": float(m.group(3)), "fgc": None, "ygc": None,
                     "old_pct": None, "eden_pct": None, "gct_s": None})
    if rows:
        df = pd.DataFrame(rows)
        if df["ts_ms"].isna().all() and start_ms is not None:
            df["ts_ms"] = [start_ms + i * interval_ms for i in range(len(df))]
        return df.dropna(subset=["ts_ms"]).astype({"ts_ms": "int64"})[GC_COLUMNS]

    # --- jstat -gcutil ---
    header_idx = None
    cols: list[str] = []
    for i, line in enumerate(lines):
        if _JSTAT_HEADER.search(line):
            header_idx = i
            cols = line.split()
            break
    if header_idx is None:
        return pd.DataFrame(columns=GC_COLUMNS)

    def col(name: str) -> Optional[int]:
        return cols.index(name) if name in cols else None

    i_fgc, i_ygc, i_o, i_e, i_gct = col("FGC"), col("YGC"), col("O"), col("E"), col("GCT")
    prev_fgc = None
    prev_fgct = None
    i_fgct = col("FGCT")
    n = 0
    for line in lines[header_idx + 1 :]:
        s = line.strip()
        if not s or _JSTAT_HEADER.search(s):
            continue
        ts = None
        m = _EPOCH.match(s)
        if m:
            ts = int(m.group(1))
            s = s[m.end():].strip()
        else:
            im = _ISO.match(s)
            if im:
                ts = _parse_iso(im.group(1), im.group(2))
                s = s[im.end():].strip()
        parts = s.split()
        # jstat -t adds a leading Timestamp column (seconds since JVM start); drop it if header has it
        if "Timestamp" in cols and len(parts) == len(cols):
            parts = parts[1:]
            offset = 1
        else:
            offset = 0
        if len(parts) < len(cols) - offset:
            continue

        def val(idx: Optional[int]) -> Optional[float]:
            if idx is None:
                return None
            j = idx - offset
            try:
                return float(parts[j])
            except (ValueError, IndexError):
                return None

        fgc, fgct = val(i_fgc), val(i_fgct)
        kind = "sample"
        pause = 0.0
        if prev_fgc is not None and fgc is not None and fgc > prev_fgc:
            kind = "full"
            if fgct is not None and prev_fgct is not None:
                pause = round((fgct - prev_fgct) * 1000, 1)
        prev_fgc, prev_fgct = fgc, fgct
        if ts is None and start_ms is not None:
            ts = start_ms + n * interval_ms
        n += 1
        rows.append({"ts_ms": ts, "kind": kind, "pause_ms": pause, "fgc": fgc, "ygc": val(i_ygc),
                     "old_pct": val(i_o), "eden_pct": val(i_e), "gct_s": val(i_gct)})
    df = pd.DataFrame(rows, columns=GC_COLUMNS)
    df = df.dropna(subset=["ts_ms"])
    if not df.empty:
        df["ts_ms"] = df["ts_ms"].astype("int64")
    return df


def gc_summary_text(gc: pd.DataFrame, max_rows: int = 120) -> str:
    if gc.empty:
        return "(GC 데이터 없음)"
    full = gc[gc["kind"] == "full"]
    head = f"samples={len(gc)}, full_gc_events={len(full)}, total_full_pause_ms={round(float(full['pause_ms'].sum()),1)}\n"
    g = gc
    if len(g) > max_rows:
        g = g.iloc[:: -(-len(g) // max_rows)]
    return head + g[["ts_ms", "kind", "pause_ms", "old_pct", "eden_pct"]].to_csv(index=False)
