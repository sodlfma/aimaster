from __future__ import annotations

from model.schemas import LoadPlan, LoadStep, ScenarioSpec
from tools.preflight import check_allowed_host, check_target_reachable, host_allowed, hosts_equivalent, preflight_checks
from config.settings import normalize_host


def _plan(n: int = 10) -> LoadPlan:
    return LoadPlan(steps=[LoadStep(name="s", target_concurrency=n, ramp_up_sec=1, hold_sec=1)], total_duration_sec=2)


def test_normalize_host_strips_user_and_port():
    assert normalize_host("ec2-user@3.35.11.139") == "3.35.11.139"
    assert normalize_host("3.35.11.139:8080") == "3.35.11.139"
    assert normalize_host("WAS01.CORP.LOCAL.") == "was01.corp.local"
    assert hosts_equivalent("ec2-user@3.35.11.139", "3.35.11.139")


def test_host_allowed_localhost_and_glob():
    assert host_allowed("localhost", [])
    assert host_allowed("was01.corp.local", ["*.corp.local"])
    assert not host_allowed("3.35.11.139", ["localhost", "127.0.0.1"])


def test_check_allowed_host_blocks_unknown(isolated_settings):
    spec = ScenarioSpec(host="3.35.11.139", port=8080)
    chk = check_allowed_host(spec, isolated_settings)
    assert not chk.ok and chk.blocking


def test_check_allowed_host_allows_diagnosed_ip(isolated_settings):
    spec = ScenarioSpec(host="3.35.11.139", port=8080)
    chk = check_allowed_host(spec, isolated_settings, diagnosed_host="ec2-user@3.35.11.139")
    assert chk.ok and "진단" in chk.detail


def test_allow_host_runtime_and_persist(isolated_settings):
    spec = ScenarioSpec(host="3.35.11.139", port=8080)
    assert not check_allowed_host(spec, isolated_settings).ok
    isolated_settings.allow_host("3.35.11.139", persist=True)
    chk = check_allowed_host(spec, isolated_settings)
    assert chk.ok
    assert "3.35.11.139" in isolated_settings.allowed_hosts_from_file()


def test_preflight_diagnosed_host_not_blocked(isolated_settings, monkeypatch):
    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("tools.preflight.socket.create_connection", lambda *a, **k: _Conn())
    spec = ScenarioSpec(host="3.35.11.139", port=8080)
    checks = preflight_checks(spec, _plan(), "local", True, isolated_settings, diagnosed_host="3.35.11.139")
    blocked = [c for c in checks if c.blocking and not c.ok]
    assert blocked == []
    assert next(c for c in checks if c.name == "allowed_host").ok


def test_target_reachable_firewall_hint(monkeypatch):
    def _boom(*a, **k):
        raise TimeoutError("timed out")

    monkeypatch.setattr("tools.preflight.socket.create_connection", _boom)
    monkeypatch.setattr("tools.preflight.is_this_machine", lambda host: False)
    chk = check_target_reachable(ScenarioSpec(host="3.35.11.139", port=8080), timeout=0.01)
    assert not chk.ok and not chk.blocking
    assert "보안그룹" in chk.detail


def test_target_reachable_hairpin_hint(monkeypatch):
    def _connect(addr, timeout=None):
        host, port = addr
        if host in ("127.0.0.1", "localhost"):
            class _Conn:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False
            return _Conn()
        raise TimeoutError("timed out")

    monkeypatch.setattr("tools.preflight.socket.create_connection", _connect)
    monkeypatch.setattr("tools.preflight.is_this_machine", lambda host: True)
    chk = check_target_reachable(ScenarioSpec(host="3.35.11.139", port=8080), timeout=0.01)
    assert not chk.ok
    assert "localhost" in chk.detail and "target.host=localhost" in chk.detail
