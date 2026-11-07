"""Common LLM invocation: render prompt → mask → structured output → unmask → record."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Optional, Type

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from config.settings import get_settings
from .masking import Masker
from .models import get_model, normalize_openai_base_url
from .registry import render_prompt

_NON_RETRYABLE = re.compile(
    r"Error code: (400|401|403|404|405|422)\b|HTTP 405|Method Not Allowed",
    re.I,
)


def is_retryable_llm_error(err: BaseException) -> bool:
    """Auth / wrong-URL errors will not succeed on retry."""
    cur: Optional[BaseException] = err
    for _ in range(6):
        if cur is None:
            break
        if _NON_RETRYABLE.search(str(cur)):
            return False
        cur = cur.__cause__ or getattr(cur, "__context__", None)
    return True


def explain_llm_error(err: BaseException, base_url: Optional[str] = None) -> str:
    text = str(err)
    shown = normalize_openai_base_url(base_url) if base_url else (base_url or "")
    if "405" in text:
        where = f" (Base URL: {shown})" if shown else ""
        return (
            f"HTTP 405 Method Not Allowed{where}. "
            "OpenAI 호환 서버는 POST /v1/chat/completions 만 받는 경우가 많습니다. "
            "Base URL은 /v1 까지만 넣으세요 (예: http://127.0.0.1:11434/v1). "
            "/chat/completions 나 /responses 는 붙이지 마세요."
        )
    if "401" in text or "403" in text:
        return f"LLM 인증 실패 (키/권한). {text[:160]}"
    if "404" in text:
        where = f" Base URL={shown}." if shown else ""
        return f"LLM 엔드포인트를 찾지 못했습니다.{where} 모델명과 Base URL을 확인하세요."
    return text[:300]


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
    if schema is not None:
        # function_calling stays on /v1/chat/completions; json_schema/responses
        # hit endpoints many 사내 호환 APIs reject with HTTP 405.
        try:
            runnable = model.with_structured_output(schema, method="function_calling")
        except TypeError:
            runnable = model.with_structured_output(schema)
    else:
        runnable = model
    messages = [SystemMessage(content=system), HumanMessage(content=user)] if system else [HumanMessage(content=user)]
    t0 = time.time()
    try:
        out = runnable.invoke(messages)
    except Exception as e:  # provider error → let RetryPolicy decide
        hint = explain_llm_error(e, get_settings().llm_base_url)
        rec = LLMCallRecord(node, prompt_name, prompt.version, _model_name(model), prompt.approx_tokens(),
                            int((time.time() - t0) * 1000), False, hint[:300])
        raise RuntimeError(f"LLM call failed in {node}: {hint}") from e
    latency = int((time.time() - t0) * 1000)
    if schema is None and hasattr(out, "content"):
        out = out.content
    out = masker.unmask(out)
    rec = LLMCallRecord(node, prompt_name, prompt.version, _model_name(model), prompt.approx_tokens(), latency, True)
    return out, rec
