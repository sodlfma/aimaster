"""Structured-output bind: fake models keep working; schema 400s retry function_calling."""
from __future__ import annotations

from llm.invoke import _invoke_structured, _is_schema_error
from llm.schemas import DiagnosisReport


def test_schema_error_detector():
    assert _is_schema_error(RuntimeError("Invalid schema for response_format 'ScenarioSpec'"))
    assert not _is_schema_error(RuntimeError("401 unauthorized"))


class _SchemaThenOk:
    model_name = "fake-retry"

    def __init__(self):
        self.calls = []

    def with_structured_output(self, schema, **kwargs):
        parent = self

        class _R:
            def invoke(_self, messages):
                parent.calls.append(dict(kwargs))
                if kwargs.get("method") != "function_calling":
                    raise RuntimeError("Error code: 400 - Invalid schema for response_format 'ScenarioSpec'")
                return DiagnosisReport(summary="via function_calling", bottleneck_order=["db"], source="llm")

        return _R()


def test_invalid_json_schema_retries_function_calling():
    model = _SchemaThenOk()
    out, include_raw = _invoke_structured(model, DiagnosisReport, messages=[])
    assert out.summary == "via function_calling"
    assert include_raw is True
    assert any(c.get("method") == "function_calling" for c in model.calls)
    assert model.calls[0].get("method") is None
