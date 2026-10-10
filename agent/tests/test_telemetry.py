import json
from types import SimpleNamespace
import httpx
from runtime import telemetry


def test_unknown_usage_and_context_isolation(monkeypatch):
    messages = []
    monkeypatch.setattr(telemetry.logger, "info", messages.append)
    with telemetry.capture(SimpleNamespace(task_id="a" * 48, execution_version=2)) as trace:
        telemetry.update(parser="llm", model_calls=1, total_tokens=None)
        telemetry.update(question="private user content", sql="private SQL")
        with telemetry.stage("model"):
            telemetry.model_error(httpx.ReadTimeout("private provider payload"))
        telemetry.usage({"prompt_tokens": True, "completion_tokens": -1})
        assert trace["error_code"] == "model_timeout"
        assert trace["total_tokens"] is None and trace["input_tokens"] is None
        assert trace["stages_ms"]["model"] >= 0
    assert telemetry.current.get() is None
    assert "private provider payload" not in messages[0]
    assert "private user content" not in messages[0] and "private SQL" not in messages[0]
    with telemetry.capture(None) as fresh:
        assert fresh["model_calls"] == 0 and fresh["total_tokens"] == 0
        assert fresh["stages_ms"] == {}
    assert json.loads(messages[0])["execution_version"] == 2


def test_provider_error_classification():
    request = httpx.Request("POST", "https://example.test")
    with telemetry.capture(None) as trace:
        response = httpx.Response(401, request=request)
        telemetry.model_error(httpx.HTTPStatusError("secret response", request=request, response=response))
        assert trace["error_code"] == "model_auth"
