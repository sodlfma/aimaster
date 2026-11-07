from __future__ import annotations

import sys
from pathlib import Path

import pytest

from graph.build import build_graph, memory_checkpointer
from tools.ssh_wrapper import (
    RemoteConfigError,
    fetch_from_homes,
    resolve_remote_path,
    split_wrapper_output,
    validate_host,
)

FAKE_SSH = Path(__file__).resolve().parent / "fixtures" / "fake_sshrun.py"
WRAPPER = f"{sys.executable} {FAKE_SSH} {{host}} cat {{path}}"


def test_resolve_remote_path_appends_standard_relatives():
    assert resolve_remote_path("/sw/tomcat", "shl/tomcat.env") == "/sw/tomcat/shl/tomcat.env"
    assert resolve_remote_path("/sw/apache", "conf/httpd-mpm.conf") == "/sw/apache/conf/httpd-mpm.conf"


def test_resolve_remote_path_keeps_explicit_files():
    assert resolve_remote_path("/opt/was/shl/tomcat.env", "shl/tomcat.env") == "/opt/was/shl/tomcat.env"


def test_validate_host_blocks_injection():
    with pytest.raises(ValueError):
        validate_host("was01; rm -rf /")
    with pytest.raises(ValueError):
        validate_host("")


def test_split_wrapper_output_separator_convention():
    stdout = "=== /sw/tomcat/shl/tomcat.env ===\nJAVA_OPTS=x\n=== /sw/apache/conf/httpd-mpm.conf ===\nMaxRequestWorkers 400\n"
    parts = split_wrapper_output(stdout)
    assert parts["/sw/tomcat/shl/tomcat.env"].strip() == "JAVA_OPTS=x"
    assert "MaxRequestWorkers 400" in parts["/sw/apache/conf/httpd-mpm.conf"]


def test_fetch_from_homes_via_fake_sshrun():
    configs = fetch_from_homes(
        "was01.corp.local",
        "/sw/apache",
        "/sw/tomcat",
        ssh_wrapper_cmd=WRAPPER,
        timeout=10,
    )
    assert "maxThreads=200" in configs.files[configs.tomcat_remote]
    assert "MaxRequestWorkers" in configs.files[configs.apache_remote]
    raw = configs.to_raw_inputs()
    assert "HOST=was01.corp.local" in raw["web_mon_cron.env"]
    assert "maxThreads=200" in raw["tomcat.env"]


def test_fetch_from_homes_reports_wrapper_failure():
    with pytest.raises(RemoteConfigError) as exc:
        fetch_from_homes(
            "fail.example",
            "/sw/apache",
            "/sw/tomcat",
            ssh_wrapper_cmd=WRAPPER,
            timeout=10,
        )
    assert "exit 1" in str(exc.value) or "Permission denied" in str(exc.value)
    assert exc.value.configs is not None
    assert exc.value.configs.errors


def test_ssh_homes_feed_sc001_diagnosis(isolated_settings, no_llm):
    configs = fetch_from_homes(
        "was01.corp.local",
        "/sw/apache",
        "/sw/tomcat",
        ssh_wrapper_cmd=WRAPPER,
        timeout=10,
    )
    graph = build_graph(memory_checkpointer())
    cfg = {"configurable": {"thread_id": "t-ssh"}}
    snap = None
    for _mode, _chunk in graph.stream(
        {"run_id": "ssh", "raw_inputs": configs.to_raw_inputs(), "llm_enabled": False, "options": {"dry_run": True}},
        cfg,
        stream_mode=["updates", "custom"],
    ):
        snap = graph.get_state(cfg)
    assert snap is not None
    v = snap.values
    assert v["thresholds"].t_db_lock == 50
    assert v["thresholds"].t_queue == 200
    assert v["thresholds"].t_drop == 300
    assert [s.target_concurrency for s in v["load_plan"].steps] == [40, 55, 250, 360]
    assert v["target_info"].host == "was01.corp.local"
