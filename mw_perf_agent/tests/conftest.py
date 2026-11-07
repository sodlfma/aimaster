from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parents[1]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

FIXTURES = PKG_ROOT / "tests" / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def raw_inputs() -> dict[str, str]:
    return {name: (FIXTURES / name).read_text(encoding="utf-8") for name in ("tomcat.env", "httpd-mpm.conf", "web_mon_cron.env")}


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Point runs/, allowed hosts and JMeter binary at temp/fake resources for a test."""
    from config.settings import Settings, reset_settings

    allowed = tmp_path / "allowed_hosts.txt"
    allowed.write_text("localhost\n127.0.0.1\n*.corp.local\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_JMETER_FAST", "1")
    s = Settings(
        llm_model="", llm_enabled=False,
        jmeter_bin_override=str(FIXTURES / "fake_jmeter.py"),
        runs_dir=str(tmp_path / "runs"),
        allowed_hosts_file=str(allowed),
        ssh_password="",
        ssh_wrapper_cmd="",
    )
    reset_settings(s)
    yield s
    reset_settings(Settings())


@pytest.fixture
def no_llm():
    from llm.models import clear_model_override, set_model_override

    set_model_override(None)
    yield
    clear_model_override()
