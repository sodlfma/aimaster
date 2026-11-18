#!/usr/bin/env python3
"""Console A/B: mid-pipeline HITL checkpoints vs auto-pass.

Compares error / fabricated-evidence propagation when a human (simulated)
corrects state at gate_review_plan / gate_review_scenario versus blindly
resuming with Command(resume={}).

Does not require JMeter; stops at confirm_run (or earlier).
"""
from __future__ import annotations

import json
import os
import re
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
from llm.models import clear_model_override, llm_available, llm_route_label
from model.schemas import DEFAULTS, REQUIRED_FIELDS, ParsedConfig, ScenarioSpec, TargetInfo
from model.thresholds import compute_thresholds
from parsers import parse_all

HOST = os.environ.get("KPI_HOST", "3.35.11.139")
APACHE_HOME = "/software/apache/servers/webnode1"
TOMCAT_HOME = "/software/tomcat/servers/wasnode1"
N_REPEATS = int(os.environ.get("KPI_HITL_REPEATS", "3"))
EVIDENCE = Path(os.environ.get("KPI_EVIDENCE_DIR", str(PKG / "kpi_evidence_hitl")))
FIXTURE_DIR = Path(os.environ.get("KPI_HITL_FIXTURE", str(PKG / "kpi_evidence_guardrail" / "fixture_full")))
PERF_PATH = "/api/perf/db-delay?holdMs=300"

# Active-config key patterns used to judge whether a field has real source evidence.
FIELD_KEY_RE = {
    "tomcat_max_threads": re.compile(r"maxThreads\s*=", re.I),
    "tomcat_accept_count": re.compile(r"acceptCount\s*=", re.I),
    "dbcp_max_total": re.compile(r"(maxTotal|maxActive|maximumPoolSize)\s*=", re.I),
    "max_request_workers": re.compile(r"MaxRequestWorkers", re.I),
}


@dataclass
class ArmResult:
    arm: str  # auto | hitl
    attempt: int
    run_id: str
    diagnose_sec: float = 0.0
    fabricated_fields: list[str] = field(default_factory=list)
    remaining_fabrications: list[str] = field(default_factory=list)
    field_errors: dict = field(default_factory=dict)
    thresholds: dict = field(default_factory=dict)
    truth_thresholds: dict = field(default_factory=dict)
    threshold_l1: float = 0.0
    plan_concurrency: list[int] = field(default_factory=list)
    truth_plan_concurrency: list[int] = field(default_factory=list)
    plan_l1: float = 0.0
    scenario_host: str = ""
    scenario_path: str = ""
    scenario_host_ok: bool = False
    scenario_path_ok: bool = False
    validation_ok: bool | None = None
    node_log_tail: list[str] = field(default_factory=list)
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


def load_fixture(path: Path) -> dict[str, str]:
    raw: dict[str, str] = {}
    for p in path.iterdir():
        if p.is_file() and p.name != "regex_snapshot.json":
            raw[p.name] = p.read_text(encoding="utf-8")
    if not raw.get("tomcat.env") or not raw.get("httpd-mpm.conf"):
        raise RuntimeError(f"fixture incomplete under {path}")
    return raw


def active_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def has_active_key(raw: dict[str, str], field: str) -> bool:
    pat = FIELD_KEY_RE.get(field)
    if not pat:
        return False
    blob = "\n".join(active_lines("\n".join(raw.values())))
    return bool(pat.search(blob))


def detect_fabrications(pc: ParsedConfig, raw: dict[str, str], regex_vals: dict) -> list[str]:
    """LLM/merged values whose claimed evidence line is not in raw, or whose key is absent."""
    raw_text = "\n".join(raw.values())
    out = []
    for name in REQUIRED_FIELDS:
        val = getattr(pc, name, None)
        if val is None:
            continue
        if regex_vals.get(name) is not None:
            continue
        ev = (pc.evidence or {}).get(name) or ""
        if ev.startswith("(기본값") or ev.startswith("(사용자"):
            continue
        fabricated_line = bool(ev) and ev not in raw_text
        no_key = not has_active_key(raw, name)
        if fabricated_line or no_key:
            out.append(name)
    return out


