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


def pytest_addoption(parser):
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="Call the real LLM API (requires LLM_MODEL and LLM_API_KEY).",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "live: calls a real LLM API (opt-in)")
    if config.getoption("--run-live"):
        os.environ.setdefault("RUN_LIVE_LLM", "1")


def pytest_collection_modifyitems(config, items):
    run_live = bool(config.getoption("--run-live")) or os.environ.get("RUN_LIVE_LLM", "").strip().lower() in ("1", "true", "yes")
    if run_live:
        return
    skip = pytest.mark.skip(reason="live LLM tests are opt-in: pytest --run-live (needs LLM_API_KEY)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


def pytest_sessionfinish(session, exitstatus):
    """Write docs/nfr_verification.md from in-process measurements when any NFR/golden test ran."""
    try:
        from tests.nfr_results import RESULTS, maybe_write_report
    except Exception:
        return
    if not RESULTS:
        return
    maybe_write_report()
