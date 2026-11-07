"""Pre-run checks (spec 5.5)."""
from __future__ import annotations

import fnmatch
import re
import socket
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import quote

from config.settings import Settings, get_settings, normalize_host
from model.schemas import LoadPlan, PreflightCheck, ScenarioSpec, RequestSpec

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_VER = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def host_allowed(host: str, allowed: list[str]) -> bool:
    host = normalize_host(host)
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    return any(fnmatch.fnmatch(host, normalize_host(pat) or pat.lower()) for pat in allowed)


def hosts_equivalent(a: str, b: str) -> bool:
    left, right = normalize_host(a), normalize_host(b)
    return bool(left) and left == right


def check_jmeter_version(settings: Settings) -> PreflightCheck:
    jb = settings.jmeter_bin
    if not jb:
        return PreflightCheck(name="jmeter_installed", ok=False, detail="jmeter 실행 파일 없음 (JMETER_HOME/JMETER_BIN 설정 필요)", blocking=True)
    try:
        proc = subprocess.run([jb, "-v"], capture_output=True, text=True, timeout=30)
        text = proc.stdout + proc.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return PreflightCheck(name="jmeter_installed", ok=False, detail=f"jmeter -v 실패: {e}", blocking=True)
    m = _VER.search(text)
    if not m:
        return PreflightCheck(name="jmeter_installed", ok=True, detail="버전 문자열을 파악하지 못했지만 실행 가능", blocking=False)
    major, minor = int(m.group(1)), int(m.group(2))
    ok = (major, minor) >= (5, 5)
    return PreflightCheck(name="jmeter_installed", ok=True, detail=f"JMeter {m.group(0)}" + ("" if ok else " (5.5 미만: Open Model TG 미지원, 일부 속성 차이 가능)"), blocking=False)


def check_docker(settings: Settings) -> PreflightCheck:
    if not shutil.which("docker"):
        return PreflightCheck(name="docker", ok=False, detail="docker 명령 없음", blocking=True)
    return PreflightCheck(name="docker", ok=True, detail=f"image={settings.docker_image}", blocking=True)


def _local_ipv4s() -> set[str]:
    found = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
    try:
        hostname = socket.gethostname()
        found.add(hostname.lower())
        for info in socket.getaddrinfo(hostname, None):
            found.add(str(info[4][0]))
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            found.add(sock.getsockname()[0])
    except OSError:
        pass
    return found


_AWS_PUBLIC_IP: object = Ellipsis


def _aws_public_ipv4(timeout: float = 0.4) -> Optional[str]:
    """Best-effort IMDS lookup so we can detect hairpin NAT to this instance's EIP."""
    global _AWS_PUBLIC_IP
    if _AWS_PUBLIC_IP is not Ellipsis:
        return _AWS_PUBLIC_IP  # type: ignore[return-value]
    try:
        req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            token = resp.read().decode("ascii", errors="ignore").strip()
        ip_req = urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/public-ipv4",
            headers={"X-aws-ec2-metadata-token": token} if token else {},
        )
        with urllib.request.urlopen(ip_req, timeout=timeout) as resp:
            ip = resp.read().decode("ascii", errors="ignore").strip()
        _AWS_PUBLIC_IP = ip or None
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        _AWS_PUBLIC_IP = None
    return _AWS_PUBLIC_IP  # type: ignore[return-value]


def is_this_machine(host: str) -> bool:
    host = normalize_host(host)
    if not host:
        return False
    locals_ = _local_ipv4s()
    pub = _aws_public_ipv4()
    if pub:
        locals_.add(normalize_host(pub))
    return host in {normalize_host(x) for x in locals_}


