"""Deterministic regex parsers. parse_all() combines them into a ParsedConfig
with a confidence score that drives the after_parse router."""
from __future__ import annotations

from model.schemas import ParsedConfig, REQUIRED_FIELDS, TargetInfo
from .httpd_mpm import parse_httpd_mpm
from .tomcat_env import parse_tomcat_env, find_evidence_line
from .web_mon_env import parse_web_mon_env

TOMCAT_KEYS = ("tomcat.env", "server.xml", "setenv.sh", "catalina.sh")
HTTPD_KEYS = ("httpd-mpm.conf", "httpd.conf")
ENV_KEYS = ("web_mon_cron.env",)


def _collect(raw_inputs: dict[str, str], keys: tuple[str, ...]) -> str:
    parts = []
    for k, v in raw_inputs.items():
        if any(k.lower().endswith(key) or key in k.lower() for key in keys) and v:
            parts.append(v)
    return "\n".join(parts)


def compute_confidence(cfg: ParsedConfig, conflicts: list[str]) -> float:
    found = sum(1 for k in REQUIRED_FIELDS if getattr(cfg, k) is not None)
    score = found / len(REQUIRED_FIELDS)
    score -= 0.2 * len([c for c in conflicts if c in REQUIRED_FIELDS])
    return max(0.0, min(1.0, round(score, 2)))


def parse_all(raw_inputs: dict[str, str]) -> tuple[ParsedConfig, TargetInfo, list[str]]:
    tomcat_text = _collect(raw_inputs, TOMCAT_KEYS)
    httpd_text = _collect(raw_inputs, HTTPD_KEYS)
    env_text = _collect(raw_inputs, ENV_KEYS)
    # Unlabelled "server config" paste: try tomcat patterns on everything.
    if not tomcat_text:
        tomcat_text = "\n".join(v for k, v in raw_inputs.items() if v and not any(x in k.lower() for x in HTTPD_KEYS + ENV_KEYS))

    tomcat_found, conflicts = parse_tomcat_env(tomcat_text)
    httpd_found = parse_httpd_mpm(httpd_text)
    target = parse_web_mon_env(env_text)

    values: dict = {}
    evidence: dict[str, str] = {}
    for field, (val, ev) in {**httpd_found, **tomcat_found}.items():
        values[field] = val
        evidence[field] = ev

    cfg = ParsedConfig(**values, evidence=evidence, source="regex" if values else "default")
    cfg.confidence = compute_confidence(cfg, conflicts)
    warnings = [f"'{c}' 값이 여러 개 발견되어 첫 번째 값을 사용했습니다." for c in conflicts]
    return cfg, target, warnings


__all__ = ["parse_all", "parse_tomcat_env", "parse_httpd_mpm", "parse_web_mon_env", "find_evidence_line", "compute_confidence"]
