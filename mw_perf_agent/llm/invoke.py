"""Common LLM invocation: render prompt → mask → structured output → unmask → record."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional, Type

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from config.settings import get_settings
from .masking import Masker
from .models import get_model
from .registry import render_prompt


class LLMUnavailable(RuntimeError):
    pass


@dataclass
class LLMCallRecord:
    node: str
    prompt: str
    prompt_version: str
    model: str
    approx_input_tokens: int
    latency_ms: int
    ok: bool
    error: str = ""

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _model_name(model: Any) -> str:
    for attr in ("model_name", "model", "model_id"):
        v = getattr(model, attr, None)
        if isinstance(v, str):
            return v
    return model.__class__.__name__


def invoke_llm(
    node: str,
    prompt_name: str,
    schema: Optional[Type[BaseModel]],
    ctx: dict,
    *,
    temperature: float = 0.0,
    mask_hosts: Optional[list[str]] = None,
) -> tuple[Any, LLMCallRecord]:
    """Returns (parsed_output, record). Raises LLMUnavailable when no model is configured
    and re-raises provider errors (the graph RetryPolicy handles retries)."""
    model = get_model(temperature=temperature)
    if model is None:
        raise LLMUnavailable("LLM model not configured")
    settings = get_settings()
    prompt = render_prompt(prompt_name, **ctx)
    masker = Masker(enabled=settings.mask_enabled, extra_hosts=mask_hosts)
    system = masker.mask(prompt.system)
    user = masker.mask(prompt.user)
    if prompt.approx_tokens() > settings.llm_max_context_tokens:
        # Hard cap: truncate the user part (data section) rather than fail.
        budget = max(1000, settings.llm_max_context_tokens * 3 - len(system))
        user = user[:budget] + "\n...(truncated by context budget)"
    runnable = model.with_structured_output(schema) if schema is not None else model
    messages = [SystemMessage(content=system), HumanMessage(content=user)] if system else [HumanMessage(content=user)]
    t0 = time.time()
    try:
        out = runnable.invoke(messages)
    except Exception as e:  # provider error → let RetryPolicy decide
        rec = LLMCallRecord(node, prompt_name, prompt.version, _model_name(model), prompt.approx_tokens(),
                            int((time.time() - t0) * 1000), False, str(e)[:300])
        raise RuntimeError(f"LLM call failed in {node}: {e}") from e
    latency = int((time.time() - t0) * 1000)
    if schema is None and hasattr(out, "content"):
        out = out.content
    out = masker.unmask(out)
    rec = LLMCallRecord(node, prompt_name, prompt.version, _model_name(model), prompt.approx_tokens(), latency, True)
    return out, rec
