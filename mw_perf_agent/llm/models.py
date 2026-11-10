"""Chat model factory. Nodes call get_model(); tests inject a fake with
set_model_override(). Nothing here is stored in graph state or checkpoints."""
from __future__ import annotations

import os
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from config.settings import get_settings

_OVERRIDE: Any = None
_OVERRIDE_SET = False
_CACHE: dict[str, Any] = {}

# OpenAI SDK appends these itself. Leaving them on base_url posts to a path
# the gateway did not register for POST (HTTP 405).
_OPENAI_PATH_SUFFIXES = ("/chat/completions", "/completions", "/responses")


def set_model_override(model: Any) -> None:
    """Force a specific model (or None to force offline mode) regardless of settings."""
    global _OVERRIDE, _OVERRIDE_SET
    _OVERRIDE, _OVERRIDE_SET = model, True


def clear_model_override() -> None:
    global _OVERRIDE, _OVERRIDE_SET
    _OVERRIDE, _OVERRIDE_SET = None, False


def uses_openai_chat_endpoint(model: str) -> bool:
    """True for `openai:<model>` and bare OpenAI model names.

    `azure_openai:` is a different client (deployment URL + api-version) and is
    left untouched.
    """
    name = (model or "").strip()
    if not name:
        return False
    if ":" in name:
        provider = name.split(":", 1)[0].replace("-", "_").lower()
        return provider == "openai"
    lower = name.lower()
    return lower.startswith(("gpt-", "o1", "o3", "o4", "chatgpt", "text-davinci"))


def normalize_openai_base_url(url: str) -> str:
    """Return a base URL the OpenAI SDK can suffix with `/chat/completions`.

    The SDK does not insert `/v1`. `https://gateway.example` is therefore posted
    to `https://gateway.example/chat/completions`. That path is usually a
    GET-only site root, and the gateway answers HTTP 405. A base that already
    ends in `/chat/completions` or `/responses` is doubled the same way.
    """
    raw = (url or "").strip()
    if not raw:
        return raw
    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return raw.rstrip("/")
    path = parts.path.rstrip("/")
    while True:
        lowered = path.lower()
        suffix = next((s for s in _OPENAI_PATH_SUFFIXES if lowered.endswith(s)), "")
        if not suffix:
            break
        path = path[: -len(suffix)].rstrip("/")
    if path in ("", "/"):
        path = "/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _openai_base_url(explicit: Optional[str]) -> str:
    """Sidebar / LLM_BASE_URL wins. Otherwise the OpenAI client's own env vars."""
    chosen = (explicit or "").strip()
    if chosen:
        return chosen
    return (os.environ.get("OPENAI_API_BASE") or os.environ.get("OPENAI_BASE_URL") or "").strip()


def build_chat_model_kwargs(
    *,
    model: str,
    base_url: Optional[str],
    api_key: Optional[str],
    temperature: float,
    timeout: int,
) -> dict[str, Any]:
    """Arguments for `init_chat_model`, with a compatible endpoint forced onto Chat Completions."""
    kwargs: dict[str, Any] = {"temperature": temperature, "timeout": timeout}
    if uses_openai_chat_endpoint(model):
        effective = _openai_base_url(base_url)
        if effective:
            # Custom gateways implement `/v1/chat/completions`. langchain-openai
            # otherwise routes some model names to POST `/v1/responses`, which
            # those gateways reject with 405.
            kwargs["base_url"] = normalize_openai_base_url(effective)
            kwargs["use_responses_api"] = False
    elif base_url and base_url.strip():
        kwargs["base_url"] = base_url.strip()
    if api_key:
        kwargs["api_key"] = api_key
    return kwargs


def get_model(temperature: float = 0.0) -> Optional[Any]:
    if _OVERRIDE_SET:
        return _OVERRIDE
    s = get_settings()
    if not s.llm_enabled or not s.llm_model:
        return None
    kwargs = build_chat_model_kwargs(
        model=s.llm_model,
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        temperature=temperature,
        timeout=s.llm_timeout_sec,
    )
    key = f"{s.llm_model}|{kwargs.get('base_url')}|{temperature}|{bool(s.llm_api_key)}|{kwargs.get('use_responses_api')}"
    if key in _CACHE:
        return _CACHE[key]
    try:
        from langchain.chat_models import init_chat_model
    except ImportError:  # pragma: no cover
        from langchain_core.language_models import BaseChatModel  # noqa: F401
        return None
    try:
        model = init_chat_model(s.llm_model, **kwargs)
    except Exception:
        return None
    _CACHE[key] = model
    return model


def llm_available() -> bool:
    return get_model() is not None
