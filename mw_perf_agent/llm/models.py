"""Chat model factory. Nodes call get_model(); tests inject a fake with
set_model_override(). Nothing here is stored in graph state or checkpoints."""
from __future__ import annotations

import os
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit, urlunsplit

from config.settings import get_settings

_OVERRIDE: Any = None
_OVERRIDE_SET = False
_CACHE: dict[str, Any] = {}
_LAST_ERROR = ""

# Azure OpenAI deployment API, not the OpenAI /v1/chat/completions shape.
# Talent Lab documents this host with api-version 2024-12-01-preview.
DEFAULT_AZURE_API_VERSION = "2024-12-01-preview"
_AZURE_GATEWAY_HOSTS = {"skax.ai-talentlab.com"}
_AZURE_PROVIDERS = {"azure_openai", "azure"}

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
    left untouched. Talent Lab and any model with `LLM_API_VERSION` also take
    that Azure path — see `uses_azure_chat`.
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


def _provider_and_name(model: str) -> tuple[str, str]:
    name = (model or "").strip()
    if ":" not in name:
        return "", name
    provider, rest = name.split(":", 1)
    return provider.replace("-", "_").lower(), rest.strip()


def deployment_name(model: str) -> str:
    """Model id used both as the Azure `{deployment}` path segment and `model`."""
    provider, rest = _provider_and_name(model)
    if provider in _AZURE_PROVIDERS or provider == "openai":
        return rest
    return rest


def _hostname(url: Optional[str]) -> str:
    if not url or not url.strip():
        return ""
    return (urlsplit(url.strip()).hostname or "").lower().rstrip(".")


def uses_azure_chat(model: str, base_url: Optional[str] = None, api_version: Optional[str] = None) -> bool:
    """True when the call must use AzureOpenAI, not ChatOpenAI `/v1`.

    Azure shape: `POST {endpoint}/openai/deployments/{model}/chat/completions?api-version=...`
    with the `api-key` header. OpenAI-compatible gateways stay on `/v1/chat/completions`.
    """
    provider, rest = _provider_and_name(model)
    if provider in _AZURE_PROVIDERS:
        return True
    openai_like = provider == "openai" or (not provider and uses_openai_chat_endpoint(rest))
    if not openai_like:
        return False
    if (api_version or "").strip():
        return True
    return _hostname(base_url) in _AZURE_GATEWAY_HOSTS


def azure_endpoint(url: str) -> str:
    """Origin only. A pasted completions URL must not become the Azure base."""
    raw = (url or "").strip()
    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return raw.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _api_version_from_url(url: Optional[str]) -> str:
    if not url or not url.strip():
        return ""
    query = parse_qs(urlsplit(url.strip()).query)
    for key in ("api-version", "api_version"):
        values = query.get(key) or []
        if values and values[0].strip():
            return values[0].strip()
    return ""


def _include_temperature(deployment: str) -> bool:
    """gpt-5 / gpt-6 reject a temperature field. Leaving it unset omits it."""
    name = (deployment or "").lower()
    return not name.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))


def build_azure_chat_kwargs(
    *,
    model: str,
    base_url: Optional[str],
    api_key: Optional[str],
    api_version: Optional[str],
    temperature: float,
    timeout: int,
) -> dict[str, Any]:
    """Keyword arguments for `AzureChatOpenAI` matching `AzureOpenAI(...)`.

    Equivalent to:
        AzureOpenAI(azure_endpoint=..., api_key=..., api_version=...)
        client.chat.completions.create(model=deployment, messages=...)
    """
    raw = (base_url or os.environ.get("AZURE_OPENAI_ENDPOINT") or "").strip()
    if not raw:
        raise ValueError("Azure OpenAI 호출에는 Base URL이 필요합니다. 예: https://skax.ai-talentlab.com")
    deployment = deployment_name(model)
    if not deployment:
        raise ValueError("모델(deployment) 이름이 비어 있습니다. 예: gpt-4.1")
    version = (api_version or "").strip() or _api_version_from_url(raw) or DEFAULT_AZURE_API_VERSION
    kwargs: dict[str, Any] = {
        "model": deployment,
        "azure_deployment": deployment,
        "azure_endpoint": azure_endpoint(raw),
        "api_version": version,
        "timeout": timeout,
        "use_responses_api": False,
    }
    if _include_temperature(deployment):
        kwargs["temperature"] = temperature
    if api_key:
        kwargs["api_key"] = api_key
    return kwargs


def _azure_chat_cls():
    from langchain_openai import AzureChatOpenAI

    return AzureChatOpenAI


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


def llm_init_error() -> str:
    """Last model-construction error. Empty after a successful init."""
    return _LAST_ERROR


def llm_route_label() -> str:
    """Short sidebar description of which HTTP API the next call will use."""
    s = get_settings()
    if uses_azure_chat(s.llm_model, s.llm_base_url, s.llm_api_version):
        try:
            kw = build_azure_chat_kwargs(
                model=s.llm_model,
                base_url=s.llm_base_url,
                api_key=None,
                api_version=s.llm_api_version,
                temperature=0.0,
                timeout=s.llm_timeout_sec,
            )
        except Exception as exc:
            return str(exc)[:180]
        return f"Azure OpenAI · {kw['azure_deployment']} · api-version {kw['api_version']}"
    if uses_openai_chat_endpoint(s.llm_model) and _openai_base_url(s.llm_base_url):
        return "OpenAI 호환 · /v1/chat/completions"
    return "init_chat_model · " + s.llm_model


def _remember_error(exc: BaseException) -> None:
    global _LAST_ERROR
    _LAST_ERROR = str(exc).strip()[:300]


def get_model(temperature: float = 0.0) -> Optional[Any]:
    global _LAST_ERROR
    if _OVERRIDE_SET:
        return _OVERRIDE
    s = get_settings()
    if not s.llm_enabled or not s.llm_model:
        return None
    azure = uses_azure_chat(s.llm_model, s.llm_base_url, s.llm_api_version)
    try:
        if azure:
            kwargs = build_azure_chat_kwargs(
                model=s.llm_model,
                base_url=s.llm_base_url,
                api_key=s.llm_api_key,
                api_version=s.llm_api_version,
                temperature=temperature,
                timeout=s.llm_timeout_sec,
            )
        else:
            kwargs = build_chat_model_kwargs(
                model=s.llm_model,
                base_url=s.llm_base_url,
                api_key=s.llm_api_key,
                temperature=temperature,
                timeout=s.llm_timeout_sec,
            )
    except Exception as exc:
        _remember_error(exc)
        return None
    if azure:
        key = "|".join([
            "azure",
            s.llm_model,
            str(kwargs.get("azure_endpoint")),
            str(kwargs.get("azure_deployment")),
            str(kwargs.get("api_version")),
            str(temperature),
            str(bool(s.llm_api_key)),
        ])
    else:
        key = f"{s.llm_model}|{kwargs.get('base_url')}|{temperature}|{bool(s.llm_api_key)}|{kwargs.get('use_responses_api')}"
    if key in _CACHE:
        _LAST_ERROR = ""
        return _CACHE[key]
    try:
        if azure:
            model = _azure_chat_cls()(**kwargs)
        else:
            try:
                from langchain.chat_models import init_chat_model
            except ImportError as exc:  # pragma: no cover
                _remember_error(exc)
                return None
            model = init_chat_model(s.llm_model, **kwargs)
    except Exception as exc:
        _remember_error(exc)
        return None
    _LAST_ERROR = ""
    _CACHE[key] = model
    return model


def llm_available() -> bool:
    return get_model() is not None
