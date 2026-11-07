"""Chat model factory. Nodes call get_model(); tests inject a fake with
set_model_override(). Nothing here is stored in graph state or checkpoints."""
from __future__ import annotations

from typing import Any, Optional

from config.settings import get_settings

_OVERRIDE: Any = None
_OVERRIDE_SET = False
_CACHE: dict[str, Any] = {}


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
    key = f"{s.llm_model}|{s.llm_base_url}|{temperature}|{bool(s.llm_api_key)}"
    if key in _CACHE:
        return _CACHE[key]
    try:
        from langchain.chat_models import init_chat_model
    except ImportError:  # pragma: no cover
        from langchain_core.language_models import BaseChatModel  # noqa: F401
        return None
    kwargs: dict[str, Any] = {"temperature": temperature, "timeout": s.llm_timeout_sec}
    if s.llm_base_url:
        kwargs["base_url"] = s.llm_base_url
    if s.llm_api_key:
        kwargs["api_key"] = s.llm_api_key
    try:
        model = init_chat_model(s.llm_model, **kwargs)
    except Exception:
        return None
    _CACHE[key] = model
    return model


def llm_available() -> bool:
    return get_model() is not None
