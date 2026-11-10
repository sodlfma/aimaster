"""Process-level settings. The UI sidebar mutates these at runtime; graph nodes
read them through get_settings() so that no secrets end up in checkpoint
metadata (only plain state values are persisted)."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

PKG_ROOT = Path(__file__).resolve().parent.parent


def normalize_host(host: str) -> str:
    """Strip user@, [brackets], and accidental :port so allow-list checks match."""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return ""
    if h.startswith("[") and "]" in h:
        h = h[1:h.index("]")]
    if "@" in h:
        h = h.rsplit("@", 1)[-1]
    if h.count(":") == 1:
        left, right = h.rsplit(":", 1)
        if right.isdigit():
            h = left
    return h


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # LLM
    llm_model: str = os.environ.get("LLM_MODEL", "")  # e.g. "gpt-4.1", "openai:gpt-4o", "azure_openai:gpt-4.1"
    llm_base_url: Optional[str] = os.environ.get("LLM_BASE_URL") or None
    llm_api_key: Optional[str] = os.environ.get("LLM_API_KEY") or None
    # Azure OpenAI 호환 게이트웨이(예: Talent Lab). 비우면 호스트에 따라 기본 api-version 을 쓴다.
    llm_api_version: Optional[str] = os.environ.get("LLM_API_VERSION") or None
    llm_enabled: bool = _env_bool("LLM_ENABLED", bool(os.environ.get("LLM_MODEL")))
    llm_timeout_sec: int = int(os.environ.get("LLM_TIMEOUT_SEC", "60"))
    llm_max_context_tokens: int = int(os.environ.get("LLM_MAX_CONTEXT_TOKENS", "12000"))
    mask_enabled: bool = _env_bool("LLM_MASK", True)
    interpret_enabled: bool = _env_bool("LLM_INTERPRET", True)
    # JMeter
    jmeter_home: Optional[str] = os.environ.get("JMETER_HOME") or None
    jmeter_bin_override: Optional[str] = os.environ.get("JMETER_BIN") or None
    docker_image: str = os.environ.get("JMETER_DOCKER_IMAGE", "justb4/jmeter:latest")
    remote_hosts: str = os.environ.get("JMETER_REMOTE_HOSTS", "")
    max_local_threads: int = int(os.environ.get("MAX_LOCAL_THREADS", "1000"))
    summariser_interval: int = 5
    # Paths
    runs_dir: str = os.environ.get("RUNS_DIR", str(PKG_ROOT / "runs"))
    allowed_hosts_file: str = os.environ.get("ALLOWED_HOSTS_FILE", str(PKG_ROOT / "config" / "allowed_hosts.txt"))
    extra_allowed_hosts: list[str] = field(default_factory=list)
    # SSH: OpenSSH 접속 정보 (비밀값은 경로만 보관, 그래프 상태에는 넣지 않음)
    ssh_wrapper_cmd: str = os.environ.get("SSH_WRAPPER_CMD", "")
    ssh_user: str = os.environ.get("SSH_USER", "")
    ssh_port: int = int(os.environ.get("SSH_PORT", "22") or "22")
    ssh_identity_file: str = os.environ.get("SSH_IDENTITY_FILE", "")
    ssh_password: str = os.environ.get("SSH_PASSWORD", "")
    ssh_strict_host_key: str = os.environ.get("SSH_STRICT_HOST_KEY", "accept-new")
    # Plan defaults
    bucket_sec: int = 5
    plan_ramp_scale: float = 1.0
    plan_hold_sec: int = 60
    extra: dict = field(default_factory=dict)

    @property
    def jmeter_bin(self) -> Optional[str]:
        if self.jmeter_bin_override:
            return self.jmeter_bin_override
        if self.jmeter_home:
            cand = Path(self.jmeter_home) / "bin" / "jmeter"
            if cand.exists():
                return str(cand)
        found = shutil.which("jmeter")
        return found

    @property
    def jmeter_available(self) -> bool:
        return self.jmeter_bin is not None

    def allowed_hosts_from_file(self) -> list[str]:
        p = Path(self.allowed_hosts_file)
        if not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
        return out

    def allowed_hosts(self) -> list[str]:
        seen: list[str] = []
        for host in self.allowed_hosts_from_file() + list(self.extra_allowed_hosts):
            if host and host not in seen:
                seen.append(host)
        return seen

    def allow_host(self, host: str, persist: bool = False) -> str:
        """Add a host to the runtime allow-list. persist=True appends the file."""
        host = normalize_host(host)
        if not host:
            return ""
        if host not in {normalize_host(h) for h in self.extra_allowed_hosts}:
            self.extra_allowed_hosts = list(self.extra_allowed_hosts) + [host]
        if persist:
            existing = {normalize_host(h) for h in self.allowed_hosts_from_file()}
            if host not in existing:
                p = Path(self.allowed_hosts_file)
                p.parent.mkdir(parents=True, exist_ok=True)
                prefix = "\n" if p.exists() and p.stat().st_size and not p.read_text(encoding="utf-8").endswith("\n") else ""
                with p.open("a", encoding="utf-8") as fh:
                    fh.write(f"{prefix}{host}\n")
        return host

    def run_dir(self, run_id: str) -> Path:
        d = Path(self.runs_dir) / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def as_dict(self) -> dict:
        d = asdict(self)
        d["llm_api_key"] = "***" if self.llm_api_key else None
        d["ssh_password"] = "***" if self.ssh_password else None
        return d


_SETTINGS: Settings = Settings()


def get_settings() -> Settings:
    return _SETTINGS


def allow_host(host: str, persist: bool = False) -> str:
    return get_settings().allow_host(host, persist=persist)


def update_settings(**kwargs) -> Settings:
    global _SETTINGS
    for k, v in kwargs.items():
        if hasattr(_SETTINGS, k):
            setattr(_SETTINGS, k, v)
        else:
            _SETTINGS.extra[k] = v
    return _SETTINGS


def reset_settings(settings: Optional[Settings] = None) -> Settings:
    global _SETTINGS
    _SETTINGS = settings or Settings()
    return _SETTINGS
