"""Pre-run checks (spec 5.5)."""
from __future__ import annotations

import fnmatch
import re
import shutil
import socket
import subprocess
from typing import Optional

from config.settings import Settings, get_settings
from model.schemas import LoadPlan, PreflightCheck, ScenarioSpec

_VER = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def host_allowed(host: str, allowed: list[str]) -> bool:
    host = host.lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    return any(fnmatch.fnmatch(host, pat.lower()) for pat in allowed)


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


def check_target_reachable(scenario: ScenarioSpec, timeout: float = 3.0) -> PreflightCheck:
    try:
        with socket.create_connection((scenario.host, scenario.port), timeout=timeout):
            return PreflightCheck(name="target_reachable", ok=True, detail=f"{scenario.host}:{scenario.port} TCP 연결 성공", blocking=False)
    except OSError as e:
        return PreflightCheck(name="target_reachable", ok=False, detail=f"{scenario.host}:{scenario.port} 연결 실패: {e}", blocking=False)


def check_allowed_host(scenario: ScenarioSpec, settings: Settings) -> PreflightCheck:
    allowed = settings.allowed_hosts()
    ok = host_allowed(scenario.host, allowed)
    return PreflightCheck(
        name="allowed_host", ok=ok,
        detail=f"{scenario.host} {'허용 목록에 있음' if ok else '허용 목록에 없음 (config/allowed_hosts.txt 에 추가 필요)'}",
        blocking=True,
    )


def check_load_capacity(plan: LoadPlan, settings: Settings) -> PreflightCheck:
    mx = max((s.target_concurrency for s in plan.steps), default=0)
    ok = mx <= settings.max_local_threads
    return PreflightCheck(
        name="load_capacity", ok=ok,
        detail=f"최대 동시 스레드 {mx} (단일 부하기 권장 상한 {settings.max_local_threads})" + ("" if ok else " → 분산 모드(-R) 권장"),
        blocking=False,
    )


def preflight_checks(scenario: ScenarioSpec, plan: LoadPlan, mode: str, validation_ok: bool, settings: Optional[Settings] = None) -> list[PreflightCheck]:
    settings = settings or get_settings()
    checks: list[PreflightCheck] = []
    if mode == "docker":
        checks.append(check_docker(settings))
    else:
        checks.append(check_jmeter_version(settings))
    checks.append(check_allowed_host(scenario, settings))
    checks.append(check_target_reachable(scenario))
    checks.append(check_load_capacity(plan, settings))
    checks.append(PreflightCheck(name="jmx_validated", ok=validation_ok, detail="JMX 3단계 검증 통과" if validation_ok else "JMX 검증 실패", blocking=True))
    return checks
