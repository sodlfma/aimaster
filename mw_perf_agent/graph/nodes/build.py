"""SC-002A deterministic nodes."""
from __future__ import annotations

from config.settings import get_settings
from jmeter.bundle import write_bundle
from jmeter.render import scenario_from_url as _from_url
from jmeter.validate import validate_jmx as _validate
from ..state import PerfAgentState


def _diagnosis_md(state: PerfAgentState) -> str:
    rep = state.get("diagnosis_report")
    if rep is None:
        return ""
    lines = ["## 진단 요약", rep.summary or ""]
    if rep.antipatterns:
        lines.append("\n### 설정 안티패턴")
        lines += [f"- **{a.title}** ({a.severity}): {a.detail}" for a in rep.antipatterns]
    if rep.rule_findings:
        lines.append("\n### 규칙 기반 탐지")
        lines += [f"- {f}" for f in rep.rule_findings]
    if rep.recommendations:
        lines.append("\n### 권고 설정 초안")
        lines += [f"- {r.component}.{r.parameter}: {r.current} → {r.proposed} ({r.rationale})" for r in rep.recommendations]
    return "\n".join(lines)


def scenario_from_url(state: PerfAgentState) -> dict:
    spec = _from_url(state["scenario_input"])
    return {"scenario_spec": spec, "node_log": [f"scenario_from_url: {spec.protocol}://{spec.host}:{spec.port} {len(spec.requests)} req"]}


def render_bundle(state: PerfAgentState) -> dict:
    s = get_settings()
    run_dir = s.run_dir(state["run_id"])
    bundle = write_bundle(
        run_dir, state["run_id"], state["scenario_spec"], state["load_plan"], state.get("thresholds"),
        diagnosis_md=_diagnosis_md(state), summariser_interval=s.summariser_interval,
    )
    return {"bundle": bundle, "node_log": [f"render_bundle: {bundle.jmx}"]}


def validate_jmx(state: PerfAgentState) -> dict:
    s = get_settings()
    opts = state.get("options") or {}
    result = _validate(
        state["bundle"].jmx,
        expected_steps=len(state["load_plan"].steps),
        jmeter_bin=s.jmeter_bin,
        dry_run=bool(opts.get("dry_run", True)),
    )
    return {"validation": result, "node_log": [f"validate_jmx: ok={result.ok} {[c['name'] for c in result.checks if c['ok']]}"]}
