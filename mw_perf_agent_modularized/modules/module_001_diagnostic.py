"""SC-001: SSH로 원격 Apache/Tomcat 설정을 수집하고 3계층 병목·4단계 부하 플랜을 산출한다.

사내 SSH 쉘 실행 프로그램을 Paramiko 없이 `subprocess.run(shell=False)`로 래핑한다.
명령 템플릿은 환경변수 `SSH_WRAPPER_CMD` (예: `/opt/tools/sshrun {host} cat {path}`).
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Optional

HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")
PATH_RE = re.compile(r"^[A-Za-z0-9._/+-]+$")

DEFAULT_TOMCAT_REL = "shl/tomcat.env"
DEFAULT_APACHE_REL = "conf/httpd-mpm.conf"
DEFAULT_SSH_WRAPPER_CMD = "ssh -o BatchMode=yes -o ConnectTimeout=10 {host} cat {path}"
DEFAULT_TIMEOUT_SEC = 30

SAMPLE_TOMCAT_ENV = """\
JAVA_OPTS="$JAVA_OPTS -Dtomcat.connector.maxThreads=200"
JAVA_OPTS="$JAVA_OPTS -Dtomcat.connector.acceptCount=100"
JAVA_OPTS="$JAVA_OPTS -Ddbcp.maxTotal=50"
"""

SAMPLE_HTTPD_MPM = """\
LoadModule mpm_event_module modules/mod_mpm_event.so
<IfModule mpm_event_module>
    MaxRequestWorkers 400
    ThreadsPerChild 25
