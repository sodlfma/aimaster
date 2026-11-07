"""Regex extraction from httpd-mpm.conf / httpd.conf."""
from __future__ import annotations

import re

Found = dict[str, tuple[object, str]]

_MAX_WORKERS = re.compile(r"^\s*(?:MaxRequestWorkers|MaxClients)\s+(\d+)", re.I)
_THREADS_PER_CHILD = re.compile(r"^\s*ThreadsPerChild\s+(\d+)", re.I)
_IFMODULE = re.compile(r"<IfModule\s+mpm_(prefork|worker|event)_module\s*>", re.I)
_LOADMODULE = re.compile(r"^\s*LoadModule\s+mpm_(prefork|worker|event)_module", re.I)


def parse_httpd_mpm(text: str) -> Found:
    found: Found = {}
    if not text:
        return found

    active_mpm = None
    for line in text.splitlines():
        m = _LOADMODULE.search(line)
        if m:
            active_mpm = m.group(1).lower()
            found["mpm_module"] = (active_mpm, line.strip())
            break

    current_block = None
    block_values: dict[str, dict[str, tuple[int, str]]] = {}
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        m = _IFMODULE.search(line)
        if m:
            current_block = m.group(1).lower()
            block_values.setdefault(current_block, {})
            continue
        if s.lower().startswith("</ifmodule"):
            current_block = None
            continue
        m = _MAX_WORKERS.match(line)
        if m:
            target = block_values.setdefault(current_block or "_global", {})
            target.setdefault("max_request_workers", (int(m.group(1)), s))
            continue
        m = _THREADS_PER_CHILD.match(line)
        if m:
            target = block_values.setdefault(current_block or "_global", {})
            target.setdefault("threads_per_child", (int(m.group(1)), s))

    # Prefer the block of the loaded MPM, then global, then event > worker > prefork.
    order = []
    if active_mpm:
        order.append(active_mpm)
    order += ["_global", "event", "worker", "prefork"]
    for key in order:
        vals = block_values.get(key)
        if not vals:
            continue
        for field, (v, ev) in vals.items():
            found.setdefault(field, (v, ev))
        if "mpm_module" not in found and key in ("event", "worker", "prefork"):
            found["mpm_module"] = (key, f"<IfModule mpm_{key}_module>")
        if "max_request_workers" in found:
            break
    return found
