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
    provider_input_tokens: int | None = None
    provider_output_tokens: int | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _usage_from(obj: Any) -> tuple[int | None, int | None]:
    """Read LangChain/OpenAI usage if the provider attached it. Missing is fine."""
    meta = getattr(obj, "usage_metadata", None) or {}
    if not isinstance(meta, dict):
        return None, None
    inp = meta.get("input_tokens")
    out = meta.get("output_tokens")
    try:
        return (int(inp) if inp is not None else None, int(out) if out is not None else None)
    except (TypeError, ValueError):
        return None, None


def _is_schema_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "invalid schema" in text or "response_format" in text or ("strict" in text and "schema" in text)


def _bind_structured(model: Any, schema: Type[BaseModel], **kwargs: Any):
    """Build a structured-output runnable. Fake models only accept `schema`."""
    try:
        return model.with_structured_output(schema, **kwargs)
    except TypeError:
        kwargs = {k: v for k, v in kwargs.items() if k in ("method",)}
        try:
            return model.with_structured_output(schema, **kwargs) if kwargs else model.with_structured_output(schema)
        except TypeError:
            return model.with_structured_output(schema)


def _invoke_structured(model: Any, schema: Type[BaseModel], messages: list) -> tuple[Any, bool]:
    """Invoke structured output. On OpenAI strict json_schema 400, retry function_calling.

    gpt-5 rejects some Pydantic models (`dict[str,str]`, extra required keys). Fake
    test models succeed on the first bind and never hit the retry.
    """
    last: Exception | None = None
    for method in (None, "function_calling"):
        kw: dict[str, Any] = {"include_raw": True}
        if method:
            kw["method"] = method
        runnable = _bind_structured(model, schema, **kw)
        try:
            return runnable.invoke(messages), True
        except TypeError:
            runnable = _bind_structured(model, schema, **({} if method is None else {"method": method}))
            try:
                return runnable.invoke(messages), False
            except Exception as e:  # noqa: BLE001
                last = e
                if method is None and _is_schema_error(e):
                    continue
                raise
        except Exception as e:  # noqa: BLE001
            last = e
            if method is None and _is_schema_error(e):
                continue
            raise
    if last is not None:
        raise last
    raise RuntimeError("structured output bind failed")


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
    messages = [SystemMessage(content=system), HumanMessage(content=user)] if system else [HumanMessage(content=user)]
    t0 = time.time()
    include_raw = False
    try:
        if schema is not None:
            out, include_raw = _invoke_structured(model, schema, messages)
        else:
            out = model.invoke(messages)
    except Exception as e:  # provider error → let RetryPolicy decide
        rec = LLMCallRecord(node, prompt_name, prompt.version, _model_name(model), prompt.approx_tokens(),
                            int((time.time() - t0) * 1000), False, str(e)[:300])
        raise RuntimeError(f"LLM call failed in {node}: {e}") from e
    latency = int((time.time() - t0) * 1000)
    provider_in = provider_out = None
    if include_raw and isinstance(out, dict):
        raw_msg = out.get("raw")
        parsed = out.get("parsed")
        provider_in, provider_out = _usage_from(raw_msg)
        if parsed is None:
            err = out.get("parsing_error") or "structured output parsed=None"
            rec = LLMCallRecord(node, prompt_name, prompt.version, _model_name(model), prompt.approx_tokens(),
                                latency, False, str(err)[:300], provider_in, provider_out)
            raise RuntimeError(f"LLM call failed in {node}: {err}")
        out = parsed
    elif schema is None:
        provider_in, provider_out = _usage_from(out)
        if hasattr(out, "content"):
            out = out.content
    out = masker.unmask(out)
    rec = LLMCallRecord(
        node, prompt_name, prompt.version, _model_name(model), prompt.approx_tokens(), latency, True, "",
        provider_in, provider_out,
    )
    return out, rec