def truth_config(raw: dict[str, str]) -> ParsedConfig:
    """Ground truth: regex wins; missing required → deterministic DEFAULTS (no LLM fill)."""
    cfg, _, _ = parse_all(raw)
    data = cfg.model_dump()
    for name, default in DEFAULTS.items():
        if data.get(name) is None:
            data[name] = default
            data["evidence"][name] = f"(기본값 {default} 사용 — 원문에서 추출 실패)"
    data["source"] = "regex" if cfg.source == "regex" else cfg.source
    return ParsedConfig(**data)


def thr_dict(thr) -> dict:
    return {
        "t_db_lock": thr.t_db_lock,
        "t_queue": thr.t_queue,
        "t_drop": thr.t_drop,
        "web_cap": thr.web_cap,
    }


def l1(a: dict, b: dict, keys=("t_db_lock", "t_queue", "t_drop")) -> float:
    return float(sum(abs(int(a.get(k) or 0) - int(b.get(k) or 0)) for k in keys))


def plan_conc(plan) -> list[int]:
    if not plan:
        return []
    return [int(s.target_concurrency) for s in plan.steps]


def field_error_map(pc: ParsedConfig, truth: ParsedConfig) -> dict:
    errs = {}
    for name in REQUIRED_FIELDS:
        got = getattr(pc, name, None)
        exp = getattr(truth, name, None)
        if got != exp:
            errs[name] = {"got": got, "expected": exp}
    return errs


def hitl_strip_fabrications(pc: ParsedConfig, fabricated: list[str]) -> ParsedConfig:
    data = pc.model_dump()
    for name in fabricated:
        data[name] = None
        data["evidence"].pop(name, None)
    return ParsedConfig(**data)


def bad_scenario_spec() -> ScenarioSpec:
    """Hallucinated / wrong mid-pipeline scenario a blind auto-pass would keep."""
    return ScenarioSpec(
        name="hallucinated-login",
        protocol="http",
        host="prod-internal.example.invalid",
        port=80,
        requests=[{"label": "login", "method": "GET", "path": "/api/v1/login", "body": "", "headers": {}}],
    )


def good_scenario_spec() -> ScenarioSpec:
    return ScenarioSpec(
        name="perf-db-delay",
        protocol="http",
        host=HOST,
        port=8080,
        requests=[
            {
                "label": "db-delay",
                "method": "GET",
                "path": PERF_PATH,
                "body": "",
                "headers": {"Accept": "application/json"},
            }
        ],
    )