def _tcp_ok(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def scenario_request_path(scenario: ScenarioSpec) -> str:
    req: Optional[RequestSpec] = scenario.requests[0] if scenario.requests else None
    path = (req.path if req else "/") or "/"
    if not path.startswith("/"):
        path = "/" + path
    return path


def http_status(protocol: str, host: str, port: int, path: str, timeout: float = 3.0) -> tuple[Optional[int], str]:
    """GET the scenario URL. Returns (status, body-or-error). status is None on connect failure."""
    path = path or "/"
    url = f"{protocol}://{host}:{port}{path}"
    try:
        req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json, */*;q=0.8"})
        ctx = None
        if protocol == "https":
            import ssl
            ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read(240).decode("utf-8", errors="replace")
            return int(resp.status), body
    except urllib.error.HTTPError as e:
        try:
            body = e.read(240).decode("utf-8", errors="replace") if e.fp else ""
        except Exception:  # noqa: BLE001
            body = ""
        return int(e.code), body
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def resolve_load_target(
    scenario: ScenarioSpec,
    mode: str = "local",
    overrides: Optional[dict] = None,
    timeout: float = 1.5,
) -> tuple[str, int, str, dict, list[str]]:
    """Host/port/protocol JMeter should actually hit, plus merged -J overrides.

    Hitting this instance's public IPv4 (AWS hairpin NAT) times out before SYN
    reaches Tomcat: 100% JMeter errors and an empty access log. Prefer loopback
    when the load generator *is* the target and localhost:port is open.
    """
    merged = {str(k): str(v) for k, v in (overrides or {}).items()}
    notes: list[str] = []
    host = normalize_host(merged.get("target.host", scenario.host)) or scenario.host
    try:
        port = int(merged.get("target.port", scenario.port))
    except (TypeError, ValueError):
        port = int(scenario.port)
    protocol = (merged.get("target.protocol", scenario.protocol) or "http").lower()
    if protocol not in ("http", "https"):
        protocol = "http"

    if mode not in ("local", "docker") or host in LOOPBACK_HOSTS or "target.host" in (overrides or {}):
        return host, port, protocol, merged, notes

    local_tcp = _tcp_ok("127.0.0.1", port, timeout)
    same = is_this_machine(host)
    remote_tcp = _tcp_ok(host, port, timeout)
    use_loopback = False
    if local_tcp and same:
        use_loopback = True
    elif local_tcp and not remote_tcp:
        code, _ = http_status(protocol, "127.0.0.1", port, scenario_request_path(scenario), timeout=timeout)
        use_loopback = code is not None

    if use_loopback:
        merged["target.host"] = "localhost"
        notes.append(
            f"{host}:{port} 는 이 부하기 자신입니다. 공인 IP 헤어핀 NAT는 Tomcat까지 패킷이 안 가서 "
            f"access log가 비고 JMeter 에러율 100%가 됩니다. 대상은 localhost:{port} 로 보냅니다."
        )
        host = "localhost"
    return host, port, protocol, merged, notes


def check_target_reachable(
    scenario: ScenarioSpec,
    timeout: float = 3.0,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> PreflightCheck:
    host = host if host is not None else scenario.host
    port = int(port if port is not None else scenario.port)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return PreflightCheck(name="target_reachable", ok=True, detail=f"{host}:{port} TCP 연결 성공", blocking=False)
    except OSError as e:
        detail = f"{host}:{port} 연결 실패: {e}"
        orig = scenario.host
        if is_this_machine(orig) and orig not in LOOPBACK_HOSTS:
            if _tcp_ok("127.0.0.1", port, timeout):
                detail += (
                    f" — 이 머신의 공인/로컬 주소로 보이며 localhost:{port} 는 연결됩니다. "
                    "AWS 공인 IP는 인스턴스 자신에서 헤어핀 NAT로 타임아웃되는 경우가 많습니다. "
                    "실행 시 대상을 localhost 로 자동 전환합니다."
                )
            else:
                detail += (
                    f" localhost:{port} 도 실패했습니다. Tomcat이 해당 포트에서 듣고 있는지 확인하세요."
                )
        else:
            detail += (
                " (보안그룹/방화벽에서 이 부하기 → WAS HTTP 포트 접근이 막혀 있을 수 있습니다. "
                "SSH(22)가 열려 있어도 8080/8443 은 별도 허용이 필요합니다. "
                "브라우저에서만 되고 부하기에서 실패하면 요청이 서버에 도달하지 않아 access log도 없습니다.)"
            )
        return PreflightCheck(name="target_reachable", ok=False, detail=detail, blocking=False)


def check_http_probe(
    scenario: ScenarioSpec,
    host: str,
    port: int,
    protocol: str,
    timeout: float = 3.0,
) -> PreflightCheck:
    path = scenario_request_path(scenario)
    # Encode only the path segment before '?' so the query string stays intact.
    if "?" in path:
        p, qs = path.split("?", 1)
        url_path = quote(p, safe="/") + "?" + qs
    else:
        url_path = quote(path, safe="/")
    url = f"{protocol}://{host}:{port}{path}"
    code, detail = http_status(protocol, host, port, url_path, timeout=timeout)
    if code is None:
        return PreflightCheck(
            name="http_probe", ok=False, blocking=False,
            detail=(
                f"{url} 연결 실패: {detail}. "
                "JMeter 샘플이 WAS access log에 한 줄도 안 남는 전형적인 원인입니다 "
                "(헤어핀 NAT·보안그룹·잘못된 호스트)."
            ),
        )
    snippet = " ".join(detail.split())[:100]
    ok = 200 <= code < 400
    hint = "" if ok else " — 이 상태면 부하 실행 시 에러율이 매우 높아집니다."
    return PreflightCheck(
        name="http_probe", ok=ok, blocking=False,
        detail=f"{url} → HTTP {code} {snippet}{hint}",
    )


def check_allowed_host(scenario: ScenarioSpec, settings: Settings, diagnosed_host: Optional[str] = None) -> PreflightCheck:
    allowed = settings.allowed_hosts()
    ok = host_allowed(scenario.host, allowed)
    if ok:
        detail = f"{scenario.host} 허용 목록에 있음"
    elif diagnosed_host and hosts_equivalent(scenario.host, diagnosed_host):
        ok = True
        detail = f"{scenario.host} 이 Run에서 SSH 진단한 대상이라 허용"
    else:
        detail = f"{scenario.host} 허용 목록에 없음 (실행 화면에서 추가하거나 config/allowed_hosts.txt 에 넣으세요)"
    return PreflightCheck(name="allowed_host", ok=ok, detail=detail, blocking=True)


def check_load_capacity(plan: LoadPlan, settings: Settings) -> PreflightCheck:
    mx = max((s.target_concurrency for s in plan.steps), default=0)
    ok = mx <= settings.max_local_threads
    return PreflightCheck(
        name="load_capacity", ok=ok,
        detail=f"최대 동시 스레드 {mx} (단일 부하기 권장 상한 {settings.max_local_threads})" + ("" if ok else " → 분산 모드(-R) 권장"),
        blocking=False,
    )


def preflight_checks(
    scenario: ScenarioSpec,
    plan: LoadPlan,
    mode: str,
    validation_ok: bool,
    settings: Optional[Settings] = None,
    diagnosed_host: Optional[str] = None,
    overrides: Optional[dict] = None,
    rewrite_notes: Optional[list[str]] = None,
) -> list[PreflightCheck]:
    settings = settings or get_settings()
    host, port, protocol, _merged, auto_notes = resolve_load_target(scenario, mode, overrides)
    notes = rewrite_notes if rewrite_notes is not None else auto_notes
    checks: list[PreflightCheck] = []
    if mode == "docker":
        checks.append(check_docker(settings))
    else:
        checks.append(check_jmeter_version(settings))
    checks.append(check_allowed_host(scenario, settings, diagnosed_host=diagnosed_host))
    if notes:
        checks.append(PreflightCheck(name="load_target", ok=True, detail=" ".join(notes), blocking=False))
    tcp = check_target_reachable(scenario, host=host, port=port)
    checks.append(tcp)
    if tcp.ok:
        checks.append(check_http_probe(scenario, host, port, protocol))
    else:
        checks.append(PreflightCheck(
            name="http_probe", ok=False, blocking=False,
            detail=f"TCP 실패로 HTTP 프로브 생략 ({host}:{port}). 요청이 WAS에 도달하지 않으면 access log가 비고 에러율 100%가 됩니다.",
        ))
    checks.append(check_load_capacity(plan, settings))
    checks.append(PreflightCheck(name="jmx_validated", ok=validation_ok, detail="JMX 3단계 검증 통과" if validation_ok else "JMX 검증 실패", blocking=True))
    return checks
