from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd

from analysis.anomalies import detect_anomalies
from analysis.gc_parser import parse_gc_text
from analysis.stats import aggregate_buckets, step_summary
from jmeter.jtl import failure_snapshot, load_jtl, step_from_thread_name
from jmeter.render import render_jmx, render_properties, scenario_from_url, split_path_query
from jmeter.validate import validate_jmx
from model.load_plan import generate_load_plan
from model.schemas import ScenarioInput, Thresholds
from tools.jmeter_runner import guess_step, parse_summary_line

THR = Thresholds(t_db_lock=50, t_queue=200, t_drop=300)


def _plan():
    return generate_load_plan(THR, hold_sec=5)[0]


def test_scenario_from_url_parses_everything():
    spec = scenario_from_url(ScenarioInput(kind="url", payload="https://api.corp.local:8443/v1/items?q=1", method="post", body='{"a":1}'))
    assert (spec.protocol, spec.host, spec.port) == ("https", "api.corp.local", 8443)
    req = spec.requests[0]
    assert req.method == "POST" and req.path == "/v1/items?q=1"
    assert req.headers["Content-Type"] == "application/json"
    assert req.assertions[0].expected == "200"


def test_scenario_from_url_drops_content_type_on_get():
    spec = scenario_from_url(ScenarioInput(
        kind="url", payload="http://3.35.11.139:8080/api/perf/db-delay?holdMs=300", method="GET",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    ))
    assert "Content-Type" not in spec.requests[0].headers
    assert spec.requests[0].headers["Accept"] == "application/json"


def test_render_jmx_is_well_formed_with_4_serial_threadgroups(tmp_path):
    plan = _plan()
    spec = scenario_from_url(ScenarioInput(kind="url", payload="http://localhost:8080/api", method="GET"))
    xml = render_jmx(spec, plan, "run1")
    root = ET.fromstring(xml)
    tgs = root.findall(".//ThreadGroup")
    assert len(tgs) == 4
    assert root.find(".//TestPlan/boolProp[@name='TestPlan.serialize_threadgroups']").text == "true"
    for i, tg in enumerate(tgs, start=1):
        assert tg.find("stringProp[@name='ThreadGroup.num_threads']").text == f"${{__P(step{i}.threads,{plan.steps[i-1].target_concurrency})}}"
        assert tg.find("stringProp[@name='ThreadGroup.duration']").text == f"${{__P(step{i}.duration,{plan.steps[i-1].duration_sec})}}"
    assert root.find(".//HTTPSamplerProxy/stringProp[@name='HTTPSampler.domain']").text == "${__P(target.host,localhost)}"

    props = render_properties(spec, plan, "run1")
    assert "step4.threads=" in props and "summariser.interval=5" in props and "target.host=localhost" in props

    p = tmp_path / "plan.jmx"
    p.write_text(xml, encoding="utf-8")
    res = validate_jmx(str(p), expected_steps=4, jmeter_bin=None, dry_run=False)
    assert res.ok, res.errors


def test_validate_jmx_rejects_broken_hashtree(tmp_path):
    p = tmp_path / "bad.jmx"
    p.write_text("<jmeterTestPlan><hashTree><TestPlan/></hashTree></jmeterTestPlan>", encoding="utf-8")
    res = validate_jmx(str(p), expected_steps=4, dry_run=False)
    assert not res.ok and res.errors


def test_summariser_regex_and_step_guess():
    line = "summary +   1234 in 00:00:05 =  246.8/s Avg:   120 Min:     5 Max:  1300 Err:    12 (0.97%) Active: 55 Started: 55 Finished: 0"
    ev = parse_summary_line(line)
    assert ev == {"count": 1234, "elapsed": "00:00:05", "tps": 246.8, "avg": 120, "min": 5, "max": 1300, "err": 12, "err_pct": 0.97, "active": 55}
    assert parse_summary_line("summary = 999 in 00:00:30 = 33.3/s Avg: 1 Min: 1 Max: 1 Err: 0 (0.00%)") is None
    plan = _plan()
    assert guess_step(40, plan) == plan.steps[0].name
    assert guess_step(360, plan) == plan.steps[3].name


def test_step_from_thread_name():
    assert step_from_thread_name("Step 3 (WAS Queueing) 1-17") == "Step 3 (WAS Queueing)"
    assert step_from_thread_name("weird") == "weird"


