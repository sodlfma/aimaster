"""OpenAI-compatible base URLs must land on POST /v1/chat/completions.

A host root, or a base that already includes /chat/completions or /responses,
makes the OpenAI SDK call a path the gateway does not allow POST on (HTTP 405).

Talent Lab and other Azure OpenAI gateways are the exception: they use
`/openai/deployments/{model}/chat/completions?api-version=...`.
"""
import pytest

from llm.models import (
    build_azure_chat_kwargs,
    build_chat_model_kwargs,
    normalize_openai_base_url,
    uses_azure_chat,
)


def test_host_root_gains_v1_prefix():
    assert normalize_openai_base_url("https://llm.internal.example") == "https://llm.internal.example/v1"
    assert normalize_openai_base_url("https://llm.internal.example/") == "https://llm.internal.example/v1"
    assert normalize_openai_base_url("http://10.1.2.3:8000") == "http://10.1.2.3:8000/v1"


def test_existing_v1_and_query_are_kept():
    assert normalize_openai_base_url("https://llm.internal.example/v1") == "https://llm.internal.example/v1"
    assert normalize_openai_base_url("https://llm.internal.example/v1/") == "https://llm.internal.example/v1"
    assert (
        normalize_openai_base_url("https://llm.internal.example/api/openai/v1")
        == "https://llm.internal.example/api/openai/v1"
    )
    assert (
        normalize_openai_base_url("https://llm.internal.example/v1?api-version=2024-02-01")
        == "https://llm.internal.example/v1?api-version=2024-02-01"
    )


def test_sdk_suffixes_are_stripped_before_v1_check():
    assert (
        normalize_openai_base_url("https://llm.internal.example/v1/chat/completions")
        == "https://llm.internal.example/v1"
    )
    assert (
        normalize_openai_base_url("https://llm.internal.example/v1/chat/completions/chat/completions")
        == "https://llm.internal.example/v1"
    )
    assert normalize_openai_base_url("https://llm.internal.example/v1/responses") == "https://llm.internal.example/v1"
    assert normalize_openai_base_url("https://LLM.Example/V1/CHAT/COMPLETIONS") == "https://LLM.Example/V1"
    # Pasting the full path with no /v1 still has to become a chat-completions prefix.
    assert normalize_openai_base_url("https://llm.internal.example/chat/completions") == "https://llm.internal.example/v1"


def test_custom_openai_base_uses_chat_completions_not_responses(monkeypatch):
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    kwargs = build_chat_model_kwargs(
        model="openai:gpt-5.6-sol",
        base_url="https://llm.internal.example",
        api_key="secret",
        temperature=0.0,
        timeout=60,
    )
    assert kwargs["base_url"] == "https://llm.internal.example/v1"
    assert kwargs["use_responses_api"] is False
    assert kwargs["api_key"] == "secret"
    assert kwargs["temperature"] == 0.0
    assert kwargs["timeout"] == 60


def test_openai_env_base_url_is_normalized_when_setting_is_empty(monkeypatch):
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.internal.example/v1/responses/")
    kwargs = build_chat_model_kwargs(
        model="gpt-4o",
        base_url=None,
        api_key=None,
        temperature=0.0,
        timeout=30,
    )
    assert kwargs["base_url"] == "https://llm.internal.example/v1"
    assert kwargs["use_responses_api"] is False
    assert "api_key" not in kwargs


