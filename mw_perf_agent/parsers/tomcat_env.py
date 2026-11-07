"""Regex extraction from shl/tomcat.env (JAVA_OPTS) with server.xml fallback.
Each match records the evidence line so the UI and the LLM merge step can
show where a value came from."""
from __future__ import annotations

import re
from typing import Optional

Found = dict[str, tuple[object, str]]  # field -> (value, evidence line)

_PATTERNS: dict[str, list[re.Pattern]] = {
    "tomcat_max_threads": [
        re.compile(r"-D[\w.]*maxThreads\s*=\s*\"?(\d+)", re.I),
        re.compile(r"\bmaxThreads\s*=\s*\"?(\d+)", re.I),
    ],
    "tomcat_accept_count": [
        re.compile(r"-D[\w.]*acceptCount\s*=\s*\"?(\d+)", re.I),
        re.compile(r"\bacceptCount\s*=\s*\"?(\d+)", re.I),
    ],
    "tomcat_port": [
        re.compile(r"-D[\w.]*(?:connector\.)?port\s*=\s*\"?(\d{2,5})", re.I),
        re.compile(r"<Connector[^>]*\bport\s*=\s*\"(\d{2,5})\"", re.I),
    ],
    "dbcp_max_total": [
        re.compile(r"-D[\w.]*(?:maxTotal|maxActive|maximumPoolSize)\s*=\s*\"?(\d+)", re.I),
        re.compile(r"\b(?:maxTotal|maxActive|maximumPoolSize)\s*=\s*\"?(\d+)", re.I),
    ],
    "dbcp_max_wait_ms": [
        re.compile(r"-D[\w.]*(?:maxWaitMillis|maxWait|connectionTimeout)\s*=\s*\"?(\d+)", re.I),
        re.compile(r"\b(?:maxWaitMillis|maxWait)\s*=\s*\"?(\d+)", re.I),
    ],
}

_XMX = re.compile(r"-Xmx(\d+)([kKmMgG]?)")
_XMS = re.compile(r"-Xms(\d+)([kKmMgG]?)")
_GC = re.compile(r"-XX:\+Use(G1GC|ParallelGC|ConcMarkSweepGC|ZGC|ShenandoahGC|SerialGC)")
_SKIP_PORT_LINE = re.compile(r"jmxremote|rmi\.|shutdown|redirectPort|debug\.port|\bajp\b", re.I)
_PREFER_PORT_LINE = re.compile(r"tomcat\.connector\.port|connector\.port|http\.port|https\.port", re.I)
_HTTP_PORTS = {80, 443, 8000, 8008, 8080, 8081, 8443, 8888, 9090}


def _to_mb(num: str, unit: str) -> int:
    n = int(num)
    u = unit.lower()
    if u == "g":
        return n * 1024
    if u == "k":
        return max(1, n // 1024)
    if u == "m" or u == "":
        return n
    return n


def _scan(text: str, patterns: list[re.Pattern]) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for pat in patterns:
            m = pat.search(line)
            if m:
                hits.append((m.group(1), stripped))
                break
    return hits


def _pick_tomcat_port(hits: list[tuple[str, str]]) -> tuple[Optional[tuple[int, str]], bool]:
    """Prefer HTTP connector ports over JMX/shutdown/AJP so the first match is not 1099."""
    usable = [(v, ev) for v, ev in hits if not _SKIP_PORT_LINE.search(ev)]
    pool = usable or hits
    preferred = [(v, ev) for v, ev in pool if _PREFER_PORT_LINE.search(ev)]
    httpish = [(v, ev) for v, ev in pool if int(v) in _HTTP_PORTS]
    chosen = preferred or httpish or pool
    values = {v for v, _ in chosen}
    return (int(chosen[0][0]), chosen[0][1]), len(values) > 1


def parse_tomcat_env(text: str) -> tuple[Found, list[str]]:
    """Return (found, conflicts). A conflict is a field that matched several
    distinct values; the first match wins but confidence is penalised."""
    found: Found = {}
    conflicts: list[str] = []
    if not text:
        return found, conflicts

    for field, pats in _PATTERNS.items():
        hits = _scan(text, pats)
        if not hits:
            continue
        if field == "tomcat_port":
            picked, conflicted = _pick_tomcat_port(hits)
            if picked is None:
                continue
            found[field] = picked
            if conflicted:
                conflicts.append(field)
            continue
        values = {v for v, _ in hits}
        if len(values) > 1:
            conflicts.append(field)
        val, ev = hits[0]
        found[field] = (int(val), ev)

    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        if "jvm_xmx_mb" not in found:
            m = _XMX.search(line)
            if m:
                found["jvm_xmx_mb"] = (_to_mb(m.group(1), m.group(2)), s)
        if "jvm_xms_mb" not in found:
            m = _XMS.search(line)
            if m:
                found["jvm_xms_mb"] = (_to_mb(m.group(1), m.group(2)), s)
        if "gc_type" not in found:
            m = _GC.search(line)
            if m:
                found["gc_type"] = (m.group(1), s)
    return found, conflicts


def find_evidence_line(text: str, value: object) -> Optional[str]:
    """Used by merge_validate to verify LLM-provided values exist in the source."""
    needle = str(value)
    for line in text.splitlines():
        if needle in line:
            return line.strip()
    return None
