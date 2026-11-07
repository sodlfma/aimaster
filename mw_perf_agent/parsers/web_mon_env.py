"""Parse web_mon_cron.env (KEY=VALUE shell-style) into TargetInfo."""
from __future__ import annotations

import re

from model.schemas import TargetInfo

_KV = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")

_HOST_KEYS = ("HOST", "HOSTNAME", "TARGET_HOST", "SERVER_NAME", "WEB_HOST", "WAS_HOST")
_APACHE_KEYS = ("APACHE_HOME", "HTTPD_HOME", "WEB_HOME", "APACHE_PATH")
_TOMCAT_KEYS = ("TOMCAT_HOME", "CATALINA_HOME", "WAS_HOME", "TOMCAT_PATH", "CATALINA_BASE")
_INSTANCE_KEYS = ("INSTANCES", "INSTANCE_LIST", "TOMCAT_INSTANCES", "WAS_INSTANCES")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v.split(" #")[0].strip()


def parse_web_mon_env(text: str) -> TargetInfo:
    kv: dict[str, str] = {}
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = _KV.match(s)
        if m:
            kv[m.group(1).upper()] = _unquote(m.group(2))

    def pick(keys: tuple[str, ...]) -> str | None:
        for k in keys:
            if kv.get(k):
                return kv[k]
        return None

    instances_raw = pick(_INSTANCE_KEYS) or ""
    instances = [i for i in re.split(r"[,\s]+", instances_raw) if i]
    return TargetInfo(
        host=pick(_HOST_KEYS) or "",
        apache_home=pick(_APACHE_KEYS),
        tomcat_home=pick(_TOMCAT_KEYS),
        instances=instances,
    )