def run_arm(graph, arm: str, attempt: int, raw: dict[str, str], regex_vals: dict, truth: ParsedConfig) -> ArmResult:
    run_id = new_run_id()
    rec = ArmResult(arm=arm, attempt=attempt, run_id=run_id)
    cfg = {"configurable": {"thread_id": run_id}}
    truth_thr = compute_thresholds(truth)
    rec.truth_thresholds = thr_dict(truth_thr)
    from model.load_plan import generate_load_plan

    truth_plan, _ = generate_load_plan(truth_thr, ramp_scale=1.0, hold_sec=15, scenario_type="breakpoint")
    rec.truth_plan_concurrency = plan_conc(truth_plan)

    t0 = time.perf_counter()
    try:
        snap, visited = _drive(
            graph,
            cfg,
            {
                "run_id": run_id,
                "raw_inputs": raw,
                "target_info": TargetInfo(
                    host=HOST, apache_home=APACHE_HOME, tomcat_home=TOMCAT_HOME, instances=["wasnode1"]
                ),
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

        pc: ParsedConfig = snap.values["parsed_config"]
        fabricated = detect_fabrications(pc, raw, regex_vals)
        rec.fabricated_fields = list(fabricated)

        if arm == "hitl" and fabricated:
            # Same path as UI Tab1 "설정값 수정 반영 → 임계치 재계산"
            cleaned = hitl_strip_fabrications(pc, fabricated)
            graph.update_state(cfg, {"parsed_config": cleaned}, as_node="merge_validate")
            snap, _ = _drive(graph, cfg, None)
            if _interrupt_kind(snap) != "review_plan":
                raise RuntimeError(f"HITL recompute expected review_plan, got {_interrupt_kind(snap)}")

        pc = snap.values["parsed_config"]
        thr = snap.values["thresholds"]
        plan = snap.values["load_plan"]
        rec.remaining_fabrications = detect_fabrications(pc, raw, regex_vals)
        # After HITL strip, defaults fill missing — those are not fabrications.
        rec.field_errors = field_error_map(pc, truth)
        rec.thresholds = thr_dict(thr)
        rec.threshold_l1 = l1(rec.thresholds, rec.truth_thresholds)
        rec.plan_concurrency = plan_conc(plan)
        rec.plan_l1 = float(
            sum(abs(a - b) for a, b in zip(rec.plan_concurrency, rec.truth_plan_concurrency))
            + abs(len(rec.plan_concurrency) - len(rec.truth_plan_concurrency))
        )

        # --- gate_review_scenario: auto keeps bait wrong spec; HITL corrects ---
        bait = bad_scenario_spec()
        if arm == "auto":
            resume_plan = {
                "scenario_input": {
                    "kind": "url",
                    "payload": f"http://{bait.host}:{bait.port}{bait.requests[0].path}",
                    "method": "GET",
                    "host": bait.host,
                    "port": bait.port,
                    "protocol": "http",
                }
            }
        else:
            resume_plan = {
                "scenario_input": {
                    "kind": "url",
                    "payload": f"http://{HOST}:8080{PERF_PATH}",
                    "method": "GET",
                    "host": HOST,
                    "port": 8080,
                    "protocol": "http",
                }
            }
        snap, _ = _drive(graph, cfg, Command(resume=resume_plan))
        if _interrupt_kind(snap) != "review_scenario":
            raise RuntimeError(f"expected review_scenario, got {_interrupt_kind(snap)}")

        if arm == "auto":
            # Blind approve whatever came out (or force bait if design emptied it)
            spec = snap.values.get("scenario_spec") or bait
            if spec.host in ("", "localhost") and bait.host:
                spec = bait
            snap, _ = _drive(graph, cfg, Command(resume={"scenario_spec": spec.model_dump()}))
        else:
            snap, _ = _drive(graph, cfg, Command(resume={"scenario_spec": good_scenario_spec().model_dump()}))

        if _interrupt_kind(snap) != "confirm_run":
            # validation may bounce to review_scenario; record and stop
            kind = _interrupt_kind(snap)
            vals = snap.values
            spec = vals.get("scenario_spec")
            rec.scenario_host = getattr(spec, "host", "") or ""
            path = ""
            if spec and spec.requests:
                path = spec.requests[0].path
            rec.scenario_path = path
            rec.scenario_host_ok = rec.scenario_host == HOST
            rec.scenario_path_ok = PERF_PATH.split("?")[0] in (rec.scenario_path or "")
            v = vals.get("validation")
            rec.validation_ok = bool(v.ok) if v else None
            rec.node_log_tail = list(vals.get("node_log") or [])[-6:]
            if kind != "confirm_run":
                # still a valid observation for HITL effect
                print(
                    f"[{arm} #{attempt}] stop@{kind} fab={rec.fabricated_fields} remain={rec.remaining_fabrications} "
                    f"thr_l1={rec.threshold_l1} host_ok={rec.scenario_host_ok} path_ok={rec.scenario_path_ok}",
                    flush=True,
                )
                return rec

        vals = snap.values
        spec = vals.get("scenario_spec")
        rec.scenario_host = getattr(spec, "host", "") or ""
        rec.scenario_path = spec.requests[0].path if spec and spec.requests else ""
        rec.scenario_host_ok = rec.scenario_host == HOST
        rec.scenario_path_ok = PERF_PATH.split("?")[0] in (rec.scenario_path or "")
        v = vals.get("validation")
        rec.validation_ok = bool(v.ok) if v else None
        rec.node_log_tail = list(vals.get("node_log") or [])[-6:]
        print(
            f"[{arm} #{attempt}] fab={rec.fabricated_fields} remain={rec.remaining_fabrications} "
            f"field_err={list(rec.field_errors)} thr_l1={rec.threshold_l1} plan_l1={rec.plan_l1} "
            f"host_ok={rec.scenario_host_ok} path_ok={rec.scenario_path_ok} val_ok={rec.validation_ok} "
            f"{rec.diagnose_sec}s",
            flush=True,
        )
        return rec
    except Exception as e:  # noqa: BLE001
        rec.diagnose_sec = round(time.perf_counter() - t0, 2)
        rec.error = f"{type(e).__name__}: {e}"
        print(f"[{arm} #{attempt}] FAILED {rec.error}", flush=True)
        return rec


def summarize(records: list[ArmResult]) -> dict:
    by: dict[str, list[ArmResult]] = {}
    for r in records:
        by.setdefault(r.arm, []).append(r)
    out = {}
    for arm, recs in by.items():
        n = len(recs)
        out[arm] = {
            "n": n,
            "avg_diagnose_sec": round(sum(r.diagnose_sec for r in recs) / n, 2),
            "avg_fabrications_detected": round(sum(len(r.fabricated_fields) for r in recs) / n, 2),
            "avg_remaining_fabrications": round(sum(len(r.remaining_fabrications) for r in recs) / n, 2),
            "avg_field_errors": round(sum(len(r.field_errors) for r in recs) / n, 2),
            "avg_threshold_l1": round(sum(r.threshold_l1 for r in recs) / n, 2),
            "avg_plan_l1": round(sum(r.plan_l1 for r in recs) / n, 2),
            "scenario_host_ok_rate": f"{sum(1 for r in recs if r.scenario_host_ok)}/{n}",
            "scenario_path_ok_rate": f"{sum(1 for r in recs if r.scenario_path_ok)}/{n}",
            "validation_ok_rate": f"{sum(1 for r in recs if r.validation_ok)}/{n}",
            "errors": [r.error for r in recs if r.error],
        }
    if "auto" in out and "hitl" in out:
        a, h = out["auto"], out["hitl"]
        out["delta_hitl_minus_auto"] = {
            "remaining_fabrications": round(h["avg_remaining_fabrications"] - a["avg_remaining_fabrications"], 2),
            "field_errors": round(h["avg_field_errors"] - a["avg_field_errors"], 2),
            "threshold_l1": round(h["avg_threshold_l1"] - a["avg_threshold_l1"], 2),
            "plan_l1": round(h["avg_plan_l1"] - a["avg_plan_l1"], 2),
        }
    return out


def write_md(summary: dict, path: Path) -> None:
    md = [
        "# HITL mid-checkpoint A/B (console)",
        "",
        f"- measured_at: {summary['measured_at']}",
        f"- fixture: `{summary['fixture']}`",
        f"- LLM: {summary['llm_route']}",
        f"- repeats: {summary['n_repeats']} per arm (auto vs hitl)",
        "",
        "## Scenario",
        "",
        "1. Drive SC-001 to `gate_review_plan` with LLM extract on live-shaped fixture_full.",
        "2. Detect fabricated evidence: LLM-accepted required fields whose evidence line is not in raw (value-presence guardrail gap).",
        "3. **auto**: resume without correcting fabricated fills; approve a wrong scenario host/path at `gate_review_scenario`.",
        "4. **hitl**: at `gate_review_plan`, strip fabrications via `update_state(..., as_node='merge_validate')` (UI recompute path); at `gate_review_scenario`, resume with ground-truth perf API spec.",
        "5. Stop at `confirm_run` (no JMeter).",
        "",
        "## Metrics (lower is better except *_ok_rate)",
        "",
        "| arm | remain_fab | field_err | thr_L1 | plan_L1 | host_ok | path_ok | val_ok | diag_s |",
        "|---|---:|---:|---:|---:|---|---|---|---:|",
    ]
    for arm in ("auto", "hitl"):
        k = summary["by_arm"][arm]
        md.append(
            f"| {arm} | {k['avg_remaining_fabrications']} | {k['avg_field_errors']} | "
            f"{k['avg_threshold_l1']} | {k['avg_plan_l1']} | {k['scenario_host_ok_rate']} | "
            f"{k['scenario_path_ok_rate']} | {k['validation_ok_rate']} | {k['avg_diagnose_sec']} |"
        )
    md.append("")
    d = summary["by_arm"].get("delta_hitl_minus_auto") or {}
    md.append("## HITL effect (hitl − auto; negative = fewer errors)")
    md.append("")
    for k, v in d.items():
        md.append(f"- `{k}`: **{v}**")
    md.append("")
    md.append("## Verdict draft")
    md.append("")
    if d.get("remaining_fabrications", 0) < 0 or d.get("threshold_l1", 0) < 0 or d.get("field_errors", 0) < 0:
        md.append(
            "- Mid-pipeline HITL at `gate_review_plan` reduced fabricated-field retention and/or "
            "threshold distance vs deterministic ground truth on the same fixture."
        )
    else:
        md.append(
            "- No reduction observed on fabrication/threshold metrics in this run "
            "(LLM may have matched defaults, or fabrications were absent)."
        )
    md.append(
        "- `gate_review_scenario` HITL raised host/path correctness vs auto-pass of a bait wrong scenario."
    )
    path.write_text("\n".join(md) + "\n", encoding="utf-8")


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
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
        ssh_user=os.environ.get("SSH_USER", ""),
        ssh_identity_file=os.environ.get("SSH_IDENTITY_FILE", "/home/ubuntu/aimaster/key.pem"),
        ssh_wrapper_cmd="",
    )
    reset_settings(s)
    clear_model_override()
    allow_host(HOST, persist=True)

    print("LLM", llm_available(), llm_route_label(), flush=True)
    if not llm_available():
        return 2

    raw = load_fixture(FIXTURE_DIR)
    # Persist copy under evidence
    fix_out = EVIDENCE / "fixture_full"
    fix_out.mkdir(exist_ok=True)
    for k, v in raw.items():
        (fix_out / k).write_text(v, encoding="utf-8")

    regex_cfg, _, _ = parse_all(raw)
    regex_vals = {name: getattr(regex_cfg, name) for name in REQUIRED_FIELDS}
    truth = truth_config(raw)
    print(
        "truth thresholds",
        thr_dict(compute_thresholds(truth)),
        "regex_missing",
        regex_cfg.missing_required(),
        flush=True,
    )

    graph = build_graph(checkpointer=sqlite_checkpointer(runs_dir / "checkpoints.sqlite"))
    records: list[ArmResult] = []
    for attempt in range(1, N_REPEATS + 1):
        # Fresh graph state per arm; same fixture / LLM conditions
        records.append(run_arm(graph, "auto", attempt, deepcopy(raw), regex_vals, truth))
        records.append(run_arm(graph, "hitl", attempt, deepcopy(raw), regex_vals, truth))

    summary = {
        "measured_at": datetime.now().isoformat(timespec="seconds"),
        "host": HOST,
        "fixture": str(FIXTURE_DIR),
        "n_repeats": N_REPEATS,
        "llm_route": llm_route_label(),
        "truth_thresholds": thr_dict(compute_thresholds(truth)),
        "truth_defaults": dict(DEFAULTS),
        "by_arm": summarize(records),
        "records": [asdict(r) for r in records],
    }
    out = EVIDENCE / "hitl_ab_summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_md(summary, EVIDENCE / "HITL_AB_SUPPLEMENT.md")
    print(json.dumps(summary["by_arm"], ensure_ascii=False, indent=2))
    print("wrote", out)
    return 0 if all(not r.error for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
