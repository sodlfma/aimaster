from __future__ import annotations

import sys
from pathlib import Path

import pytest

from modules.module_001_diagnostic import (
    RemoteConfigError,
    collect_and_diagnose,
    fetch_remote_configs,
    fetch_remote_configs_mock,
    parse_tomcat_env,
    resolve_remote_path,
    split_wrapper_output,
    validate_host,
)

FAKE_SSH = Path(__file__).resolve().parent / "fake_sshrun.py"
WRAPPER = f"{sys.executable} {FAKE_SSH} {{host}} cat {{path}}"


def test_resolve_remote_path_appends_standard_relatives():
    assert resolve_remote_path("/sw/tomcat", "shl/tomcat.env") == "/sw/tomcat/shl/tomcat.env"
    assert resolve_remote_path("/sw/apache", "conf/httpd-mpm.conf") == "/sw/apache/conf/httpd-mpm.conf"


def test_resolve_remote_path_keeps_explicit_files():
    assert resolve_remote_path("/opt/was/shl/tomcat.env", "shl/tomcat.env") == "/opt/was/shl/tomcat.env"
    assert resolve_remote_path("/etc/httpd/conf/httpd-mpm.conf", "conf/httpd-mpm.conf") == "/etc/httpd/conf/httpd-mpm.conf"


def test_validate_host_blocks_injection():
    with pytest.raises(ValueError):
        validate_host("was01; rm -rf /")
    with pytest.raises(ValueError):
        validate_host("was01 && cat /etc/passwd")
    with pytest.raises(ValueError):
        validate_host("")


def test_split_wrapper_output_separator_convention():
    stdout = "=== /sw/tomcat/shl/tomcat.env ===\nJAVA_OPTS=x\n=== /sw/apache/conf/httpd-mpm.conf ===\nMaxRequestWorkers 400\n"
    parts = split_wrapper_output(stdout)
    assert parts["/sw/tomcat/shl/tomcat.env"].strip() == "JAVA_OPTS=x"
    assert "MaxRequestWorkers 400" in parts["/sw/apache/conf/httpd-mpm.conf"]


def test_fetch_remote_configs_via_fake_sshrun():
    configs = fetch_remote_configs(
        "was01.corp.local",
        "/sw/apache",
        "/sw/tomcat",
        ssh_wrapper_cmd_template=WRAPPER,
        timeout=10,
    )
    assert "maxThreads=200" in configs.files[configs.tomcat_remote]
    assert "MaxRequestWorkers 400" in configs.files[configs.apache_remote]
    assert configs.errors == {}


def test_fetch_remote_configs_rejects_bad_host():
    with pytest.raises(ValueError):
        fetch_remote_configs("bad host", "/sw/apache", "/sw/tomcat", ssh_wrapper_cmd_template=WRAPPER)


def test_fetch_remote_configs_reports_wrapper_failure():
    with pytest.raises(RemoteConfigError) as exc:
        fetch_remote_configs(
            "fail.example",
            "/sw/apache",
            "/sw/tomcat",
            ssh_wrapper_cmd_template=WRAPPER,
            timeout=10,
        )
    assert "종료코드 1" in str(exc.value)
    assert exc.value.configs is not None
    assert exc.value.configs.errors


def test_collect_and_diagnose_from_ssh_wrapper():
    result = collect_and_diagnose(
        "was01.corp.local",
        "/sw/apache",
        "/sw/tomcat",
        ssh_wrapper_cmd_template=WRAPPER,
        timeout=10,
    )
    assert result["max_threads"] == 200
    assert result["accept_count"] == 100
    assert result["db_max_total"] == 50
    assert (result["t_db_lock"], result["t_queue"], result["t_drop"]) == (50, 200, 300)
    conc = [row["Target Concurrency"] for row in result["load_plan"]]
    assert conc == [40, 55, 250, 360]


def test_mock_and_parse_match_original_java_opts():
    configs = fetch_remote_configs_mock("host", "/sw/apache", "/sw/tomcat")
    max_t, acc_c, db_max = parse_tomcat_env(configs.text_for_parse())
    assert (max_t, acc_c, db_max) == (200, 100, 50)
