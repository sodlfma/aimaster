#!/usr/bin/env python3
"""KPI measurement harness for mentor feedback: timed E2E runs with LLM on.

Measures:
  - diagnosis+plan wall time (ingest → review_plan interrupt)
  - build+run+collect+analyze wall time (approve → END)
  - JMeter CLI success (exit_code==0, result.jtl exists)
  - load+LLM report success (result.jtl + report.md + llm analysis source)
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
os.chdir(PKG)

from langgraph.types import Command

from config.settings import Settings, allow_host, get_settings, reset_settings
from graph.build import build_graph, sqlite_checkpointer
from graph.nodes.diagnose import new_run_id
from llm.models import clear_model_override, llm_available, llm_init_error, llm_route_label
from model.schemas import LoadPlan, LoadStep, TargetInfo

HOST = os.environ.get("KPI_HOST", "3.35.11.139")
PORT = int(os.environ.get("KPI_PORT", "8080"))
APACHE_HOME = "/software/apache/servers/webnode1"
TOMCAT_HOME = "/software/tomcat/servers/wasnode1"
HOLD_SEC = int(os.environ.get("KPI_HOLD_SEC", "15"))
N_RUNS = int(os.environ.get("KPI_N_RUNS", "3"))
SCALE = float(os.environ.get("KPI_SCALE", "0.25"))  # scale formula concurrency for sample-app safety
EVIDENCE = Path(os.environ.get("KPI_EVIDENCE_DIR", str(PKG / "kpi_evidence")))

HTTPD = r'''#
# Server-Pool Management (MPM specific)
#
<IfModule !mpm_netware_module>
    PidFile "/software/apache/servers/webnode1/logs/httpd.pid"
</IfModule>
<IfModule mpm_prefork_module>
    StartServers             5
    MinSpareServers          5
    MaxSpareServers         10
    MaxRequestWorkers      250
    MaxConnectionsPerChild   0
</IfModule>
<IfModule mpm_worker_module>
    StartServers             4
    ServerLimit             16
    MinSpareThreads        100
    MaxSpareThreads        250
    ThreadsPerChild         25
    MaxRequestWorkers      400
    MaxConnectionsPerChild   0
</IfModule>
<IfModule mpm_event_module>
    StartServers             4
    ServerLimit             16
    MinSpareThreads        100
    MaxSpareThreads        250
    ThreadsPerChild         25
    MaxRequestWorkers      400
    MaxConnectionsPerChild   0
</IfModule>
'''

TOMCAT = r'''##############################################
# instance    : wasnode1
SERVER_NAME="wasnode1"
TOMCAT_USER="webwas"
export CATALINA_HOME="/software/tomcat"
export CATALINA_BASE="$CATALINA_HOME/servers/$SERVER_NAME"
export CATALINA_OPTS="-Denv=product -Denv.servername=$SERVER_NAME -Djava.security.egd=file:/dev/./urandom"
export CATALINA_OUT="$CATALINA_BASE/logs/catalina.out"
export CATALINA_PID="$CATALINA_BASE/catalina.pid"
JAVA_OPTS="-D$SERVER_NAME"
JAVA_OPTS="$JAVA_OPTS -server"
JAVA_OPTS="$JAVA_OPTS -Xms1024m -Xmx1024m -XX:NewSize=384m -XX:MaxNewSize=384m -XX:MetaspaceSize=256m -XX:MaxMetaspaceSize=256m"
JAVA_OPTS="$JAVA_OPTS -XX:SurvivorRatio=8 -XX:ParallelGCThreads=4 -XX:+DisableExplicitGC -XX:+UseParallelGC -XX:-UseAdaptiveSizePolicy"
JAVA_OPTS="$JAVA_OPTS -Xlog:gc*,gc+heap=trace:file=$CATALINA_BASE/logs/gclog/gc_${SERVER_NAME}_$DATE.log:time"
JAVA_OPTS="$JAVA_OPTS -XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=$CATALINA_BASE/logs/heapdump"
JAVA_OPTS="$JAVA_OPTS -Djava.awt.headless=true"
JAVA_OPTS="$JAVA_OPTS -Djava.net.preferIPv4Stack=true"
JAVA_OPTS="$JAVA_OPTS -Dtomcat.port.shutdown=8105"
JAVA_OPTS="$JAVA_OPTS -Dtomcat.port.http=8180"
JAVA_OPTS="$JAVA_OPTS -Dtomcat.port.https=8443"
JAVA_OPTS="$JAVA_OPTS -Dtomcat.port.ajp=8109"
JAVA_OPTS="$JAVA_OPTS -Dtomcat.connector.address=0.0.0.0"
JAVA_OPTS="$JAVA_OPTS -Dtomcat.connector.secretRequired=false"
JAVA_OPTS="$JAVA_OPTS -Dtomcat.connector.URIEncoding=UTF-8"
JAVA_OPTS="$JAVA_OPTS -Dtomcat.connector.connectionTimeout=60000"
JAVA_OPTS="$JAVA_OPTS -Dtomcat.connector.keepAliveTimeout=60000"
JAVA_OPTS="$JAVA_OPTS -Dtomcat.connector.maxPostSize=2097152"
JAVA_OPTS="$JAVA_OPTS -Dtomcat.connector.maxThreads=200"
JAVA_OPTS="$JAVA_OPTS -Dtomcat.connector.minSpareThreads=200"
export JAVA_OPTS
'''

SCENARIOS = [
    {
        "name": "db-delay-breakpoint",
        "kind": "url",
        "path": "/api/perf/db-delay?holdMs=300",
        "method": "GET",
        "think_time_ms": 0,
        "scenario_type": "breakpoint",
        "use_llm_scenario": False,
        "nl": "",
    },
    {
        "name": "alloc-memory-gc",
        "kind": "nl",
        "path": "/api/perf/alloc-memory?allocMb=20&leak=false",
        "method": "GET",
        "think_time_ms": 0,
        "scenario_type": "breakpoint",
        "use_llm_scenario": True,
        "nl": (
            f"GET http://{HOST}:{PORT}/api/perf/alloc-memory?allocMb=20&leak=false "
            "헤더 Accept: application/json 과 Content-Type: application/json 으로 "
            "단기 객체 할당 Young GC 유도 부하 시나리오를 만들어라. 요청 1개면 충분하다."
        ),
    },
]


@dataclass
class PhaseTimes:
    diagnose_sec: float = 0.0
    build_run_analyze_sec: float = 0.0
    total_sec: float = 0.0


@dataclass
class RunRecord:
    attempt: int
    scenario: str
    run_id: str
    ok_jmeter: bool = False
    ok_report: bool = False
    ok_llm_analyze: bool = False
    ok_llm_interpret: bool = False
    jmeter_exit: int | None = None
    thresholds: dict = field(default_factory=dict)
    load_plan_users: list[int] = field(default_factory=list)
    scaled_users: list[int] = field(default_factory=list)
    times: PhaseTimes = field(default_factory=PhaseTimes)
    artifacts: dict = field(default_factory=dict)
    error: str = ""
    llm_skipped: list[str] = field(default_factory=list)
    diagnosis_source: str = ""
    analysis_source: str = ""


def _raw_inputs() -> dict[str, str]:
    return {
        "httpd-mpm.conf": HTTPD,
        "tomcat.env": TOMCAT,
        "web_mon_cron.env": (
            f"HOST={HOST}\nAPACHE_HOME={APACHE_HOME}\nTOMCAT_HOME={TOMCAT_HOME}\n"
            'INSTANCES="wasnode1"\n'
        ),
    }


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
                ramp_up_sec=max(1, min(s.ramp_up_sec, 10)),
                hold_sec=hold_sec,
                expected_symptom=s.expected_symptom,
            )
        )
    # keep strictly increasing
    for i in range(1, len(steps)):
        if steps[i].target_concurrency <= steps[i - 1].target_concurrency:
            steps[i].target_concurrency = steps[i - 1].target_concurrency + 1
    out = LoadPlan(
        steps=steps,
        scenario_type=plan.scenario_type,
        scenario_label=plan.scenario_label,
        purpose=plan.purpose + f" [KPI scale×{scale}, hold={hold_sec}s]",
        n_target=plan.n_target,
        verification_points=list(plan.verification_points),
    )
    return out.recompute_total()


def _artifact_map(run_dir: Path) -> dict:
    names = [
        "plan.jmx", "user.properties", "run.sh", "plan.json", "README.md", "bundle.zip",
        "result.jtl", "buckets.csv", "gc.csv", "step_summary.json", "report.md",
        "state.json", "llm_calls.jsonl",
    ]
    out = {}
    for n in names:
        p = run_dir / n
        out[n] = {"exists": p.exists(), "bytes": p.stat().st_size if p.exists() else 0}
    report_dir = run_dir / "report"
    out["report/"] = {"exists": report_dir.is_dir(), "bytes": sum(f.stat().st_size for f in report_dir.rglob('*') if f.is_file()) if report_dir.is_dir() else 0}
    return out


def run_once(graph, attempt: int, scenario: dict, scale: float, hold_sec: int) -> RunRecord:
    run_id = new_run_id()
    rec = RunRecord(attempt=attempt, scenario=scenario["name"], run_id=run_id)
    cfg = {"configurable": {"thread_id": run_id}}
    t_all = time.perf_counter()
    try:
        t0 = time.perf_counter()
        snap, _ = _drive(
            graph,
            cfg,
            {
                "run_id": run_id,
                "raw_inputs": _raw_inputs(),
                "target_info": TargetInfo(host=HOST, apache_home=APACHE_HOME, tomcat_home=TOMCAT_HOME, instances=["wasnode1"]),
                "llm_enabled": True,
                "options": {
                    "interpret": True,
                    "ramp_scale": 1.0,
                    "hold_sec": hold_sec,
                    "bucket_sec": 5,
                    "dry_run": True,
                    "scenario_type": scenario["scenario_type"],
                    "n_target": 40,
                    "spike_ramp_sec": 2,
                },
            },
        )
        if _interrupt_kind(snap) != "review_plan":
            raise RuntimeError(f"expected review_plan, got {_interrupt_kind(snap)} next={snap.next}")
        rec.times.diagnose_sec = round(time.perf_counter() - t0, 2)
        vals = snap.values
        thr = vals.get("thresholds")
        plan: LoadPlan = vals["load_plan"]
        rec.thresholds = thr.model_dump() if thr else {}
        rec.load_plan_users = [s.target_concurrency for s in plan.steps]
        rec.diagnosis_source = getattr(vals.get("diagnosis_report"), "source", "") or ""
        rec.ok_llm_interpret = rec.diagnosis_source == "llm"
        scaled = _scale_plan(plan, scale, hold_sec)
        rec.scaled_users = [s.target_concurrency for s in scaled.steps]

        if scenario["use_llm_scenario"]:
            si = {
                "kind": "nl",
                "payload": scenario["nl"],
                "headers": {"Accept": "application/json", "Content-Type": "application/json"},
                "host": HOST,
                "port": PORT,
                "protocol": "http",
                "think_time_ms": scenario["think_time_ms"],
            }
        else:
            si = {
                "kind": "url",
                "payload": f"http://{HOST}:{PORT}{scenario['path']}",
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
        spec = snap.values["scenario_spec"]
        # force exact path/port for KPI reproducibility even if LLM designed scenario
        from model.schemas import Assertion, RequestSpec, ScenarioSpec

        forced = ScenarioSpec(
            name=spec.name or scenario["name"],
            protocol="http",
            host=HOST,
            port=PORT,
            default_headers={"Accept": "application/json", "Content-Type": "application/json"},
            requests=[
                RequestSpec(
                    label=scenario["name"],
                    method=scenario["method"],
                    path=scenario["path"],
                    headers={},
                    body=None,
                    assertions=[Assertion(kind="response_code", expected="200")],
                )
            ],
            think_time_ms=scenario["think_time_ms"],
        )
        snap, _ = _drive(graph, cfg, Command(resume={"scenario_spec": forced.model_dump()}))
        if _interrupt_kind(snap) != "confirm_run":
            raise RuntimeError(f"expected confirm_run, got {_interrupt_kind(snap)} validation={snap.values.get('validation')}")
        if not snap.values["validation"].ok:
            raise RuntimeError(f"JMX validation failed: {snap.values['validation'].errors}")

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

        vals = snap.values
        rr = vals.get("run_result")
        rec.jmeter_exit = getattr(rr, "exit_code", None)
        run_dir = Path(get_settings().runs_dir) / run_id
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
            rec.error = f"graph not at END, next={snap.next}"
        print(
            f"[run {attempt}] {scenario['name']} run_id={run_id} "
            f"diagnose={rec.times.diagnose_sec}s build_run={rec.times.build_run_analyze_sec}s "
            f"jmeter={rec.ok_jmeter} report={rec.ok_report} llm={rec.analysis_source}",
            flush=True,
        )
        return rec
    except Exception as e:  # noqa: BLE001
        rec.times.total_sec = round(time.perf_counter() - t_all, 2)
        rec.error = f"{type(e).__name__}: {e}"
        run_dir = Path(get_settings().runs_dir) / run_id
        if run_dir.exists():
            rec.artifacts = _artifact_map(run_dir)
        print(f"[run {attempt}] FAILED {scenario['name']}: {rec.error}", flush=True)
        return rec


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    jmeter_home = os.environ.get("JMETER_HOME", "/opt/apache-jmeter")
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
        jmeter_home=jmeter_home,
        runs_dir=str(runs_dir),
        allowed_hosts_file=str(allowed),
        max_local_threads=1000,
        summariser_interval=5,
    )
    reset_settings(s)
    clear_model_override()
    allow_host(HOST, persist=True)

    print("LLM available:", llm_available(), "route:", llm_route_label(), "err:", llm_init_error() or "-")
    if not llm_available():
        print("FATAL: LLM required for this KPI run", file=sys.stderr)
        return 2

    ckpt = runs_dir / "checkpoints.sqlite"
    graph = build_graph(checkpointer=sqlite_checkpointer(ckpt))

    records: list[RunRecord] = []
    # Primary KPI scenario repeated N times
    primary = SCENARIOS[0]
    for i in range(1, N_RUNS + 1):
        records.append(run_once(graph, i, primary, SCALE, HOLD_SEC))
        # reset leak/memory between runs if previous left residue (best-effort)
        try:
            import urllib.request

            urllib.request.urlopen(
                urllib.request.Request(f"http://{HOST}:{PORT}/api/perf/reset-memory", method="POST"),
                timeout=10,
            ).read()
        except Exception:  # noqa: BLE001
            pass

    # One LLM-designed scenario run (alloc-memory)
    records.append(run_once(graph, N_RUNS + 1, SCENARIOS[1], SCALE, HOLD_SEC))

    summary = {
        "measured_at": datetime.now().isoformat(timespec="seconds"),
        "host": HOST,
        "port": PORT,
        "hold_sec": HOLD_SEC,
        "scale": SCALE,
        "n_primary": N_RUNS,
        "llm_route": llm_route_label(),
        "records": [
            {
                **{k: v for k, v in asdict(r).items() if k != "times"},
                "times": asdict(r.times),
            }
            for r in records
        ],
    }
    primary_recs = [r for r in records if r.scenario == primary["name"]]
    jmeter_ok = sum(1 for r in primary_recs if r.ok_jmeter)
    report_ok = sum(1 for r in primary_recs if r.ok_report)
    diag_secs = [r.times.diagnose_sec for r in primary_recs if not r.error]
    build_secs = [r.times.build_run_analyze_sec for r in primary_recs if r.ok_jmeter]
    summary["kpi"] = {
        "diagnose_minutes_avg": round((sum(diag_secs) / len(diag_secs) / 60.0), 2) if diag_secs else None,
        "diagnose_seconds_avg": round(sum(diag_secs) / len(diag_secs), 1) if diag_secs else None,
        "build_run_collect_minutes_avg": round((sum(build_secs) / len(build_secs) / 60.0), 2) if build_secs else None,
        "build_run_collect_seconds_avg": round(sum(build_secs) / len(build_secs), 1) if build_secs else None,
        "jmeter_cli_success": f"{jmeter_ok}/{len(primary_recs)}",
        "load_and_llm_report_success": f"{report_ok}/{len(primary_recs)}",
        "formula_users_example": primary_recs[0].load_plan_users if primary_recs else [],
        "scaled_users_example": primary_recs[0].scaled_users if primary_recs else [],
        "note_scale": (
            f"샘플 앱 안전 및 반복 측정을 위해 공식 산출 동시사용자에 ×{SCALE} 배율을 적용하고 "
            f"hold={HOLD_SEC}s 로 측정. 원본 공식 수치는 load_plan_users 에 기록."
        ),
    }
    out = EVIDENCE / "kpi_summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary["kpi"], ensure_ascii=False, indent=2))
    print("wrote", out)
    return 0 if jmeter_ok == len(primary_recs) and report_ok == len(primary_recs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
