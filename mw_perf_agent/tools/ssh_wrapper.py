"""Wrap the in-house SSH shell runner with subprocess (shell=False) — see spec 6.1."""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config.settings import get_settings

_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_PATH_RE = re.compile(r"^[A-Za-z0-9._/+-]+$")
SEPARATOR = "=== {path} ==="

DEFAULT_TOMCAT_REL = "shl/tomcat.env"
DEFAULT_APACHE_REL = "conf/httpd-mpm.conf"
DEFAULT_SSH_WRAPPER_CMD = "ssh -o BatchMode=yes -o ConnectTimeout=10 {host} cat {path}"
DEFAULT_TIMEOUT_SEC = 30
DEFAULT_SSH_PORT = 22
DEFAULT_STRICT_HOST_KEY = "accept-new"
STRICT_HOST_KEY_CHOICES = ("accept-new", "yes", "no")


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
    ssh_user: str = ""

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


def validate_user(user: str) -> str:
    user = (user or "").strip()
    if not user:
        return ""
    if not _USER_RE.match(user):
        raise ValueError(f"허용되지 않는 SSH 사용자명: {user!r}")
    return user


def validate_port(port: int | str | None) -> int:
    if port is None or port == "":
        return DEFAULT_SSH_PORT
    try:
        value = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"허용되지 않는 SSH 포트: {port!r}") from exc
    if not 1 <= value <= 65535:
        raise ValueError(f"허용되지 않는 SSH 포트: {port!r}")
    return value


def validate_strict_host_key(value: str | None) -> str:
    choice = (value or DEFAULT_STRICT_HOST_KEY).strip() or DEFAULT_STRICT_HOST_KEY
    if choice not in STRICT_HOST_KEY_CHOICES:
        raise ValueError(f"호스트 키 정책은 {STRICT_HOST_KEY_CHOICES} 중 하나여야 합니다: {value!r}")
    return choice


def validate_identity_file(path: str | None, *, must_exist: bool = True) -> str:
    path = (path or "").strip()
    if not path:
        return ""
    expanded = str(Path(path).expanduser())
    if must_exist and not Path(expanded).is_file():
        raise ValueError(f"SSH 개인키 파일이 없습니다: {expanded}")
    return expanded


def parse_user_host(host: str, user: str | None = None) -> tuple[str, str]:
    """`user@host` 입력을 분리한다. 명시적 user가 있으면 그쪽을 우선한다."""
    raw = (host or "").strip()
    inline_user = ""
    if "@" in raw:
        inline_user, _, raw = raw.rpartition("@")
        inline_user = inline_user.strip()
    host_ok = validate_host(raw)
    explicit = validate_user(user if user is not None else get_settings().ssh_user)
    return host_ok, explicit or validate_user(inline_user)


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


def uses_builtin_openssh(template: Optional[str] = None) -> bool:
    cmd = (template if template is not None else "").strip()
    if not cmd:
        cmd = (get_settings().ssh_wrapper_cmd or "").strip()
    return (not cmd) or cmd == DEFAULT_SSH_WRAPPER_CMD


def format_ssh_error(returncode: int, stderr: str) -> str:
    text = (stderr or "").strip()
    low = text.lower()
    hints: list[str] = []
    if "host key verification failed" in low:
        hints.append(
            "대상 서버의 SSH 호스트 키가 known_hosts에 없거나 바뀌었습니다. "
            "진단 화면의 호스트 키 정책을 '최초 접속 허용(accept-new)'으로 바꾸거나, "
            "`ssh-keyscan <host> >> ~/.ssh/known_hosts` 로 등록하세요."
        )
    if "permission denied" in low or "authentication failed" in low or "no such identity" in low:
        hints.append(
            "SSH 인증에 실패했습니다. 사용자명, 비밀번호, 개인키 경로를 확인하세요. "
            "비밀번호는 진단 화면의 SSH 접속 칸에 넣으면 ASKPASS로 전달됩니다."
        )
    if "could not resolve hostname" in low:
        hints.append("호스트명을 해석하지 못했습니다. 대상 호스트 값을 확인하세요.")
    if returncode == 255 and not hints:
        hints.append(
            "ssh 연결 단계 실패(exit 255). 사용자/비밀번호/포트/개인키/호스트 키 설정을 확인하세요. "
            "계정 정보는 진단 화면의 'SSH 접속'에서 입력합니다."
        )
    prefix = f"# error: exit {returncode}: {text[:300]}"
    if hints:
        return prefix + "\n# hint: " + " ".join(hints)
    return prefix


def effective_ssh_password(override: Optional[str] = None) -> str:
    if override is not None:
        return override
    return get_settings().ssh_password or ""


def askpass_executable() -> str:
    """Return a 0700 shell wrapper that runs ssh_askpass.py with this interpreter."""
    src = Path(__file__).resolve().parent / "ssh_askpass.py"
    dest_dir = Path(get_settings().runs_dir) / ".ssh-keys"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "askpass.sh"
    body = "#!/bin/sh\n" + f"exec {shlex.quote(sys.executable)} {shlex.quote(str(src))} \"$@\"\n"
    if not dest.exists() or dest.read_text(encoding="utf-8") != body:
        dest.write_text(body, encoding="utf-8")
        dest.chmod(0o700)
    return str(dest)


