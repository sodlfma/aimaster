from __future__ import annotations

from model.load_plan import generate_load_plan
from model.schemas import ParsedConfig, Thresholds
from model.thresholds import compute_thresholds, resolve_with_defaults, rule_antipatterns
from parsers import parse_all
from parsers.httpd_mpm import parse_httpd_mpm
from parsers.tomcat_env import parse_tomcat_env


def test_tomcat_env_extracts_java_opts(raw_inputs):
    found, conflicts = parse_tomcat_env(raw_inputs["tomcat.env"])
    assert found["tomcat_max_threads"][0] == 200
    assert found["tomcat_accept_count"][0] == 100
    assert found["dbcp_max_total"][0] == 50
    assert found["dbcp_max_wait_ms"][0] == 10000
    assert found["jvm_xmx_mb"][0] == 2048
    assert found["gc_type"][0].lower().startswith("g1")
    assert "maxThreads=200" in found["tomcat_max_threads"][1]
    assert conflicts == []


def test_tomcat_env_conflict_detected():
    text = 'JAVA_OPTS="-Dtomcat.connector.maxThreads=200"\nJAVA_OPTS="$JAVA_OPTS -Dtomcat.connector.maxThreads=300"\n'
    _, conflicts = parse_tomcat_env(text)
    assert conflicts == ["tomcat_max_threads"]


def test_tomcat_port_skips_jmx_and_prefers_connector():
    text = (
        'JAVA_OPTS="-Dcom.sun.management.jmxremote.port=1099"\n'
        'JAVA_OPTS="$JAVA_OPTS -Dtomcat.connector.port=8080"\n'
        'JAVA_OPTS="$JAVA_OPTS -Dtomcat.shutdown.port=8005"\n'
    )
    found, conflicts = parse_tomcat_env(text)
    assert found["tomcat_port"][0] == 8080
    assert "jmxremote" not in found["tomcat_port"][1]
    assert "tomcat_port" not in conflicts


def test_httpd_mpm_picks_loaded_module_block(raw_inputs):
    found = parse_httpd_mpm(raw_inputs["httpd-mpm.conf"])
    assert found["mpm_module"][0] == "event"
    assert found["max_request_workers"][0] == 400
    assert found["threads_per_child"][0] == 25


def test_parse_all_merges_sources(raw_inputs):
    cfg, target, warnings = parse_all(raw_inputs)
    assert cfg.source == "regex"
    assert cfg.missing_required() == []
    assert cfg.confidence >= 0.9
    assert target.host == "was01.corp.local"
    assert target.instances == ["inst01", "inst02"]


def test_thresholds_formulas():
    cfg = ParsedConfig(tomcat_max_threads=200, tomcat_accept_count=100, dbcp_max_total=50, max_request_workers=400)
    thr = compute_thresholds(cfg)
    assert (thr.t_db_lock, thr.t_queue, thr.t_drop, thr.web_cap) == (50, 200, 300, 400)
    assert "t_drop" in thr.formulas


def test_resolve_with_defaults_fills_missing():
    cfg, warnings = resolve_with_defaults(ParsedConfig())
    assert cfg.tomcat_max_threads == 200 and cfg.dbcp_max_total == 50
    assert len(warnings) == 3


def test_load_plan_is_monotonic_and_sized():
    plan, warnings = generate_load_plan(Thresholds(t_db_lock=50, t_queue=200, t_drop=300), ramp_scale=1.0, hold_sec=60)
    conc = [s.target_concurrency for s in plan.steps]
    assert len(conc) == 4
    assert conc == sorted(conc) and len(set(conc)) == 4
    assert conc[0] == 40 and conc[1] == 55 and conc[2] == 250 and conc[3] == 360
    assert plan.total_duration_sec == sum(s.ramp_up_sec + s.hold_sec for s in plan.steps)
    assert warnings == []


def test_load_plan_antipattern_correction():
    # DBCP maxTotal >= maxThreads → step1/2 would exceed step3; must be corrected with a warning.
    thr = compute_thresholds(ParsedConfig(tomcat_max_threads=50, tomcat_accept_count=10, dbcp_max_total=200))
    plan, warnings = generate_load_plan(thr)
    conc = [s.target_concurrency for s in plan.steps]
    assert plan.scenario_type == "breakpoint"
    assert all(b > a for a, b in zip(conc, conc[1:]))
    assert warnings
    assert any("maxTotal" in f for f in rule_antipatterns(ParsedConfig(tomcat_max_threads=50, tomcat_accept_count=10, dbcp_max_total=200), thr))


def test_spike_plan_returns_to_baseline():
    thr = Thresholds(t_db_lock=50, t_queue=200, t_drop=300)
    plan, warnings = generate_load_plan(thr, hold_sec=60, spike_ramp_sec=2, scenario_type="spike")
    assert plan.scenario_type == "spike"
    assert [s.name for s in plan.steps] == ["Base (평시)", "Spike (급등)", "Recovery (복귀)"]
    assert [s.target_concurrency for s in plan.steps] == [25, 390, 25]
    assert plan.steps[1].ramp_up_sec == 2 and plan.steps[1].hold_sec == 30
    assert plan.steps[0].hold_sec == 60 and plan.steps[2].hold_sec == 60
    assert plan.steps[0].target_concurrency == plan.steps[2].target_concurrency
    assert warnings == []
    # Ramp outside 1..3 is clamped, and the global ramp scale does not stretch the spike.
    clamped, _ = generate_load_plan(thr, ramp_scale=3.0, spike_ramp_sec=9, scenario_type="spike")
    assert clamped.steps[1].ramp_up_sec == 3
    assert clamped.steps[0].ramp_up_sec == 30


def test_spike_warns_when_burst_does_not_exceed_baseline():
    thr = Thresholds(t_db_lock=400, t_queue=20, t_drop=30)
    plan, warnings = generate_load_plan(thr, scenario_type="spike")
    assert plan.steps[1].target_concurrency <= plan.steps[0].target_concurrency
    assert any("스파이크" in w for w in warnings)


def test_sla_plan_scales_to_target_and_warns_above_db_lock():
    thr = Thresholds(t_db_lock=50, t_queue=200, t_drop=300)
    plan, warnings = generate_load_plan(thr, scenario_type="sla", n_target=80, hold_sec=60)
    assert plan.scenario_type == "sla" and plan.n_target == 80
    assert [s.target_concurrency for s in plan.steps] == [40, 80, 96]
    assert any(w.startswith("사전 경고") and "80" in w and "50" in w for w in warnings)
    under, quiet = generate_load_plan(thr, scenario_type="sla", n_target=40)
    assert [s.target_concurrency for s in under.steps] == [20, 40, 48]
    assert not any(w.startswith("사전 경고") for w in quiet)
    missing, warned = generate_load_plan(thr, scenario_type="sla", n_target=None)
    assert missing.n_target == 1
    assert any("N_target" in w for w in warned)


def test_unknown_scenario_falls_back_to_breakpoint():
    thr = Thresholds(t_db_lock=50, t_queue=200, t_drop=300)
    plan, warnings = generate_load_plan(thr, scenario_type="soak")
    assert plan.scenario_type == "breakpoint"
    assert len(plan.steps) == 4
    assert warnings
