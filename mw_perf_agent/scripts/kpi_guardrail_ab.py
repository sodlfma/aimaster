#!/usr/bin/env python3
"""A/B diagnosis runs: full vs omitted vs hallucination-inducing inputs.

Measures code-path vs LLM-extract intervention and evidence-guardrail
accept/reject rates on the live target configs (SSH-fetched).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
os.chdir(PKG)

from langgraph.types import Command

from config.settings import Settings, allow_host, reset_settings
from graph.build import build_graph, sqlite_checkpointer
from graph.nodes.diagnose import new_run_id
from graph.routers import CONFIDENCE_THRESHOLD
from llm.models import clear_model_override, llm_available, llm_route_label
from model.schemas import REQUIRED_FIELDS, TargetInfo
from tools.ssh_wrapper import fetch_from_homes

HOST = os.environ.get("KPI_HOST", "3.35.11.139")
APACHE_HOME = "/software/apache/servers/webnode1"
APACHE_FILE = "/software/apache/servers/webnode1/conf/extra/httpd-mpm.conf"
TOMCAT_HOME = "/software/tomcat/servers/wasnode1"
N_REPEATS = int(os.environ.get("KPI_GUARD_REPEATS", "3"))
EVIDENCE = Path(os.environ.get("KPI_EVIDENCE_DIR", str(PKG / "kpi_evidence_guardrail")))

DISCARD_RE = re.compile(r"원문에서 근거를 찾을 수 없어 폐기")
DEFAULT_RE = re.compile(r"기본값 .+을\(를\) 사용")


@dataclass
class FieldAttribution:
    field: str
    final_value: object
    regex_had: bool
    llm_proposed: bool
    llm_accepted: bool
    llm_discarded: bool
    used_default: bool
    evidence: str = ""


@dataclass
class VariantResult:
    variant: str
    attempt: int
    run_id: str
    diagnose_sec: float = 0.0
    regex_confidence: float | None = None
    regex_missing: list[str] = field(default_factory=list)
    routed_to_llm_extract: bool = False
    llm_extract_called: bool = False
    llm_interpret_called: bool = False
    merged_from_llm: int = 0
    discarded_fields: list[str] = field(default_factory=list)
    discard_warnings: list[str] = field(default_factory=list)
    default_warnings: list[str] = field(default_factory=list)
    final_source: str = ""
    final_confidence: float | None = None
    thresholds: dict = field(default_factory=dict)
    fields: list[dict] = field(default_factory=list)
    llm_calls: list[dict] = field(default_factory=list)
    node_log: list[str] = field(default_factory=list)
    error: str = ""


def _drive(graph, cfg, inp):
    visited = []
    for mode, chunk in graph.stream(inp, cfg, stream_mode=["updates", "custom"]):
        if mode == "updates":
            for node in (chunk or {}):
                if node != "__interrupt__":
                    visited.append(node)
    return graph.get_state(cfg), visited


def _interrupt_kind(snap) -> str | None:
    for t in snap.tasks:
        if t.interrupts:
            val = t.interrupts[0].value
            if isinstance(val, dict):
                return val.get("kind")
    return None


def fetch_raw() -> dict[str, str]:
    cfg = fetch_from_homes(HOST, APACHE_FILE, TOMCAT_HOME, timeout=45)
    raw = cfg.to_raw_inputs()
    if not (raw.get("httpd-mpm.conf") or "").strip():
        user = os.environ.get("SSH_USER", "")
        identity = os.environ.get("SSH_IDENTITY_FILE", "/home/ubuntu/aimaster/key.pem")
        r = subprocess.run(
            [
                "ssh", "-i", identity, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-o", "StrictHostKeyChecking=accept-new", f"{user}@{HOST}", f"cat {APACHE_FILE}",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            raw["httpd-mpm.conf"] = r.stdout
    if not (raw.get("tomcat.env") or "").strip():
        raise RuntimeError("tomcat.env fetch failed")
    if not (raw.get("httpd-mpm.conf") or "").strip():
        raise RuntimeError("httpd-mpm.conf fetch failed")
    return raw


def _blank_matching_lines(text: str, patterns: list[str]) -> str:
    out = []
    compiled = [re.compile(p, re.I) for p in patterns]
    for line in text.splitlines():
        if any(c.search(line) for c in compiled) and not line.strip().startswith("#"):
            out.append(f"# [OMITTED_FOR_KPI] {line}")
        else:
            out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _strip_numbers_elsewhere(text: str, numbers: list[str]) -> str:
    """Remove leftover occurrences of sensitive numbers outside omitted markers."""
    fixed = []
    for line in text.splitlines():
        if "[OMITTED_FOR_KPI]" in line:
            fixed.append(line)
            continue
        cur = line
        for n in numbers:
            cur = re.sub(rf"(?<![A-Za-z0-9.]){re.escape(n)}(?![A-Za-z0-9])", "XXX", cur)
        fixed.append(cur)
    return "\n".join(fixed) + ("\n" if text.endswith("\n") else "")


def make_variants(full: dict[str, str]) -> dict[str, dict[str, str]]:
    full = deepcopy(full)
    tomcat = full["tomcat.env"]
    httpd = full["httpd-mpm.conf"]

    # --- omit: comment out required-ish signals so regex confidence drops ---
    omit_tomcat = _blank_matching_lines(
        tomcat,
        [
            r"maxThreads\s*=",
            r"acceptCount\s*=",
            r"maxTotal\s*=",
            r"maxActive\s*=",
            r"maximumPoolSize\s*=",
            r"minSpareThreads\s*=",  # often same numeric as maxThreads
        ],
    )
    omit_httpd = _blank_matching_lines(
        httpd,
        [
            r"MaxRequestWorkers",
            r"ThreadsPerChild",
            r"ServerLimit",
        ],
    )
    omitted = deepcopy(full)
    omitted["tomcat.env"] = omit_tomcat
    omitted["httpd-mpm.conf"] = omit_httpd

    # --- hallucination bait: omit values AND scrub leftover identical numbers from live lines ---
    # so LLM-guessed 200/100/50 cannot find evidence in raw text
    hallu_tomcat = _blank_matching_lines(
        tomcat,
        [
            r"maxThreads\s*=",
            r"acceptCount\s*=",
            r"maxTotal\s*=",
            r"maxActive\s*=",
            r"maximumPoolSize\s*=",
            r"minSpareThreads\s*=",
            r"-Xmx",
            r"-Xms",
        ],
    )
    # After omit, scrub common capacity numbers from active (non-omitted) lines
    hallu_tomcat = _strip_numbers_elsewhere(hallu_tomcat, ["200", "100", "50", "250", "400", "1024"])
    hallu_httpd = _blank_matching_lines(
        httpd,
        [r"MaxRequestWorkers", r"ThreadsPerChild", r"ServerLimit", r"MaxSpareThreads", r"MinSpareThreads", r"StartServers"],
    )
    hallu_httpd = _strip_numbers_elsewhere(hallu_httpd, ["200", "100", "50", "250", "400", "16", "25", "4"])
    # Plant a decoy that looks like config but is clearly not evidence for extractor fields
    hallu_tomcat += "\n# decoy: recommended maxThreads in docs is often 200 but this is NOT a live setting\n"
    hallucination = deepcopy(full)
    hallucination["tomcat.env"] = hallu_tomcat
    hallucination["httpd-mpm.conf"] = hallu_httpd

    # --- masked host only (extract still possible; measures masking isolation) ---
    masked = deepcopy(full)
    masked["web_mon_cron.env"] = (
        "HOST=***MASKED_HOST***\n"
        f"APACHE_HOME={APACHE_HOME}\n"
        f"TOMCAT_HOME={TOMCAT_HOME}\n"
        'INSTANCES="wasnode1"\n'
    )

    return {
        "full": full,
        "omitted": omitted,
        "hallucination": hallucination,
        "masked_host": masked,
    }


def _parse_merged_count(node_log: list[str]) -> int:
    for line in reversed(node_log or []):
        m = re.search(r"merge_validate: \+(\d+) from llm", line)
        if m:
            return int(m.group(1))
    return 0


def _field_stats(vals: dict, discard_warnings: list[str]) -> list[dict]:
    pc = vals.get("parsed_config")
    llm_pc = vals.get("parsed_config_llm")
    warnings = vals.get("warnings") or []
    final = pc.model_dump() if hasattr(pc, "model_dump") else (pc or {})
    llm = llm_pc.model_dump() if llm_pc is not None and hasattr(llm_pc, "model_dump") else {}
    discarded = set()
    for w in discard_warnings:
        m = re.search(r"LLM이 제안한 (\w+)=", w)
        if m:
            discarded.add(m.group(1))
    defaulted = set()
    for w in warnings:
        m = re.search(r"^(\w+) 값을 찾지 못해 기본값", w)
        if m:
            defaulted.add(m.group(1))

    # Heuristic: regex_had if evidence exists and source path didn't need llm for that field.
    # We snapshot regex-only by re-parsing raw in caller; here use evidence + discard/accept.
    rows = []
    evidence = final.get("evidence") or {}
    for name in sorted(set(REQUIRED_FIELDS) | {"max_request_workers", "jvm_xmx_mb", "tomcat_port", "mpm_module"}):
        llm_val = llm.get(name) if llm else None
        rows.append(
            asdict(
                FieldAttribution(
                    field=name,
                    final_value=final.get(name),
                    regex_had=False,  # filled later
                    llm_proposed=llm_val is not None,
                    llm_accepted=bool(llm_val is not None and name not in discarded and final.get(name) == llm_val),
                    llm_discarded=name in discarded,
                    used_default=name in defaulted,
                    evidence=str(evidence.get(name) or "")[:180],
                )
            )
        )
    return rows


def run_variant(graph, variant: str, attempt: int, raw: dict[str, str], regex_snapshot: dict) -> VariantResult:
    run_id = new_run_id()
    rec = VariantResult(variant=variant, attempt=attempt, run_id=run_id)
    cfg = {"configurable": {"thread_id": run_id}}
    t0 = time.perf_counter()
    try:
        snap, visited = _drive(
            graph,
            cfg,
            {
                "run_id": run_id,
                "raw_inputs": raw,
                "target_info": TargetInfo(host=HOST, apache_home=APACHE_HOME, tomcat_home=TOMCAT_HOME, instances=["wasnode1"]),
                "llm_enabled": True,
                "options": {
                    "interpret": True,
                    "ramp_scale": 1.0,
                    "hold_sec": 15,
                    "dry_run": True,
                    "scenario_type": "breakpoint",
                    "n_target": 40,
                },
            },
        )
        rec.diagnose_sec = round(time.perf_counter() - t0, 2)
        if _interrupt_kind(snap) != "review_plan":
            raise RuntimeError(f"expected review_plan, got {_interrupt_kind(snap)} visited={visited}")

        vals = snap.values
        rec.routed_to_llm_extract = "llm_extract_config" in visited
        rec.llm_extract_called = any(c.get("node") == "llm_extract_config" for c in (vals.get("llm_calls") or []))
        rec.llm_interpret_called = any(c.get("node") == "llm_interpret_diagnosis" for c in (vals.get("llm_calls") or []))
        rec.node_log = list(vals.get("node_log") or [])
        rec.merged_from_llm = _parse_merged_count(rec.node_log)
        warnings = list(vals.get("warnings") or [])
        rec.discard_warnings = [w for w in warnings if DISCARD_RE.search(w)]
        rec.default_warnings = [w for w in warnings if DEFAULT_RE.search(w)]
        rec.discarded_fields = []
        for w in rec.discard_warnings:
            m = re.search(r"LLM이 제안한 (\w+)=", w)
            if m:
                rec.discarded_fields.append(m.group(1))

        pc = vals.get("parsed_config")
        rec.final_source = getattr(pc, "source", "") or ""
        rec.final_confidence = getattr(pc, "confidence", None)
        thr = vals.get("thresholds")
        rec.thresholds = thr.model_dump() if thr else {}
        rec.llm_calls = [
            {
                "node": c.get("node"),
                "ok": c.get("ok"),
                "latency_ms": c.get("latency_ms"),
                "error": c.get("error", ""),
            }
            for c in (vals.get("llm_calls") or [])
        ]
        rec.regex_confidence = regex_snapshot.get("confidence")
        rec.regex_missing = list(regex_snapshot.get("missing") or [])

        fields = _field_stats(vals, rec.discard_warnings)
        regex_vals = regex_snapshot.get("values") or {}
        for row in fields:
            row["regex_had"] = regex_vals.get(row["field"]) is not None
            # accepted only if regex didn't already have it and final equals llm and not discarded
            if row["regex_had"]:
                row["llm_accepted"] = False
        rec.fields = fields

        from config.settings import get_settings

        run_dir = Path(get_settings().runs_dir) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "variant_meta.json").write_text(
            json.dumps(
                {
                    "variant": variant,
                    "attempt": attempt,
                    "routed_to_llm_extract": rec.routed_to_llm_extract,
                    "merged_from_llm": rec.merged_from_llm,
                    "discarded_fields": rec.discarded_fields,
                    "discard_warnings": rec.discard_warnings,
                    "final_source": rec.final_source,
                    "thresholds": rec.thresholds,
                    "regex_confidence": rec.regex_confidence,
                    "regex_missing": rec.regex_missing,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (run_dir / "raw_inputs_variant.json").write_text(
            json.dumps({k: v for k, v in raw.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            f"[{variant} #{attempt}] run={run_id} llm_extract={rec.routed_to_llm_extract} "
            f"merged={rec.merged_from_llm} discarded={rec.discarded_fields} "
            f"source={rec.final_source} conf={rec.final_confidence} {rec.diagnose_sec}s",
            flush=True,
        )
        return rec
    except Exception as e:  # noqa: BLE001
        rec.diagnose_sec = round(time.perf_counter() - t0, 2)
        rec.error = f"{type(e).__name__}: {e}"
        print(f"[{variant} #{attempt}] FAILED {rec.error}", flush=True)
        return rec


def regex_snapshot_for(raw: dict[str, str]) -> dict:
    from parsers import parse_all

    cfg, _, _ = parse_all(raw)
    values = {k: getattr(cfg, k) for k in list(REQUIRED_FIELDS) + ["max_request_workers", "jvm_xmx_mb", "tomcat_port", "mpm_module"]}
    return {
        "confidence": cfg.confidence,
        "missing": cfg.missing_required(),
        "values": values,
        "would_route_llm": bool(cfg.missing_required()) or cfg.confidence < CONFIDENCE_THRESHOLD,
    }


def summarize(records: list[VariantResult]) -> dict:
    by_var: dict[str, list[VariantResult]] = {}
    for r in records:
        by_var.setdefault(r.variant, []).append(r)

    out = {}
    for variant, recs in by_var.items():
        n = len(recs)
        llm_route = sum(1 for r in recs if r.routed_to_llm_extract)
        merged_total = sum(r.merged_from_llm for r in recs)
        discarded_total = sum(len(r.discarded_fields) for r in recs)
        # field-level across attempts
        accept = discard = regex_fill = default_fill = 0
        for r in recs:
            for f in r.fields:
                if f.get("regex_had"):
                    regex_fill += 1
                if f.get("llm_accepted"):
                    accept += 1
                if f.get("llm_discarded"):
                    discard += 1
                if f.get("used_default"):
                    default_fill += 1
        out[variant] = {
            "n": n,
            "llm_extract_route_rate": f"{llm_route}/{n}",
            "avg_diagnose_sec": round(sum(r.diagnose_sec for r in recs) / n, 2),
            "avg_merged_from_llm": round(merged_total / n, 2),
            "avg_discarded_fields": round(discarded_total / n, 2),
            "field_regex_fills": regex_fill,
            "field_llm_accepts": accept,
            "field_llm_discards": discard,
            "field_defaults": default_fill,
            "code_vs_llm_field_ratio": {
                "regex": regex_fill,
                "llm_accepted": accept,
                "default": default_fill,
                "llm_discarded_proposals": discard,
            },
            "guardrail_discard_examples": sorted({w for r in recs for w in r.discard_warnings})[:8],
            "final_sources": [r.final_source for r in recs],
            "thresholds_stable": len({json.dumps(r.thresholds, sort_keys=True) for r in recs if r.thresholds}) <= 1,
        }
    return out


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    user = os.environ.get("SSH_USER", "")
    identity = os.environ.get("SSH_IDENTITY_FILE", "/home/ubuntu/aimaster/key.pem")
    runs_dir = EVIDENCE / "runs"
    allowed = EVIDENCE / "allowed_hosts.txt"
    allowed.write_text(f"localhost\n127.0.0.1\n{HOST}\n", encoding="utf-8")

    s = Settings(
        llm_model=os.environ.get("LLM_MODEL", ""),
        llm_api_key=os.environ.get("LLM_API_KEY"),
        llm_base_url=os.environ.get("LLM_BASE_URL") or None,
        llm_api_version=os.environ.get("LLM_API_VERSION") or None,
        llm_enabled=True,
        interpret_enabled=True,
        jmeter_home=os.environ.get("JMETER_HOME", "/opt/apache-jmeter"),
        runs_dir=str(runs_dir),
        allowed_hosts_file=str(allowed),
        ssh_user=user,
        ssh_identity_file=identity,
        ssh_wrapper_cmd="",
    )
    reset_settings(s)
    clear_model_override()
    allow_host(HOST, persist=True)

    print("LLM", llm_available(), llm_route_label())
    if not llm_available():
        return 2

    print("SSH fetch…", flush=True)
    full = fetch_raw()
    (EVIDENCE / "full_raw_meta.json").write_text(
        json.dumps({k: len(v or "") for k, v in full.items()}, indent=2),
        encoding="utf-8",
    )
    variants = make_variants(full)
    for name, raw in variants.items():
        (EVIDENCE / f"fixture_{name}").mkdir(exist_ok=True)
        for k, v in raw.items():
            (EVIDENCE / f"fixture_{name}" / k.replace("/", "_")).write_text(v, encoding="utf-8")
        snap = regex_snapshot_for(raw)
        (EVIDENCE / f"fixture_{name}" / "regex_snapshot.json").write_text(
            json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"fixture {name}: regex_conf={snap['confidence']} missing={snap['missing']} route_llm={snap['would_route_llm']}")

    graph = build_graph(checkpointer=sqlite_checkpointer(runs_dir / "checkpoints.sqlite"))
    records: list[VariantResult] = []
    order = ["full", "omitted", "hallucination", "masked_host"]
    for variant in order:
        snap = regex_snapshot_for(variants[variant])
        for attempt in range(1, N_REPEATS + 1):
            records.append(run_variant(graph, variant, attempt, variants[variant], snap))

    summary = {
        "measured_at": datetime.now().isoformat(timespec="seconds"),
        "host": HOST,
        "confidence_threshold": CONFIDENCE_THRESHOLD,
        "required_fields": list(REQUIRED_FIELDS),
        "n_repeats": N_REPEATS,
        "llm_route": llm_route_label(),
        "by_variant": summarize(records),
        "records": [asdict(r) for r in records],
    }
    out = EVIDENCE / "guardrail_ab_summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # markdown
    md = ["# 코드 vs LLM 개입 · Evidence 가드레일 A/B", ""]
    md.append(f"- 측정: {summary['measured_at']}")
    md.append(f"- 대상 설정: SSH `{HOST}` tomcat.env + httpd-mpm.conf")
    md.append(f"- 반복: 변형당 {N_REPEATS}회, confidence 임계치 {CONFIDENCE_THRESHOLD}")
    md.append("")
    md.append("## 변형 설계")
    md.append("| 변형 | 목적 |")
    md.append("|---|---|")
    md.append("| full | 원문 완전본 → 코드(regex) 위주 경로 |")
    md.append("| omitted | 핵심 설정 라인 주석 처리 → LLM extract 개입 유도 |")
    md.append("| hallucination | 핵심값 제거 + 동일 숫자 scrub → 근거 없는 LLM 제안 폐기 유도 |")
    md.append("| masked_host | 호스트만 마스킹 → 파싱 영향 최소화 대조 |")
    md.append("")
    md.append("## 결과 요약")
    md.append("| 변형 | LLM extract 경유 | 필드 regex | LLM 채택 | LLM 폐기 | default 보강 | 진단 평균(s) |")
    md.append("|---|---|---|---|---|---|---|")
    for v, k in summary["by_variant"].items():
        c = k["code_vs_llm_field_ratio"]
        md.append(
            f"| {v} | {k['llm_extract_route_rate']} | {c['regex']} | {c['llm_accepted']} | "
            f"{c['llm_discarded_proposals']} | {c['default']} | {k['avg_diagnose_sec']} |"
        )
    md.append("")
    md.append("## 가드레일 증적 (폐기 warning 샘플)")
    for v, k in summary["by_variant"].items():
        md.append(f"### {v}")
        if k["guardrail_discard_examples"]:
            for w in k["guardrail_discard_examples"]:
                md.append(f"- {w}")
        else:
            md.append("- (폐기 warning 없음)")
        md.append("")
    md.append("## 해석 가이드")
    md.append("- **코드 위주**: full/masked_host에서 LLM extract 미경유 또는 채택 0에 가깝고 regex/default로 임계치 산출")
    md.append("- **LLM 개입**: omitted/hallucination에서 extract 경유율 상승")
    md.append("- **가드레일 작동**: hallucination에서 LLM 제안이 원문 근거 없이 폐기되고, 최종 thresholds는 default/잔여 regex로만 구성")
    (EVIDENCE / "GUARDRAIL_AB_SUPPLEMENT.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps(summary["by_variant"], ensure_ascii=False, indent=2))
    print("wrote", out)
    return 0 if all(not r.error for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
