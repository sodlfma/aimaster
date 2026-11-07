"""Reversible masking of hostnames / IPs / JDBC URLs / user names before text
leaves the organisation. Tokens are stable within one Masker instance so the
LLM can refer to HOST_1 consistently and we can un-mask the answer."""
from __future__ import annotations

import re
from typing import Any

_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_JDBC = re.compile(r"jdbc:[a-z0-9]+:(?://)?[^\s\"']+", re.I)
_HOST = re.compile(r"\b(?=[a-z0-9-]*[a-z])[a-z0-9-]+(?:\.[a-z0-9-]+)+\b", re.I)  # dotted names with a letter
_USER = re.compile(r"\b(?:user(?:name)?|uid)\s*=\s*([A-Za-z0-9_.-]+)", re.I)

_SKIP_HOSTS = {"localhost", "127.0.0.1", "example.com", "jmeter.apache.org"}
_SKIP_SUFFIX = (".xml", ".conf", ".env", ".sh", ".log", ".jtl", ".jmx", ".json", ".properties", ".java", ".jar", ".py")


class Masker:
    def __init__(self, enabled: bool = True, extra_hosts: list[str] | None = None):
        self.enabled = enabled
        self.map: dict[str, str] = {}
        self.rev: dict[str, str] = {}
        self.counts = {"HOST": 0, "IP": 0, "JDBC": 0, "USER": 0}
        for h in extra_hosts or []:
            if h:
                self._token("HOST", h)

    def _token(self, kind: str, original: str) -> str:
        if original in self.map:
            return self.map[original]
        self.counts[kind] += 1
        tok = f"{kind}_{self.counts[kind]}"
        self.map[original] = tok
        self.rev[tok] = original
        return tok

    def mask(self, text: str) -> str:
        if not self.enabled or not text:
            return text
        out = text
        out = _JDBC.sub(lambda m: self._token("JDBC", m.group(0)), out)
        out = _IP.sub(lambda m: self._token("IP", m.group(0)), out)

        def host_sub(m: re.Match) -> str:
            h = m.group(0)
            if h.lower() in _SKIP_HOSTS or h.lower().endswith(_SKIP_SUFFIX) or h.replace(".", "").isdigit():
                return h
            # version-like tokens (1.2.3) or property keys (tomcat.connector.maxThreads) are not hosts
            if re.fullmatch(r"[\d.]+", h) or re.search(r"\.(maxThreads|acceptCount|maxTotal|port|connector|dbcp|save|saveservice)\b", h, re.I):
                return h
            return self._token("HOST", h)

        out = _HOST.sub(host_sub, out)
        out = _USER.sub(lambda m: m.group(0).replace(m.group(1), self._token("USER", m.group(1))), out)
        # Pre-registered hosts (from target_info) even if they are single-label names
        for original, tok in list(self.map.items()):
            if tok.startswith("HOST_") and original in out and not re.search(rf"\b{re.escape(tok)}\b", out):
                out = out.replace(original, tok)
        return out

    def unmask(self, obj: Any) -> Any:
        if not self.enabled or not self.rev:
            return obj
        if isinstance(obj, str):
            out = obj
            for tok, original in sorted(self.rev.items(), key=lambda kv: -len(kv[0])):
                out = out.replace(tok, original)
            return out
        if isinstance(obj, list):
            return [self.unmask(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.unmask(v) for k, v in obj.items()}
        if hasattr(obj, "model_dump") and hasattr(obj, "model_validate"):
            return obj.__class__.model_validate(self.unmask(obj.model_dump()))
        return obj
