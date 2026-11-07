"""Prompt registry: Jinja2 templates under llm/prompts with a metadata header.

Template files start with a Jinja comment such as
    {# version: 2.0 | schema: AnalysisReport #}
and contain two sections separated by a line `---USER---`:
system prompt first, user prompt second."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
_META = re.compile(r"\{#\s*version:\s*(?P<version>[\w.]+)\s*\|\s*schema:\s*(?P<schema>\w+)\s*#\}")

_env = Environment(loader=FileSystemLoader(str(PROMPT_DIR)), autoescape=False, undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
_env.filters["tojson"] = lambda v: json.dumps(v, ensure_ascii=False, default=str)


@dataclass
class RenderedPrompt:
    name: str
    version: str
    schema: str
    system: str
    user: str

    def approx_tokens(self) -> int:
        return (len(self.system) + len(self.user)) // 3


def render_prompt(name: str, **ctx) -> RenderedPrompt:
    src = (PROMPT_DIR / f"{name}.j2").read_text(encoding="utf-8")
    m = _META.search(src)
    version = m.group("version") if m else "0"
    schema = m.group("schema") if m else "str"
    text = _env.get_template(f"{name}.j2").render(**ctx)
    if "---USER---" in text:
        system, user = text.split("---USER---", 1)
    else:
        system, user = "", text
    return RenderedPrompt(name=name, version=version, schema=schema, system=system.strip(), user=user.strip())


def list_prompts() -> list[str]:
    return sorted(p.stem for p in PROMPT_DIR.glob("*.j2"))
