"""预算与重试观测在提供商请求前生效，响应原样传递。"""
import importlib
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def observer(monkeypatch):
    from runtime import app
    original_client, original_execute = httpx.Client, app.execute
    module = importlib.import_module("scripts.understanding_runtime")
    module.issued = 0
    module.exhausted = False
    module.observations.clear()
    monkeypatch.setenv("SERVICE_KEY", "test-only-service-key")
    monkeypatch.setenv("EVAL_MAX_MODEL_CALLS", "1")
    yield module
    httpx.Client, app.execute = original_client, original_execute


def test_budget_checks_before_outbound_request(observer, monkeypatch):
    calls = []
    response = httpx.Response(200, json={"original": True})
    monkeypatch.setattr(observer.base_client, "post", lambda *args, **kwargs: calls.append(1) or response)
    with observer.ObservedClient() as client:
        assert client.post("https://test.invalid") is response
        with pytest.raises(httpx.RequestError):
            client.post("https://test.invalid")
    assert len(calls) == 1 and observer.issued == 1 and observer.exhausted


def test_retry_tokens_preserve_unknown_usage(observer, monkeypatch):
    telemetry = {"parser": "llm", "error_code": "model_timeout", "input_tokens": None, "output_tokens": None, "total_tokens": None, "stages_ms": {"model": 2}}

    def response(_):
        observer.active.get().update(model_calls=1)
        return {"telemetry": telemetry}

    monkeypatch.setattr(observer.runtime, "query", response)
    body = {"tenant_id": "test", "role": "ops", "allowed_metrics": ["gmv"], "input": {"question": "本月 GMV"}, "execution": {"task_id": "a" * 48, "execution_version": 1}}
    observer.query(body, "test-only-service-key")
    telemetry.update(error_code="", input_tokens=3, output_tokens=4, total_tokens=7)
    body["execution"]["execution_version"] = 2
    observer.query(body, "test-only-service-key")
    value = observer.observations["a" * 48]
    assert value["attempts"] == 2 and value["model_calls"] == 2
    assert value["known_total_tokens"] == 7 and value["unknown_usage_calls"] == 1
    assert value["known_input_tokens"] == 3 and value["known_output_tokens"] == 4
    assert value["stages_ms"]["model"] == 4
    assert telemetry["stages_ms"]["model"] == 2
