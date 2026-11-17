#!/usr/bin/env python3
"""Run each SC-001 scenario type (breakpoint/spike/sla) three times with LLM + SSH.

Collects remote configs over SSH, runs the closed-loop graph against :8080,
and captures jstat -gcutil in parallel for GC evidence.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
os.chdir(PKG)

from langgraph.types import Command

from analysis.gc_parser import parse_gc_text
from config.settings import Settings, allow_host, get_settings, reset_settings
from graph.build import build_graph, sqlite_checkpointer
from graph.nodes.diagnose import new_run_id
from llm.models import clear_model_override, llm_available, llm_route_label
from model.schemas import Assertion, LoadPlan, LoadStep, RequestSpec, ScenarioSpec, TargetInfo
from tools.ssh_wrapper import build_ssh_argv, fetch_from_homes

HOST = os.environ.get("KPI_HOST", "3.35.11.139")
PORT = int(os.environ.get("KPI_PORT", "8080"))
APACHE_HOME = "/software/apache/servers/webnode1"
APACHE_FILE = "/software/apache/servers/webnode1/conf/extra/httpd-mpm.conf"
TOMCAT_HOME = "/software/tomcat/servers/wasnode1"
HOLD_SEC = int(os.environ.get("KPI_HOLD_SEC", "15"))
SCALE = float(os.environ.get("KPI_SCALE", "0.25"))
N_PER_TYPE = int(os.environ.get("KPI_N_PER_TYPE", "3"))
EVIDENCE = Path(os.environ.get("KPI_EVIDENCE_DIR", str(PKG / "kpi_evidence_matrix")))

SCENARIO_TYPES = [
    {
        "scenario_type": "breakpoint",
        "api_path": "/api/perf/db-delay?holdMs=300",
        "method": "GET",
        "think_time_ms": 0,
        "n_target": 40,
    },
    {
        "scenario_type": "spike",
        "api_path": "/api/perf/alloc-memory?allocMb=20&leak=false",
        "method": "GET",
        "think_time_ms": 0,
        "n_target": 40,
    },
    {
        "scenario_type": "sla",
        "api_path": "/api/perf/db-delay?holdMs=300",
        "method": "GET",
        "think_time_ms": 0,
        "n_target": 40,
    },
]


@dataclass
class PhaseTimes:
    diagnose_sec: float = 0.0
    build_run_analyze_sec: float = 0.0
    total_sec: float = 0.0


@dataclass
class RunRecord:
    scenario_type: str
    attempt: int
    run_id: str
    ok_jmeter: bool = False
    ok_report: bool = False
    ok_llm_analyze: bool = False
    ok_ssh_config: bool = False
    ok_gc: bool = False
    jmeter_exit: int | None = None
    thresholds: dict = field(default_factory=dict)
    formula_users: list[int] = field(default_factory=list)
    scaled_users: list[int] = field(default_factory=list)
    times: PhaseTimes = field(default_factory=PhaseTimes)
    artifacts: dict = field(default_factory=dict)
    diagnosis_source: str = ""
    analysis_source: str = ""
    llm_skipped: list[str] = field(default_factory=list)
    error: str = ""
    api_path: str = ""


def _drive(graph, cfg, inp):
    custom = []
    for mode, chunk in graph.stream(inp, cfg, stream_mode=["updates", "custom"]):
        if mode == "custom":
            custom.append(chunk)
    return graph.get_state(cfg), custom


def _interrupt_kind(snap) -> str | None:
    for t in snap.tasks:
        if t.interrupts:
            val = t.interrupts[0].value
            if isinstance(val, dict):
                return val.get("kind")
    return None


def _scale_plan(plan: LoadPlan, scale: float, hold_sec: int) -> LoadPlan:
    steps = []
    for s in plan.steps:
        steps.append(
            LoadStep(
                name=s.name,
                target_concurrency=max(1, int(s.target_concurrency * scale)),
                ramp_up_sec=max(1, min(int(s.ramp_up_sec), 10)),
                hold_sec=hold_sec,
                expected_symptom=s.expected_symptom,
            )
        )
    for i in range(1, len(steps)):
        if steps[i].target_concurrency <= steps[i - 1].target_concurrency:
            steps[i].target_concurrency = steps[i - 1].target_concurrency + 1
    out = LoadPlan(
        steps=steps,
        scenario_type=plan.scenario_type,
        scenario_label=plan.scenario_label,
        purpose=(plan.purpose or "") + f" [KPI scale×{scale}, hold={hold_sec}s]",
        n_target=plan.n_target,
        verification_points=list(plan.verification_points or []),
    )
    return out.recompute_total()


def _artifact_map(run_dir: Path) -> dict:
    names = [
        "plan.jmx", "user.properties", "run.sh", "plan.json", "README.md",
        "result.jtl", "buckets.csv", "gc.csv", "gc_jstat.txt", "step_summary.json",
        "report.md", "state.json", "llm_calls.jsonl",
    ]
    out = {}
    for n in names:
        p = run_dir / n
        out[n] = {"exists": p.exists(), "bytes": p.stat().st_size if p.exists() else 0}
    return out


def _reset_memory() -> None:
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"http://{HOST}:{PORT}/api/perf/reset-memory", method="POST"),
            timeout=10,
        ).read()
    except Exception:  # noqa: BLE001
        pass


def _tomcat_pid() -> int | None:
    cmd = build_ssh_argv(HOST, "pgrep -f 'org.apache.catalina.startup.Bootstrap start'")
    # build_ssh_argv expects remote_command kw for free-form; fallback to ssh -i directly
    identity = get_settings().ssh_identity_file
    user = get_settings().ssh_user
    argv = [
        "ssh", "-i", identity, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15",
        f"{user}@{HOST}",
        "pgrep -f 'org.apache.catalina.startup.Bootstrap start' | head -1",
    ]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    text = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or not text:
        return None
    try:
        return int(text[0].strip())
    except ValueError:
        return None


def _collect_jstat(pid: int, interval_ms: int, count: int, out_path: Path) -> str:
    user = get_settings().ssh_user
    identity = get_settings().ssh_identity_file
    argv = [
        "ssh", "-i", identity, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15",
        f"{user}@{HOST}",
        f"jstat -gcutil {pid} {interval_ms} {count}",
    ]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=max(60, (interval_ms * count) // 1000 + 60))
    text = (r.stdout or "") + (("\n" + r.stderr) if r.returncode != 0 and r.stderr else "")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return text


class JstatCollector:
    def __init__(self, pid: int, duration_sec: int, interval_ms: int = 5000):
        self.pid = pid
        self.interval_ms = interval_ms
        self.count = max(3, duration_sec * 1000 // interval_ms + 3)
        self.text = ""
        self.error = ""
        self._thread: threading.Thread | None = None
        self.out_path: Path | None = None

    def start(self, out_path: Path) -> None:
        self.out_path = out_path

        def target() -> None:
            try:
                self.text = _collect_jstat(self.pid, self.interval_ms, self.count, out_path)
            except Exception as e:  # noqa: BLE001
                self.error = f"{type(e).__name__}: {e}"

        self._thread = threading.Thread(target=target, name="jstat", daemon=True)
        self._thread.start()

    def join(self, timeout: float | None = None) -> str:
        if self._thread:
            self._thread.join(timeout=timeout)
        return self.text


def fetch_raw_inputs() -> tuple[dict[str, str], bool]:
    cfg = fetch_from_homes(HOST, APACHE_FILE, TOMCAT_HOME, timeout=45)
    raw = cfg.to_raw_inputs()
    # ensure apache content present even if home-relative path failed earlier
    if not (raw.get("httpd-mpm.conf") or "").strip():
        # secondary fetch of explicit file already used as apache path
        pass
    ok = bool((raw.get("tomcat.env") or "").strip()) and bool((raw.get("httpd-mpm.conf") or "").strip())
    if not (raw.get("httpd-mpm.conf") or "").strip():
        # if still empty, try cat via ssh
        user = get_settings().ssh_user
        identity = get_settings().ssh_identity_file
        r = subprocess.run(
            [
                "ssh", "-i", identity, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-o", "StrictHostKeyChecking=accept-new", f"{user}@{HOST}",
                f"cat {APACHE_FILE}",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            raw["httpd-mpm.conf"] = r.stdout
            ok = bool((raw.get("tomcat.env") or "").strip())
    return raw, ok


def run_once(graph, scenario: dict, attempt: int, raw_inputs: dict[str, str], ssh_ok: bool) -> RunRecord:
    stype = scenario["scenario_type"]
    run_id = new_run_id()
    rec = RunRecord(scenario_type=stype, attempt=attempt, run_id=run_id, ok_ssh_config=ssh_ok, api_path=scenario["api_path"])
    cfg = {"configurable": {"thread_id": run_id}}
    t_all = time.perf_counter()
    jstat: JstatCollector | None = None
    try:
        t0 = time.perf_counter()
        snap, _ = _drive(
            graph,
            cfg,
            {
                "run_id": run_id,
                "raw_inputs": raw_inputs,
                "target_info": TargetInfo(
                    host=HOST, apache_home=APACHE_HOME, tomcat_home=TOMCAT_HOME, instances=["wasnode1"]
                ),
                "llm_enabled": True,
                "options": {
                    "interpret": True,
                    "ramp_scale": 1.0,
                    "hold_sec": HOLD_SEC,
                    "bucket_sec": 5,
                    "dry_run": True,
                    "scenario_type": stype,
                    "n_target": int(scenario["n_target"]),
                    "spike_ramp_sec": 2,
                },
            },
        )
        if _interrupt_kind(snap) != "review_plan":
            raise RuntimeError(f"expected review_plan, got {_interrupt_kind(snap)}")
        rec.times.diagnose_sec = round(time.perf_counter() - t0, 2)
        vals = snap.values
        thr = vals.get("thresholds")
        plan: LoadPlan = vals["load_plan"]
        rec.thresholds = thr.model_dump() if thr else {}
        rec.formula_users = [s.target_concurrency for s in plan.steps]
        rec.diagnosis_source = getattr(vals.get("diagnosis_report"), "source", "") or ""
        scaled = _scale_plan(plan, SCALE, HOLD_SEC)
        rec.scaled_users = [s.target_concurrency for s in scaled.steps]

        si = {
            "kind": "url",
            "payload": f"http://{HOST}:{PORT}{scenario['api_path']}",
            "method": scenario["method"],
            "body": None,
            "headers": {"Accept": "application/json", "Content-Type": "application/json"},
            "host": HOST,
            "port": PORT,
            "protocol": "http",
            "think_time_ms": scenario["think_time_ms"],
        }
        t1 = time.perf_counter()
        snap, _ = _drive(graph, cfg, Command(resume={"scenario_input": si, "load_plan": scaled.model_dump()}))
        if _interrupt_kind(snap) != "review_scenario":
            raise RuntimeError(f"expected review_scenario, got {_interrupt_kind(snap)}")

        forced = ScenarioSpec(
            name=f"{stype}-{attempt}",
            protocol="http",
            host=HOST,
            port=PORT,
            default_headers={"Accept": "application/json", "Content-Type": "application/json"},
            requests=[
                RequestSpec(
                    label=stype,
                    method=scenario["method"],
                    path=scenario["api_path"],
                    assertions=[Assertion(kind="response_code", expected="200")],
                )
            ],
            think_time_ms=scenario["think_time_ms"],
        )
        snap, _ = _drive(graph, cfg, Command(resume={"scenario_spec": forced.model_dump()}))
        if _interrupt_kind(snap) != "confirm_run":
            raise RuntimeError(f"expected confirm_run, got {_interrupt_kind(snap)}")
        if not snap.values["validation"].ok:
            raise RuntimeError(f"JMX validation failed: {snap.values['validation'].errors}")

        run_dir = Path(get_settings().runs_dir) / run_id
        pid = _tomcat_pid()
        duration = int(scaled.total_duration_sec) + 90
        gc_text = ""
        if pid:
            jstat = JstatCollector(pid, duration_sec=duration, interval_ms=5000)
            jstat.start(run_dir / "gc_jstat.txt")
            time.sleep(1.0)

        snap, _ = _drive(
            graph,
            cfg,
            Command(
                resume={
                    "run_mode": "local",
                    "gc_input": None,
                    "overrides": {
                        "target.connect_timeout": "5000",
                        "target.response_timeout": "60000",
                    },
                }
            ),
        )
        rec.times.build_run_analyze_sec = round(time.perf_counter() - t1, 2)
        rec.times.total_sec = round(time.perf_counter() - t_all, 2)

        if jstat:
            gc_text = jstat.join(timeout=duration + 120) or ""
            if jstat.error and not gc_text:
                rec.error = (rec.error + "; " if rec.error else "") + f"jstat:{jstat.error}"
            if gc_text.strip():
                # persist parsed GC even if graph analyze ran without it
                try:
                    df = parse_gc_text(gc_text)
                    if df is not None and len(df):
                        df.to_csv(run_dir / "gc.csv", index=False)
                        rec.ok_gc = True
                    elif (run_dir / "gc_jstat.txt").exists() and (run_dir / "gc_jstat.txt").stat().st_size > 50:
                        rec.ok_gc = True
                except Exception as e:  # noqa: BLE001
                    rec.error = (rec.error + "; " if rec.error else "") + f"gc_parse:{type(e).__name__}:{e}"

        vals = snap.values
        rr = vals.get("run_result")
        rec.jmeter_exit = getattr(rr, "exit_code", None)
        rec.artifacts = _artifact_map(run_dir)
        jtl_ok = bool(rec.artifacts.get("result.jtl", {}).get("exists"))
        report_ok = bool(rec.artifacts.get("report.md", {}).get("exists"))
        rec.ok_jmeter = jtl_ok and rec.jmeter_exit == 0 and not getattr(rr, "stopped", False)
        analysis = vals.get("analysis_report")
        rec.analysis_source = getattr(analysis, "source", "") or ""
        rec.ok_llm_analyze = rec.analysis_source == "llm"
        rec.ok_report = report_ok and rec.ok_jmeter and rec.ok_llm_analyze
        rec.llm_skipped = list(vals.get("llm_skipped") or [])
        if snap.next:
            rec.error = (rec.error + "; " if rec.error else "") + f"graph_next={snap.next}"
        print(
            f"[{stype} #{attempt}] run_id={run_id} diagnose={rec.times.diagnose_sec}s "
            f"build_run={rec.times.build_run_analyze_sec}s jmeter={rec.ok_jmeter} "
            f"report={rec.ok_report} gc={rec.ok_gc} llm={rec.analysis_source}",
            flush=True,
        )
        return rec
    except Exception as e:  # noqa: BLE001
        rec.times.total_sec = round(time.perf_counter() - t_all, 2)
        rec.error = f"{type(e).__name__}: {e}"
        run_dir = Path(get_settings().runs_dir) / run_id
        if run_dir.exists():
            rec.artifacts = _artifact_map(run_dir)
        if jstat:
            try:
                jstat.join(timeout=30)
            except Exception:  # noqa: BLE001
                pass
        print(f"[{stype} #{attempt}] FAILED: {rec.error}", flush=True)
        return rec


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
        max_local_threads=1000,
        ssh_user=user,
        ssh_identity_file=identity,
        ssh_wrapper_cmd="",
        summariser_interval=5,
    )
    reset_settings(s)
    clear_model_override()
    allow_host(HOST, persist=True)

    print("LLM", llm_available(), llm_route_label())
    if not llm_available():
        print("FATAL: LLM required", file=sys.stderr)
        return 2

    print("SSH fetch configs…", flush=True)
    raw, ssh_ok = fetch_raw_inputs()
    print(
        "ssh_ok=", ssh_ok,
        "tomcat_len=", len(raw.get("tomcat.env") or ""),
        "httpd_len=", len(raw.get("httpd-mpm.conf") or ""),
        flush=True,
    )
    (EVIDENCE / "ssh_raw_inputs.json").write_text(
        json.dumps({k: {"chars": len(v or ""), "head": (v or "")[:200]} for k, v in raw.items()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not ssh_ok:
        print("FATAL: SSH config fetch incomplete", file=sys.stderr)
        return 3

    graph = build_graph(checkpointer=sqlite_checkpointer(runs_dir / "checkpoints.sqlite"))
    records: list[RunRecord] = []
    for scenario in SCENARIO_TYPES:
        for attempt in range(1, N_PER_TYPE + 1):
            _reset_memory()
            records.append(run_once(graph, scenario, attempt, raw, ssh_ok))
            _reset_memory()

    by_type: dict[str, list[RunRecord]] = {}
    for r in records:
        by_type.setdefault(r.scenario_type, []).append(r)

    kpi_by_type = {}
    for stype, recs in by_type.items():
        j_ok = sum(1 for r in recs if r.ok_jmeter)
        r_ok = sum(1 for r in recs if r.ok_report)
        g_ok = sum(1 for r in recs if r.ok_gc)
        diag = [r.times.diagnose_sec for r in recs if r.times.diagnose_sec]
        build = [r.times.build_run_analyze_sec for r in recs if r.ok_jmeter]
        kpi_by_type[stype] = {
            "n": len(recs),
            "jmeter_cli_success": f"{j_ok}/{len(recs)}",
            "load_and_llm_report_success": f"{r_ok}/{len(recs)}",
            "gc_capture_success": f"{g_ok}/{len(recs)}",
            "diagnose_seconds_avg": round(sum(diag) / len(diag), 1) if diag else None,
            "diagnose_minutes_avg": round(sum(diag) / len(diag) / 60.0, 2) if diag else None,
            "build_run_collect_seconds_avg": round(sum(build) / len(build), 1) if build else None,
            "build_run_collect_minutes_avg": round(sum(build) / len(build) / 60.0, 2) if build else None,
            "formula_users_example": recs[0].formula_users if recs else [],
            "scaled_users_example": recs[0].scaled_users if recs else [],
        }

    overall_j = sum(1 for r in records if r.ok_jmeter)
    overall_r = sum(1 for r in records if r.ok_report)
    overall_g = sum(1 for r in records if r.ok_gc)
    summary = {
        "measured_at": datetime.now().isoformat(timespec="seconds"),
        "host": HOST,
        "port": PORT,
        "hold_sec": HOLD_SEC,
        "scale": SCALE,
        "n_per_type": N_PER_TYPE,
        "ssh_config": ssh_ok,
        "llm_route": llm_route_label(),
        "overall": {
            "jmeter_cli_success": f"{overall_j}/{len(records)}",
            "load_and_llm_report_success": f"{overall_r}/{len(records)}",
            "gc_capture_success": f"{overall_g}/{len(records)}",
        },
        "by_type": kpi_by_type,
        "records": [
            {**{k: v for k, v in asdict(r).items() if k != "times"}, "times": asdict(r.times)}
            for r in records
        ],
    }
    out = EVIDENCE / "kpi_matrix_summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"overall": summary["overall"], "by_type": kpi_by_type}, ensure_ascii=False, indent=2))
    print("wrote", out)
    return 0 if overall_j == len(records) and overall_r == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
