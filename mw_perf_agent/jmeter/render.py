"""Jinja2 rendering of the JMX plan, user.properties and run.sh."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from model.schemas import LoadPlan, RequestSpec, ScenarioInput, ScenarioSpec, Assertion

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=False,  # templates escape explicitly with |e where XML needs it
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)


def render_jmx(scenario: ScenarioSpec, plan: LoadPlan, run_id: str) -> str:
    return _env.get_template("plan.jmx.j2").render(scenario=scenario, plan=plan, run_id=run_id)


def render_properties(scenario: ScenarioSpec, plan: LoadPlan, run_id: str, summariser_interval: int = 5) -> str:
    return _env.get_template("user.properties.j2").render(
        scenario=scenario, plan=plan, run_id=run_id, summariser_interval=summariser_interval
    )


def render_run_sh(run_id: str, summariser_interval: int = 5) -> str:
    return _env.get_template("run.sh.j2").render(run_id=run_id, summariser_interval=summariser_interval)


def scenario_from_url(inp: ScenarioInput) -> ScenarioSpec:
    """Deterministic ScenarioSpec from a single URL (+ method/body/headers)."""
    url = inp.payload.strip()
    if "://" not in url:
        url = f"{inp.protocol}://{url}"
    parsed = urlparse(url)
    protocol = (parsed.scheme or inp.protocol or "http").lower()
    if protocol not in ("http", "https"):
        protocol = "http"
    host = parsed.hostname or inp.host or "localhost"
    port = parsed.port or inp.port or (443 if protocol == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    method = (inp.method or "GET").upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        method = "GET"
    headers = dict(inp.headers)
    body = inp.body or None
    if body and not any(k.lower() == "content-type" for k in headers):
        headers["Content-Type"] = "application/json"
    req = RequestSpec(
        label=f"{method} {parsed.path or '/'}",
        method=method,
        path=path,
        headers=headers,
        body=body,
        assertions=[Assertion(kind="response_code", expected="200")],
    )
    return ScenarioSpec(
        name=f"MW Perf-Agent: {host}",
        protocol=protocol,
        host=host,
        port=port,
        requests=[req],
        think_time_ms=inp.think_time_ms,
    )


def apply_user_target(spec: ScenarioSpec, inp: ScenarioInput) -> ScenarioSpec:
    """Host/port/protocol always come from the user, never from the LLM."""
    data = spec.model_dump()
    if inp.host:
        data["host"] = inp.host
    if inp.port:
        data["port"] = inp.port
    if inp.protocol:
        data["protocol"] = inp.protocol
    if inp.think_time_ms:
        data["think_time_ms"] = inp.think_time_ms
    return ScenarioSpec(**data)
