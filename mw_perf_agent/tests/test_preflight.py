from __future__ import annotations

from model.schemas import LoadPlan, LoadStep, RequestSpec, ScenarioSpec
from tools.preflight import (
    check_allowed_host,
    check_target_reachable,
    host_allowed,
    hosts_equivalent,
    preflight_checks,
    resolve_load_target,
)
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
    monkeypatch.setattr("tools.preflight.http_status", lambda *a, **k: (200, '{"status":"SUCCESS"}'))
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
    assert "localhost" in chk.detail and "헤어핀" in chk.detail


def test_resolve_load_target_rewrites_same_machine_public_ip(monkeypatch):
    def _connect(addr, timeout=None):
        host, _port = addr
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
    spec = ScenarioSpec(
        host="3.35.11.139", port=8080, protocol="http",
        requests=[RequestSpec(method="GET", path="/api/perf/db-delay?holdMs=300", label="GET /api/perf/db-delay")],
    )
    host, port, protocol, merged, notes = resolve_load_target(spec, "local", {}, timeout=0.01)
    assert (host, port, protocol) == ("localhost", 8080, "http")
    assert merged["target.host"] == "localhost"
    assert notes and "헤어핀" in notes[0]


def test_resolve_load_target_respects_user_override(monkeypatch):
    monkeypatch.setattr("tools.preflight.is_this_machine", lambda host: True)
    spec = ScenarioSpec(host="3.35.11.139", port=8080)
    host, _port, _proto, merged, notes = resolve_load_target(
        spec, "local", {"target.host": "3.35.11.139"}, timeout=0.01,
    )
    assert host == "3.35.11.139" and "target.host" in merged
    assert notes == []


def test_preflight_http_probe_and_rewrite_notes(isolated_settings, monkeypatch):
    def _connect(addr, timeout=None):
        host, _port = addr
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
    monkeypatch.setattr("tools.preflight.http_status", lambda *a, **k: (200, '{"status":"SUCCESS","holdMs":300}'))
    spec = ScenarioSpec(
        host="3.35.11.139", port=8080,
        requests=[RequestSpec(method="GET", path="/api/perf/db-delay?holdMs=300", label="db-delay")],
    )
    _, _, _, merged, notes = resolve_load_target(spec, "local", {}, timeout=0.01)
    checks = preflight_checks(
        spec, _plan(), "local", True, isolated_settings,
        diagnosed_host="3.35.11.139", overrides=merged, rewrite_notes=notes,
    )
    assert next(c for c in checks if c.name == "load_target").ok
    assert next(c for c in checks if c.name == "target_reachable").ok
    probe = next(c for c in checks if c.name == "http_probe")
    assert probe.ok and "HTTP 200" in probe.detail and "localhost" in probe.detail
    assert not [c for c in checks if c.blocking and not c.ok]