def _synthetic_jtl(tmp_path: Path) -> Path:
    rows = []
    t0 = 1_700_000_000_000
    for i in range(600):
        step = ["Step 1 (Normal)", "Step 2 (DB Saturation)", "Step 3 (WAS Queueing)", "Step 4 (Stress/Drop)"][i // 150]
        elapsed = 50 if i < 300 else (900 if i < 450 else 60)
        code, success = ("200", "true")
        if 160 <= i < 200:
            code, success = ("500", "false")
        if i >= 500:
            code, success = ("503", "false")
        rows.append(f"{t0 + i * 100},{elapsed},GET /api,{code},OK,{step} 1-{i % 50 + 1},text,{success},,120,80,{(i // 150 + 1) * 40},{(i // 150 + 1) * 40},http://h/api,{elapsed - 5},0,1")
    p = tmp_path / "result.jtl"
    p.write_text("timeStamp,elapsed,label,responseCode,responseMessage,threadName,dataType,success,failureMessage,bytes,sentBytes,grpThreads,allThreads,URL,Latency,IdleTime,Connect\n" + "\n".join(rows), encoding="utf-8")
    return p


def test_aggregate_steps_and_anomalies(tmp_path):
    df = load_jtl(str(_synthetic_jtl(tmp_path)))
    assert set(df["step"].unique()) == {"Step 1 (Normal)", "Step 2 (DB Saturation)", "Step 3 (WAS Queueing)", "Step 4 (Stress/Drop)"}
    buckets = aggregate_buckets(df, bucket_sec=5)
    assert not buckets.empty and {"tps", "p99_ms", "err_rate", "http_503", "active"} <= set(buckets.columns)
    summary = step_summary(df, _plan())
    assert [s["step"] for s in summary][0] == "Step 1 (Normal)"
    s4 = next(s for s in summary if s["step"].startswith("Step 4"))
    assert s4["http_503"] > 0 and s4["err_rate"] > 0
    kinds = {(a.kind, a.step) for a in detect_anomalies(buckets, None, bucket_sec=5)}
    assert ("latency_spike", "Step 3 (WAS Queueing)") in kinds
    assert ("error_rate", "Step 2 (DB Saturation)") in kinds
    assert any(k == "http_503" and s.startswith("Step 4") for k, s in kinds)


def test_gc_parser_jstat_and_log():
    jstat = "  S0     S1     E      O      M     CCS    YGC     YGCT    FGC    FGCT     GCT\n  0.00  12.50  40.00  55.00  95.00  90.00    120    1.200     3    0.900    2.100\n  0.00  12.50  80.00  70.00  95.00  90.00    121    1.210     4    1.500    2.710\n"
    df = parse_gc_text(jstat, start_ms=1000, interval_ms=5000)
    assert len(df) == 2 and df["ts_ms"].tolist() == [1000, 6000]
    assert df.iloc[1]["fgc"] == 4 and df.iloc[1]["pause_ms"] > 0
    log = "[2024-01-01T10:00:00.000+0900][info][gc] GC(10) Pause Young (Normal) (G1 Evacuation Pause) 100M->50M(512M) 12.345ms\n" \
          "[2024-01-01T10:00:05.000+0900][info][gc] GC(11) Pause Full (Allocation Failure) 500M->300M(512M) 812.0ms\n"
    df2 = parse_gc_text(log)
    assert len(df2) == 2 and df2["pause_ms"].max() == 812.0
    assert (df2["kind"] == "full").any()


def test_split_query_into_http_arguments():
    spec = scenario_from_url(ScenarioInput(
        kind="url", payload="http://3.35.11.139:8080/api/perf/db-delay?holdMs=300", method="GET",
    ))
    assert spec.requests[0].path == "/api/perf/db-delay?holdMs=300"
    assert split_path_query(spec.requests[0].path) == ("/api/perf/db-delay", [("holdMs", "300")])
    xml = render_jmx(spec, _plan(), "runq")
    root = ET.fromstring(xml)
    path = root.find(".//HTTPSamplerProxy/stringProp[@name='HTTPSampler.path']").text
    assert path == "/api/perf/db-delay"
    names = [e.find("stringProp[@name='Argument.name']").text for e in root.findall(".//elementProp[@elementType='HTTPArgument']")]
    values = [e.find("stringProp[@name='Argument.value']").text for e in root.findall(".//elementProp[@elementType='HTTPArgument']")]
    assert "holdMs" in names and "300" in values


def test_failure_snapshot_connect_timeout(tmp_path):
    p = tmp_path / "result.jtl"
    p.write_text(
        "timeStamp,elapsed,label,responseCode,responseMessage,threadName,dataType,success,failureMessage\n"
        "1,5000,GET /api,Non HTTP response code: java.net.ConnectException,"
        "Non HTTP response message: Connection timed out,Step 1 (Normal) 1-1,text,false,Connection timed out\n",
        encoding="utf-8",
    )
    snap = failure_snapshot(p)
    assert "ConnectException" in snap["hint"] or "Non HTTP" in snap["hint"]
    assert "localhost" in snap["hint"]
    assert snap["samples"]


def test_docker_uses_host_network_for_localhost(tmp_path, isolated_settings):
    from model.schemas import BundlePaths
    from tools.jmeter_runner import build_command

    bundle = BundlePaths(
        jmx=str(tmp_path / "plan.jmx"), properties=str(tmp_path / "user.properties"),
        run_sh=str(tmp_path / "run.sh"), plan_json=str(tmp_path / "plan.json"),
        readme=str(tmp_path / "README.md"),
    )
    cmd = build_command(tmp_path, bundle, "docker", isolated_settings, {"target.host": "localhost"})
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert "--network" in cmd and "host" in cmd