def ssh_exec_env(password: Optional[str] = None) -> dict[str, str]:
    """Copy os.environ and, if a password is set, wire SSH_ASKPASS (never argv)."""
    env = os.environ.copy()
    secret = effective_ssh_password(password)
    if not secret:
        return env
    env["MW_SSH_PASSWORD"] = secret
    env["SSHPASS"] = secret  # sshpass -e 를 쓰는 사내 래퍼용
    env["SSH_ASKPASS"] = askpass_executable()
    env["SSH_ASKPASS_REQUIRE"] = "force"
    env.setdefault("DISPLAY", ":0")
    return env


def _connection_from_settings(
    *,
    user: Optional[str] = None,
    port: Optional[int] = None,
    identity_file: Optional[str] = None,
    strict_host_key: Optional[str] = None,
    must_exist_identity: bool = True,
) -> tuple[str, int, str, str]:
    s = get_settings()
    user_ok = validate_user(user if user is not None else s.ssh_user)
    port_ok = validate_port(port if port is not None else s.ssh_port)
    identity_ok = validate_identity_file(
        identity_file if identity_file is not None else s.ssh_identity_file,
        must_exist=must_exist_identity,
    )
    strict_ok = validate_strict_host_key(
        strict_host_key if strict_host_key is not None else s.ssh_strict_host_key
    )
    return user_ok, port_ok, identity_ok, strict_ok


def build_ssh_argv(
    host: str,
    path: str,
    *,
    template: Optional[str] = None,
    user: Optional[str] = None,
    port: Optional[int] = None,
    identity_file: Optional[str] = None,
    strict_host_key: Optional[str] = None,
    remote_command: Optional[str] = None,
    must_exist_identity: bool = True,
) -> list[str]:
    """OpenSSH argv 또는 사내 래퍼 템플릿을 리스트로 만든다 (shell=False)."""
    host, parsed_user = parse_user_host(host, user)
    user_ok, port_ok, identity_ok, strict_ok = _connection_from_settings(
        user=parsed_user or user,
        port=port,
        identity_file=identity_file,
        strict_host_key=strict_host_key,
        must_exist_identity=must_exist_identity,
    )
    if not user_ok:
        user_ok = parsed_user
    tmpl = effective_ssh_wrapper_cmd(template)
    password = effective_ssh_password()
    if uses_builtin_openssh(template):
        cmd = [
            "ssh",
            "-o", "ConnectTimeout=10",
            "-o", f"StrictHostKeyChecking={strict_ok}",
            "-p", str(port_ok),
        ]
        if password:
            # BatchMode=yes 는 비밀번호 프롬프트를 막아 ASKPASS가 동작하지 않는다.
            cmd += [
                "-o", "NumberOfPasswordPrompts=1",
                "-o", "PasswordAuthentication=yes",
                "-o", "KbdInteractiveAuthentication=yes",
            ]
            if identity_ok:
                cmd += ["-o", "PreferredAuthentications=publickey,password,keyboard-interactive"]
            else:
                cmd += [
                    "-o", "PreferredAuthentications=password,keyboard-interactive",
                    "-o", "PubkeyAuthentication=no",
                ]
        else:
            cmd += ["-o", "BatchMode=yes"]
        if identity_ok:
            cmd += ["-i", identity_ok, "-o", "IdentitiesOnly=yes"]
        if user_ok:
            cmd += ["-l", user_ok]
        remote = remote_command if remote_command is not None else f"cat {path}"
        cmd += [host] + shlex.split(remote)
        return cmd
    if not tmpl:
        raise RemoteConfigError("SSH 래퍼 명령 템플릿이 비어 있습니다 (예: '/opt/tools/sshrun {host} cat {path}').")
    if "{host}" not in tmpl or "{path}" not in tmpl:
        raise RemoteConfigError("SSH 래퍼 명령에는 {host}와 {path} 자리표시자가 필요합니다.")
    path_token = path if remote_command is None else remote_command
    return [
        part.replace("{host}", host)
        .replace("{path}", path_token)
        .replace("{user}", user_ok)
        .replace("{port}", str(port_ok))
        .replace("{identity}", identity_ok)
        for part in shlex.split(tmpl)
    ]


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
    parse_user_host(host)  # validate; user@host 는 build_ssh_argv 가 분리
    template = effective_ssh_wrapper_cmd(ssh_wrapper_cmd)
    out: dict[str, str] = {}
    for path in paths:
        if not _PATH_RE.match(path):
            out[path] = f"# skipped: 허용되지 않는 경로 문자 ({path})"
            continue
        cmd = build_ssh_argv(host, path, template=template)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
                env=ssh_exec_env(),
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            out[path] = f"# error: timeout after {timeout}s"
            continue
        except OSError as e:
            out[path] = f"# error: {e}"
            continue
        if proc.returncode != 0:
            out[path] = format_ssh_error(proc.returncode, proc.stderr or "")
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
    host_ok, user = parse_user_host(host)
    apache_remote = resolve_remote_path(apache_path, DEFAULT_APACHE_REL)
    tomcat_remote = resolve_remote_path(tomcat_path, DEFAULT_TOMCAT_REL)
    template = effective_ssh_wrapper_cmd(ssh_wrapper_cmd)
    configs = RemoteConfigs(
        host=host_ok,
        apache_path=apache_path,
        tomcat_path=tomcat_path,
        apache_remote=apache_remote,
        tomcat_remote=tomcat_remote,
        wrapper_cmd=template,
        ssh_user=user,
    )
    fetched = fetch_remote_configs(
        host, [tomcat_remote, apache_remote], timeout=timeout, ssh_wrapper_cmd=ssh_wrapper_cmd
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
    parse_user_host(host)
    remote = f"jstat -gcutil {int(pid)} {int(interval_ms)} {int(count)}"
    return build_ssh_argv(host, remote, remote_command=remote)
