"""Wrap the in-house SSH shell runner with subprocess (shell=False) — see spec 6.1."""
from __future__ import annotations

import re
import shlex
import subprocess
from typing import Optional

from config.settings import get_settings

_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_PATH_RE = re.compile(r"^[A-Za-z0-9._/+-]+$")
SEPARATOR = "=== {path} ==="


def validate_host(host: str) -> str:
    if not host or not _HOST_RE.match(host):
        raise ValueError(f"허용되지 않는 호스트명 형식: {host!r}")
    return host


def _build_cmd(template: str, host: str, path: str) -> list[str]:
    if not template:
        raise RuntimeError("SSH_WRAPPER_CMD 설정이 비어 있습니다 (예: '/opt/tools/sshrun {host} cat {path}')")
    return [part.replace("{host}", host).replace("{path}", path) for part in shlex.split(template)]


def fetch_remote_configs(host: str, paths: list[str], timeout: int = 30) -> dict[str, str]:
    """Return {filename: content}. Raises on invalid input; returns partial results on per-file failure."""
    settings = get_settings()
    host = validate_host(host)
    out: dict[str, str] = {}
    for path in paths:
        if not _PATH_RE.match(path):
            out[path] = f"# skipped: 허용되지 않는 경로 문자 ({path})"
            continue
        cmd = _build_cmd(settings.ssh_wrapper_cmd, host, path)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=False)
        except subprocess.TimeoutExpired:
            out[path] = f"# error: timeout after {timeout}s"
            continue
        except OSError as e:
            out[path] = f"# error: {e}"
            continue
        if proc.returncode != 0:
            out[path] = f"# error: exit {proc.returncode}: {proc.stderr.strip()[:300]}"
        else:
            out[path] = proc.stdout
    return out


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


def collect_gc_stats_cmd(host: str, pid: int, interval_ms: int, count: int) -> list[str]:
    settings = get_settings()
    host = validate_host(host)
    remote = f"jstat -gcutil {int(pid)} {int(interval_ms)} {int(count)}"
    return _build_cmd(settings.ssh_wrapper_cmd, host, remote)
