"""Chat model factory. Nodes call get_model(); tests inject a fake with
set_model_override(). Nothing here is stored in graph state or checkpoints."""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

from config.settings import get_settings

_OVERRIDE: Any = None
_OVERRIDE_SET = False
_CACHE: dict[str, Any] = {}

# OpenAI SDK always appends /chat/completions (or /responses) onto base_url.
_STRIP_SUFFIXES = (
    "/v1/chat/completions",
    "/chat/completions",
    "/v1/completions",
    "/completions",
    "/v1/responses",
    "/responses",
)


def normalize_openai_base_url(url: str) -> str:
    """Turn a pasted chat-completions URL into the root the OpenAI SDK expects.

    Users often paste `http://host:8000/v1/chat/completions`. The client then
    POSTs `.../chat/completions/chat/completions` and compatible gateways
    respond 405 Method Not Allowed. Empty paths get `/v1` so Ollama/vLLM work.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    path = (parsed.path or "").rstrip("/")
    lower = path.lower()
    for suffix in _STRIP_SUFFIXES:
        if lower.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            lower = path.lower()
            break
    if path in ("", "/"):
        path = "/v1"
    return urlunparse(parsed._replace(path=path, query="", fragment="")).rstrip("/")


def _provider_of(model_id: str) -> str:
    if ":" in (model_id or ""):
        return model_id.split(":", 1)[0].strip().lower()
    return "openai"


def set_model_override(model: Any) -> None:
    """Force a specific model (or None to force offline mode) regardless of settings."""
    global _OVERRIDE, _OVERRIDE_SET
    _OVERRIDE, _OVERRIDE_SET = model, True


def clear_model_override() -> None:
    global _OVERRIDE, _OVERRIDE_SET
    _OVERRIDE, _OVERRIDE_SET = None, False


def get_model(temperature: float = 0.0) -> Optional[Any]:
    if _OVERRIDE_SET:
        return _OVERRIDE
    s = get_settings()
    if not s.llm_enabled or not s.llm_model:
        return None
    provider = _provider_of(s.llm_model)
    base_url = s.llm_base_url
    if base_url and provider in ("openai", "ollama"):
        base_url = normalize_openai_base_url(base_url)
    key = f"{s.llm_model}|{base_url}|{temperature}|{bool(s.llm_api_key)}"
    if key in _CACHE:
        return _CACHE[key]
    try:
        from langchain.chat_models import init_chat_model
    except ImportError:  # pragma: no cover
        from langchain_core.language_models import BaseChatModel  # noqa: F401
        return None
    kwargs: dict[str, Any] = {"temperature": temperature, "timeout": s.llm_timeout_sec}
    if base_url:
        kwargs["base_url"] = base_url
        # langchain-openai 1.x may POST /v1/responses; most OpenAI-compatible
        # gateways only implement /v1/chat/completions and answer 405 otherwise.
        if provider in ("openai", "ollama"):
            kwargs["use_responses_api"] = False
    if s.llm_api_key:
        kwargs["api_key"] = s.llm_api_key
    try:
        model = init_chat_model(s.llm_model, **kwargs)
    except TypeError:
        kwargs.pop("use_responses_api", None)
        try:
            model = init_chat_model(s.llm_model, **kwargs)
        except Exception:
            return None
    except Exception:
        return None
    _CACHE[key] = model
    return model


def llm_available() -> bool:
    return get_model() is not None