def test_explicit_base_url_wins_over_env(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env.example/v1")
    kwargs = build_chat_model_kwargs(
        model="openai:company-model",
        base_url="https://sidebar.example/v1/chat/completions",
        api_key=None,
        temperature=0.2,
        timeout=10,
    )
    assert kwargs["base_url"] == "https://sidebar.example/v1"


def test_official_openai_keeps_responses_routing(monkeypatch):
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    kwargs = build_chat_model_kwargs(
        model="openai:gpt-4o",
        base_url=None,
        api_key="k",
        temperature=0.0,
        timeout=60,
    )
    assert "base_url" not in kwargs
    assert "use_responses_api" not in kwargs


def test_non_openai_provider_is_not_rewritten():
    kwargs = build_chat_model_kwargs(
        model="anthropic:claude-3-5-sonnet-latest",
        base_url="https://api.anthropic.com",
        api_key="k",
        temperature=0.0,
        timeout=60,
    )
    assert kwargs["base_url"] == "https://api.anthropic.com"
    assert "use_responses_api" not in kwargs

    azure = build_chat_model_kwargs(
        model="azure_openai:gpt-4o",
        base_url="https://example.openai.azure.com",
        api_key="k",
        temperature=0.0,
        timeout=60,
    )
    assert azure["base_url"] == "https://example.openai.azure.com"
    assert "use_responses_api" not in azure


def test_get_model_passes_normalized_kwargs(isolated_settings, monkeypatch):
    from llm import models

    models._CACHE.clear()
    models.clear_model_override()
    isolated_settings.llm_enabled = True
    isolated_settings.llm_model = "openai:gpt-4o"
    isolated_settings.llm_base_url = "https://llm.internal.example/v1/chat/completions"
    isolated_settings.llm_api_key = "test-key"
    seen: dict = {}

    def fake_init(model, **kwargs):
        seen["model"] = model
        seen["kwargs"] = kwargs
        return "fake-model"

    monkeypatch.setattr("langchain.chat_models.init_chat_model", fake_init)
    try:
        assert models.get_model() == "fake-model"
        assert seen["model"] == "openai:gpt-4o"
        assert seen["kwargs"]["base_url"] == "https://llm.internal.example/v1"
        assert seen["kwargs"]["use_responses_api"] is False
        assert seen["kwargs"]["api_key"] == "test-key"
        # Second call uses the cache and must not rebuild with the raw URL.
        assert models.get_model() == "fake-model"
    finally:
        models._CACHE.clear()
        models.clear_model_override()


def test_talentlab_host_is_azure_not_v1_chat():
    assert uses_azure_chat("gpt-4.1", "https://skax.ai-talentlab.com", None)
    assert uses_azure_chat("openai:gpt-4o", "https://skax.ai-talentlab.com/", None)
    assert not uses_azure_chat("gpt-4o", "https://llm.internal.example", None)
    assert uses_azure_chat("gpt-4o", "https://llm.internal.example", "2024-12-01-preview")
    assert not uses_azure_chat("anthropic:claude-3-5-sonnet-latest", "https://skax.ai-talentlab.com", "2024-12-01-preview")


def test_azure_kwargs_match_azure_openai_client():
    kwargs = build_azure_chat_kwargs(
        model="openai:gpt-4.1",
        base_url="https://skax.ai-talentlab.com/openai/deployments/gpt-4.1/chat/completions?api-version=2024-10-21",
        api_key="atl-test",
        api_version=None,
        temperature=0.0,
        timeout=60,
    )
    assert kwargs == {
        "model": "gpt-4.1",
        "azure_deployment": "gpt-4.1",
        "azure_endpoint": "https://skax.ai-talentlab.com",
        "api_version": "2024-10-21",
        "timeout": 60,
        "use_responses_api": False,
        "temperature": 0.0,
        "api_key": "atl-test",
    }


def test_azure_default_api_version_and_reasoning_models_omit_temperature():
    kwargs = build_azure_chat_kwargs(
        model="azure_openai:gpt-5-mini",
        base_url="https://example.openai.azure.com/",
        api_key=None,
        api_version="",
        temperature=0.0,
        timeout=30,
    )
    assert kwargs["azure_endpoint"] == "https://example.openai.azure.com"
    assert kwargs["azure_deployment"] == "gpt-5-mini"
    assert kwargs["api_version"] == "2024-12-01-preview"
    assert "temperature" not in kwargs
    assert "base_url" not in kwargs
    assert "api_key" not in kwargs


def test_get_model_builds_azure_client_for_talentlab(isolated_settings, monkeypatch):
    from llm import models

    models._CACHE.clear()
    models.clear_model_override()
    isolated_settings.llm_enabled = True
    isolated_settings.llm_model = "gpt-4.1"
    isolated_settings.llm_base_url = "https://skax.ai-talentlab.com"
    isolated_settings.llm_api_key = "atl-test"
    isolated_settings.llm_api_version = None
    seen: dict = {}

    class FakeAzure:
        def __init__(self, **kwargs):
            seen["kwargs"] = kwargs

    def forbid_openai(*_args, **_kwargs):
        raise AssertionError("ChatOpenAI path must not be used for Talent Lab")

    monkeypatch.setattr(models, "_azure_chat_cls", lambda: FakeAzure)
    monkeypatch.setattr("langchain.chat_models.init_chat_model", forbid_openai)
    try:
        model = models.get_model()
        assert isinstance(model, FakeAzure)
        assert seen["kwargs"]["azure_endpoint"] == "https://skax.ai-talentlab.com"
        assert seen["kwargs"]["azure_deployment"] == "gpt-4.1"
        assert seen["kwargs"]["model"] == "gpt-4.1"
        assert seen["kwargs"]["api_version"] == "2024-12-01-preview"
        assert seen["kwargs"]["api_key"] == "atl-test"
        assert seen["kwargs"]["use_responses_api"] is False
        assert "base_url" not in seen["kwargs"]
        assert models.llm_route_label().startswith("Azure OpenAI · gpt-4.1")
        assert models.get_model() is model
        assert models.llm_init_error() == ""
    finally:
        models._CACHE.clear()
        models.clear_model_override()


def test_real_azure_client_uses_deployment_completions_url():
    langchain_openai = pytest.importorskip("langchain_openai")
    kwargs = build_azure_chat_kwargs(
        model="gpt-4.1",
        base_url="https://skax.ai-talentlab.com",
        api_key="atl-test",
        api_version="2024-12-01-preview",
        temperature=0.0,
        timeout=60,
    )
    model = langchain_openai.AzureChatOpenAI(**kwargs)
    client = model.root_client
    assert str(client.base_url).rstrip("/") == "https://skax.ai-talentlab.com/openai/deployments/gpt-4.1"
    assert client.default_query["api-version"] == "2024-12-01-preview"
    assert client.api_key == "atl-test"


def test_azure_init_error_is_reported(isolated_settings, monkeypatch):
    from llm import models

    models._CACHE.clear()
    models.clear_model_override()
    isolated_settings.llm_enabled = True
    isolated_settings.llm_model = "azure_openai:gpt-4o"
    isolated_settings.llm_base_url = None
    isolated_settings.llm_api_version = None
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    try:
        assert models.get_model() is None
        assert "Base URL" in models.llm_init_error()
    finally:
        models._CACHE.clear()
        models.clear_model_override()
