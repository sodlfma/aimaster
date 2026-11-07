from __future__ import annotations

from graph.build import build_graph, memory_checkpointer
from graph.nodes import llm_nodes
from llm.invoke import explain_llm_error, is_retryable_llm_error
from llm.models import clear_model_override, normalize_openai_base_url, set_model_override


def test_normalize_openai_base_url_strips_chat_completions():
    assert normalize_openai_base_url("http://10.0.0.5:8000/v1/chat/completions") == "http://10.0.0.5:8000/v1"
    assert normalize_openai_base_url("http://10.0.0.5:8000/chat/completions/") == "http://10.0.0.5:8000/v1"
    assert normalize_openai_base_url("http://127.0.0.1:11434") == "http://127.0.0.1:11434/v1"
    assert normalize_openai_base_url("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434/v1"
    assert normalize_openai_base_url("https://openwebui.example/api") == "https://openwebui.example/api"


def test_405_is_not_retryable_and_explains_base_url():
    err = RuntimeError("Error code: 405 - {'error': {'message': 'Method Not Allowed'}}")
    assert is_retryable_llm_error(err) is False
    msg = explain_llm_error(err, "http://10.0.0.5:8000/v1/chat/completions")
    assert "405" in msg and "/v1" in msg and "chat/completions" in msg
    assert is_retryable_llm_error(ConnectionError("provider down")) is True


def test_interpret_does_not_retry_http_405(raw_inputs, isolated_settings, monkeypatch):
    class Once405:
        model_name = "proxy"
        calls = 0

        def with_structured_output(self, schema, **kwargs):
            return self

        def invoke(self, messages):
            Once405.calls += 1
            raise RuntimeError("Error code: 405 - Method Not Allowed")

    monkeypatch.setattr(llm_nodes, "LLM_BACKOFF_SEC", 0.0)
    isolated_settings.llm_enabled = True
    isolated_settings.llm_model = "openai:corp-model"
    isolated_settings.llm_base_url = "http://10.0.0.5:8000/v1/chat/completions"
    set_model_override(Once405())
    try:
        graph = build_graph(memory_checkpointer())
        cfg = {"configurable": {"thread_id": "t-405"}}
        snap = None
        for _mode, _chunk in graph.stream(
            {"raw_inputs": raw_inputs, "llm_enabled": True, "options": {"dry_run": True, "interpret": True}},
            cfg,
            stream_mode=["updates", "custom"],
        ):
            snap = graph.get_state(cfg)
        assert snap is not None
        v = snap.values
        assert v["diagnosis_report"].source == "rules"
        assert "llm_interpret_diagnosis" in v["llm_skipped"]
        assert Once405.calls == 1
        warn = " ".join(v["warnings"])
        assert "405" in warn and "1회" in warn
    finally:
        clear_model_override()