</IfModule>
"""


class RemoteConfigError(Exception):
    """SSH 수집이 진단에 필요한 설정을 가져오지 못했을 때."""

    def __init__(self, message: str, configs: Optional["RemoteConfigs"] = None):
        super().__init__(message)
        self.configs = configs


@dataclass
class RemoteConfigs:
    host: str
    apache_path: str
    tomcat_path: str
    apache_remote: str
    tomcat_remote: str
    files: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    wrapper_cmd: str = ""

    def text_for_parse(self) -> str:
        """tomcat.env를 우선하고, 없으면 수집된 원문을 이어 붙여 JAVA_OPTS를 찾는다."""
        preferred: list[str] = []
        others: list[str] = []
        for path, content in self.files.items():
            lowered = path.lower()
            if "tomcat.env" in lowered or lowered.endswith("tomcat.env"):
                preferred.append(content)
            else:
                others.append(content)
        return "\n".join(part for part in preferred + others if part and not part.lstrip().startswith("# error"))


def validate_host(host: str) -> str:
    host = (host or "").strip()
    if not HOST_RE.match(host):
        raise ValueError(f"허용되지 않는 호스트명 형식: {host!r}")
    return host


def validate_path(path: str) -> str:
    path = (path or "").strip()
    if not path or not PATH_RE.match(path):
        raise ValueError(f"허용되지 않는 경로 문자: {path!r}")
    return path


def resolve_remote_path(base: str, default_rel: str) -> str:
    """디렉터리면 사내 표준 상대경로를 붙이고, 이미 파일(.env/.conf/.xml/.sh)이면 그대로 쓴다."""
    base = (base or "").strip().replace("\\", "/").rstrip("/")
    default_rel = default_rel.replace("\\", "/").lstrip("/")
    if not base:
        return "/" + default_rel
    lowered = base.lower()
    if lowered.endswith((".env", ".conf", ".xml", ".sh")):
        return validate_path(base)
    return validate_path(f"{base}/{default_rel}")


def ssh_wrapper_cmd(override: Optional[str] = None) -> str:
    cmd = (override if override is not None else os.environ.get("SSH_WRAPPER_CMD", "")).strip()
    return cmd or DEFAULT_SSH_WRAPPER_CMD


def _build_cmd(template: str, host: str, path: str) -> list[str]:
    if not template:
        raise RemoteConfigError("SSH 래퍼 명령 템플릿이 비어 있습니다 (예: '/opt/tools/sshrun {host} cat {path}').")
    if "{host}" not in template or "{path}" not in template:
        raise RemoteConfigError("SSH 래퍼 명령에는 {host}와 {path} 자리표시자가 필요합니다.")
    return [part.replace("{host}", host).replace("{path}", path) for part in shlex.split(template)]


def split_wrapper_output(stdout: str) -> dict[str, str]:
    """단일 래퍼 출력에서 `=== <path> ===` 구분자 규약을 분리한다."""
    parts: dict[str, str] = {}
    current: Optional[str] = None
    buf: list[str] = []
    for line in stdout.splitlines():
        m = re.match(r"^===\s+(.+?)\s+===$", line.strip())
        if m:
            if current is not None:
                parts[current] = "\n".join(buf)
            current, buf = m.group(1), []
        else:
            buf.append(line)
    if current is not None:
        parts[current] = "\n".join(buf)
    return parts


def _run_wrapper(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=False)


def _fetch_one(template: str, host: str, path: str, timeout: int) -> str:
    cmd = _build_cmd(template, host, path)
    try:
        proc = _run_wrapper(cmd, timeout)
    except subprocess.TimeoutExpired as exc:
        raise RemoteConfigError(f"{path}: SSH 수집 타임아웃 ({timeout}s)") from exc
    except OSError as exc:
        raise RemoteConfigError(f"{path}: SSH 래퍼 실행 실패: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:300]
        raise RemoteConfigError(f"{path}: SSH 래퍼 종료코드 {proc.returncode}: {err}")
    return proc.stdout or ""


def fetch_remote_configs(
    host: str,
    apache_path: str,
    tomcat_path: str,
    ssh_wrapper_cmd_template: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> RemoteConfigs:
    """호스트의 Apache/Tomcat 설정 원문을 SSH 래퍼로 읽어 반환한다."""
    host = validate_host(host)
    apache_remote = resolve_remote_path(apache_path, DEFAULT_APACHE_REL)
    tomcat_remote = resolve_remote_path(tomcat_path, DEFAULT_TOMCAT_REL)
    template = ssh_wrapper_cmd(ssh_wrapper_cmd_template)
    configs = RemoteConfigs(
        host=host,
        apache_path=apache_path,
        tomcat_path=tomcat_path,
        apache_remote=apache_remote,
        tomcat_remote=tomcat_remote,
        wrapper_cmd=template,
    )

    for path in (tomcat_remote, apache_remote):
        try:
            stdout = _fetch_one(template, host, path, timeout)
        except RemoteConfigError as exc:
            configs.errors[path] = str(exc)
            continue
        split = split_wrapper_output(stdout)
        if split:
            configs.files.update(split)
        else:
            configs.files[path] = stdout

    if tomcat_remote not in configs.files and not any("tomcat.env" in p.lower() for p in configs.files):
        detail = configs.errors.get(tomcat_remote) or "tomcat.env를 수집하지 못했습니다."
        raise RemoteConfigError(detail, configs=configs)
    return configs


def fetch_remote_configs_mock(host: str, apache_path: str, tomcat_path: str) -> RemoteConfigs:
    """SSH 없이 사내 표준 템플릿 샘플을 반환한다 (로컬 미리보기용)."""
    apache_remote = resolve_remote_path(apache_path or "/sw/apache", DEFAULT_APACHE_REL)
    tomcat_remote = resolve_remote_path(tomcat_path or "/sw/tomcat", DEFAULT_TOMCAT_REL)
    return RemoteConfigs(
        host=host or "mock-host",
        apache_path=apache_path,
        tomcat_path=tomcat_path,
        apache_remote=apache_remote,
        tomcat_remote=tomcat_remote,
        files={
            tomcat_remote: SAMPLE_TOMCAT_ENV,
            apache_remote: SAMPLE_HTTPD_MPM,
        },
        wrapper_cmd="mock",
    )


def parse_tomcat_env(text: str) -> tuple[int, int, int]:
    """tomcat.env의 JAVA_OPTS에서 maxThreads / acceptCount / dbcp maxTotal을 추출한다."""
    max_threads = 200
    accept_count = 100
    db_max_total = 50

    match_threads = re.search(r"maxThreads=(\d+)", text or "")
    match_accept = re.search(r"acceptCount=(\d+)", text or "")
    match_db = re.search(r"maxTotal=(\d+)", text or "")

    if match_threads:
        max_threads = int(match_threads.group(1))
    if match_accept:
        accept_count = int(match_accept.group(1))
    if match_db:
        db_max_total = int(match_db.group(1))

    return max_threads, accept_count, db_max_total


def calculate_thresholds(max_threads: int, accept_count: int, db_max_total: int) -> tuple[int, int, int]:
    db_lock = db_max_total
    queueing = max_threads
    drop = max_threads + accept_count
    return db_lock, queueing, drop


def generate_load_plan(db_lock: int, queueing: int, drop: int) -> list[dict]:
    return [
        {"Step": "Step 1 (Normal)", "Target Concurrency": int(db_lock * 0.8), "Ramp-up (sec)": 10, "Hold Time (sec)": 60},
        {"Step": "Step 2 (DB Saturation)", "Target Concurrency": int(db_lock * 1.1), "Ramp-up (sec)": 10, "Hold Time (sec)": 60},
        {"Step": "Step 3 (WAS Queueing)", "Target Concurrency": int((queueing + drop) / 2), "Ramp-up (sec)": 20, "Hold Time (sec)": 60},
        {"Step": "Step 4 (Stress/Drop)", "Target Concurrency": int(drop * 1.2), "Ramp-up (sec)": 30, "Hold Time (sec)": 60},
    ]


def diagnose_config_text(text: str) -> dict:
    max_t, acc_c, db_max = parse_tomcat_env(text)
    t_db, t_queue, t_drop = calculate_thresholds(max_t, acc_c, db_max)
    return {
        "max_threads": max_t,
        "accept_count": acc_c,
        "db_max_total": db_max,
        "t_db_lock": t_db,
        "t_queue": t_queue,
        "t_drop": t_drop,
        "load_plan": generate_load_plan(t_db, t_queue, t_drop),
    }


def collect_and_diagnose(
    host: str,
    apache_path: str,
    tomcat_path: str,
    ssh_wrapper_cmd_template: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    use_mock: bool = False,
) -> dict:
    """원격(또는 mock) 설정을 수집한 뒤 임계치와 부하 플랜을 산출한다."""
    configs = (
        fetch_remote_configs_mock(host, apache_path, tomcat_path)
        if use_mock
        else fetch_remote_configs(
            host,
            apache_path,
            tomcat_path,
            ssh_wrapper_cmd_template=ssh_wrapper_cmd_template,
            timeout=timeout,
        )
    )
    result = diagnose_config_text(configs.text_for_parse())
    result["configs"] = configs
    return result
