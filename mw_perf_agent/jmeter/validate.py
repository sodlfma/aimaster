"""Three-stage JMX validation (SC-002A 3.2.4)."""
from __future__ import annotations

import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from model.schemas import ValidationResult


def _check_well_formed(jmx_text: str) -> tuple[bool, str, Optional[ET.Element]]:
    try:
        root = ET.fromstring(jmx_text)
        return True, "XML well-formed", root
    except ET.ParseError as e:
        return False, f"XML parse error: {e}", None


def _check_structure(root: ET.Element, expected_steps: int) -> tuple[bool, str]:
    problems = []
    if root.tag != "jmeterTestPlan":
        problems.append("root element is not jmeterTestPlan")
    if len(root.findall(".//TestPlan")) != 1:
        problems.append("TestPlan element count != 1")
    tgs = root.findall(".//ThreadGroup")
    if len(tgs) != expected_steps:
        problems.append(f"ThreadGroup count {len(tgs)} != plan steps {expected_steps}")
    if not root.findall(".//HTTPSamplerProxy"):
        problems.append("no HTTPSamplerProxy")
    # Every test element must be followed by a sibling hashTree (JMeter's tree encoding).
    for parent in root.iter():
        children = list(parent)
        for idx, child in enumerate(children):
            if child.tag == "hashTree":
                continue
            if child.tag in ("TestPlan", "ThreadGroup", "HTTPSamplerProxy", "HeaderManager", "CookieManager",
                             "JSONPostProcessor", "ResponseAssertion", "ConstantTimer"):
                nxt = children[idx + 1] if idx + 1 < len(children) else None
                if nxt is None or nxt.tag != "hashTree":
                    problems.append(f"{child.tag} '{child.get('testname')}' is not followed by hashTree")
    if problems:
        return False, "; ".join(problems)
    return True, f"structure ok ({len(tgs)} ThreadGroups, {len(root.findall('.//HTTPSamplerProxy'))} samplers)"


def _check_dry_run(jmx_path: str, jmeter_bin: Optional[str], steps: int, timeout: int = 90) -> tuple[bool, str, bool]:
    """Returns (ok, detail, executed)."""
    if not jmeter_bin:
        return True, "skipped (jmeter not installed)", False
    with tempfile.TemporaryDirectory() as td:
        args = [jmeter_bin, "-n", "-t", jmx_path, "-l", os.path.join(td, "dry.jtl"), "-j", os.path.join(td, "dry.log"),
                "-Jtarget.host=127.0.0.1", "-Jtarget.port=9", "-Jtarget.connect_timeout=200", "-Jtarget.response_timeout=200"]
        for i in range(1, steps + 1):
            args += [f"-Jstep{i}.threads=1", f"-Jstep{i}.rampup=1", f"-Jstep{i}.duration=1"]
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "dry-run timeout", True
        except OSError as e:
            return False, f"dry-run failed to start: {e}", True
        log = Path(td, "dry.log").read_text(encoding="utf-8", errors="replace") if Path(td, "dry.log").exists() else ""
        fatal = [l for l in log.splitlines() if "ERROR" in l and ("Conversion" in l or "could not" in l.lower() or "Problem loading" in l)]
        if proc.returncode != 0 or fatal:
            return False, f"dry-run exit={proc.returncode}; {fatal[:3] or proc.stderr[-500:]}", True
        return True, "dry-run ok (1 thread × 1s per step)", True


def validate_jmx(jmx_path: str, expected_steps: int, jmeter_bin: Optional[str] = None, dry_run: bool = True) -> ValidationResult:
    text = Path(jmx_path).read_text(encoding="utf-8")
    checks: list[dict] = []
    errors: list[str] = []

    ok, detail, root = _check_well_formed(text)
    checks.append({"name": "well_formed", "ok": ok, "detail": detail})
    if not ok:
        errors.append(detail)
        return ValidationResult(ok=False, checks=checks, errors=errors)

    ok, detail = _check_structure(root, expected_steps)
    checks.append({"name": "structure", "ok": ok, "detail": detail})
    if not ok:
        errors.append(detail)

    if dry_run:
        ok3, detail3, executed = _check_dry_run(jmx_path, jmeter_bin, expected_steps)
        checks.append({"name": "dry_run", "ok": ok3, "detail": detail3, "executed": executed})
        if not ok3:
            errors.append(detail3)

    return ValidationResult(ok=not errors, checks=checks, errors=errors)
