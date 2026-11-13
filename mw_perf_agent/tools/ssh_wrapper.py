"""Wrap the in-house SSH shell runner with subprocess (shell=False) — see spec 6.1."""
from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from config.settings import get_settings

_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_PATH_RE = re.compile(r"^[A-Za-z0-9._/+-]+$")
SEPARATOR = "=== {path} ==="

DEFAULT_TOMCAT_REL = "shl/tomcat.env"
DEFAULT_APACHE_REL = "conf/httpd-mpm.conf"
DEFAULT_SSH_WRAPPER_CMD = "ssh -o BatchMode=yes -o ConnectTimeout=10 {host} cat {path}"
DEFAULT_TIMEOUT_SEC = 30


class RemoteConfigError(Exception):
    """SSH 수집이 진단에 필요한 tomcat.env를 가져오지 못했을 때."""

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

    def to_raw_inputs(self) -> dict[str, str]:
        raw = {
            "web_mon_cron.env": (
                f"HOST={self.host}\n"
                f"APACHE_HOME={self.apache_path}\n"
                f"TOMCAT_HOME={self.tomcat_path}\n"
            ),
            "httpd-mpm.conf": "",
            "tomcat.env": "",
        }
        for path, content in self.files.items():
            raw[classify_remote_path(path)] = content
        return raw


def validate_host(host: str) -> str:
    host = (host or "").strip()
    if not _HOST_RE.match(host):
        raise ValueError(f"허용되지 않는 호스트명 형식: {host!r}")
    return host


def validate_path(path: str) -> str:
    path = (path or "").strip()
    if not path or not _PATH_RE.match(path):
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


def classify_remote_path(path: str) -> str:
    lowered = path.replace("\\", "/").lower()
    name = lowered.rsplit("/", 1)[-1]
    if "tomcat.env" in lowered or name == "tomcat.env":
        return "tomcat.env"
    if "httpd" in lowered or name.endswith(".conf"):
        return "httpd-mpm.conf"
    if "web_mon" in lowered:
        return "web_mon_cron.env"
    return path


def effective_ssh_wrapper_cmd(override: Optional[str] = None) -> str:
    cmd = (override if override is not None else "").strip()
    if not cmd:
        cmd = (get_settings().ssh_wrapper_cmd or "").strip()
    return cmd or DEFAULT_SSH_WRAPPER_CMD


def _build_cmd(template: str, host: str, path: str) -> list[str]:
    if not template:
        raise RemoteConfigError("SSH 래퍼 명령 템플릿이 비어 있습니다 (예: '/opt/tools/sshrun {host} cat {path}').")
    if "{host}" not in template or "{path}" not in template:
        raise RemoteConfigError("SSH 래퍼 명령에는 {host}와 {path} 자리표시자가 필요합니다.")
    return [part.replace("{host}", host).replace("{path}", path) for part in shlex.split(template)]


def split_wrapper_output(stdout: str) -> dict[str, str]:
    """Split a single wrapper output using the '=== <path> ===' separator convention."""
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


def fetch_remote_configs(
    host: str,
    paths: list[str],
    timeout: int = DEFAULT_TIMEOUT_SEC,
    ssh_wrapper_cmd: Optional[str] = None,
) -> dict[str, str]:
    """Return {path: content}. Raises on invalid host; records per-file failures as `# error:` stubs."""
    host = validate_host(host)
    template = effective_ssh_wrapper_cmd(ssh_wrapper_cmd)
    out: dict[str, str] = {}
    for path in paths:
        if not _PATH_RE.match(path):
            out[path] = f"# skipped: 허용되지 않는 경로 문자 ({path})"
            continue
        cmd = _build_cmd(template, host, path)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=False)
        except subprocess.TimeoutExpired:
            out[path] = f"# error: timeout after {timeout}s"
            continue
        except OSError as e:
            out[path] = f"# error: {e}"
            continue
        if proc.returncode != 0:
            out[path] = f"# error: exit {proc.returncode}: {(proc.stderr or '').strip()[:300]}"
        else:
            split = split_wrapper_output(proc.stdout or "")
            if split:
                out.update(split)
            else:
                out[path] = proc.stdout or ""
    return out


def fetch_from_homes(
    host: str,
    apache_path: str,
    tomcat_path: str,
    ssh_wrapper_cmd: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> RemoteConfigs:
    """호스트의 Apache/Tomcat 홈(또는 파일 경로)에서 설정 원문을 수집한다."""
    host = validate_host(host)
    apache_remote = resolve_remote_path(apache_path, DEFAULT_APACHE_REL)
    tomcat_remote = resolve_remote_path(tomcat_path, DEFAULT_TOMCAT_REL)
    template = effective_ssh_wrapper_cmd(ssh_wrapper_cmd)
    configs = RemoteConfigs(
        host=host,
        apache_path=apache_path,
        tomcat_path=tomcat_path,
        apache_remote=apache_remote,
        tomcat_remote=tomcat_remote,
        wrapper_cmd=template,
    )
    fetched = fetch_remote_configs(
        host, [tomcat_remote, apache_remote], timeout=timeout, ssh_wrapper_cmd=template
    )
    for path, content in fetched.items():
        if (content or "").lstrip().startswith("# error") or (content or "").lstrip().startswith("# skipped"):
            configs.errors[path] = content.strip()
        else:
            configs.files[path] = content
    has_tomcat = tomcat_remote in configs.files or any(
        classify_remote_path(p) == "tomcat.env" for p in configs.files
    )
    if not has_tomcat:
        detail = configs.errors.get(tomcat_remote) or "tomcat.env를 수집하지 못했습니다."
        raise RemoteConfigError(detail, configs=configs)
    return configs


def collect_gc_stats_cmd(host: str, pid: int, interval_ms: int, count: int) -> list[str]:
    host = validate_host(host)
    remote = f"jstat -gcutil {int(pid)} {int(interval_ms)} {int(count)}"
    return _build_cmd(effective_ssh_wrapper_cmd(), host, remote)
