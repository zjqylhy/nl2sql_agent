import json
import httpx
import pytest
from fastapi.testclient import TestClient
from runtime import app as runtime


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SERVICE_KEY", "s" * 40)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("TENANT_DATASOURCES", json.dumps({"alpha:ops": {"host": "mysql", "database": "olist_alpha", "user": "alpha_ro", "password": "test-only", "analysis_date": "2018-08-31"}}))
    runtime.sources.cache_clear()
    with TestClient(runtime.app) as client:
        yield client
    runtime.sources.cache_clear()


def request(tenant="alpha", metrics=None, question="本月 GMV"):
    return {"tenant_id": tenant, "role": "ops", "allowed_metrics": metrics if metrics is not None else ["gmv"], "input": {"question": question}}


def test_internal_endpoint_requires_auth(client):
    assert client.post("/internal/query", json=request()).status_code == 401


def test_tenant_and_metric_denials_do_not_touch_database(client, monkeypatch):
    def unexpected(*args):
        pytest.fail("Unauthorized request reached database")
    monkeypatch.setattr(runtime, "execute", unexpected)
    assert client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=request("beta")).status_code == 403
    assert client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=request(metrics=["orders"])).status_code == 403


def test_query_uses_trusted_datasource_and_bound_values(client, monkeypatch):
    calls = []
    def execute(source, sql, params):
        calls.append((source.database, sql, params))
        return [{"label": "整体", "value": "12.30", "sample_size": 1, "total_orders": 1, "excluded_orders": 0, "unavailable_orders": 0}]
    monkeypatch.setattr(runtime, "execute", execute)
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=request())
    assert response.status_code == 200
    result = response.json()
    assert result["analysis_date"] == "2018-08-31"
    assert result["usage"]["parser"] == "rules"
    assert calls[0][0] == "olist_alpha"
    assert calls[0][2] == ["2018-08-01", "2018-09-01"]
    assert "test-only" not in response.text


def test_clarification_does_not_execute(client, monkeypatch):
    monkeypatch.setattr(runtime, "execute", lambda *args: pytest.fail("ambiguous query executed"))
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=request(question="GMV"))
    assert response.json()["status"] == "needs_clarification"


def mock_model(monkeypatch, content, status=200):
    monkeypatch.setenv("LLM_API_KEY", "test-key-no-production")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.example")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    requests = []
    def handle(req):
        requests.append(json.loads(req.content))
        return httpx.Response(status, json={"choices": [{"message": {"content": json.dumps(content)}}], "usage": {"total_tokens": 123}})
    client_class = httpx.Client
    monkeypatch.setattr(runtime.httpx, "Client", lambda **options: client_class(transport=httpx.MockTransport(handle), **options))
    return requests


def test_model_intent_contract(client, monkeypatch):
    calls = mock_model(monkeypatch, {"metric": "gmv", "period": "this_month", "group_by": "state", "states": ["SP"]})
    executions = []
    def execute(source, sql, params):
        executions.append(params)
        return [{"label": "SP", "value": "123.45", "sample_size": 1}]
    monkeypatch.setattr(runtime, "execute", execute)
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=request(question="本月圣保罗州销售额"))
    assert response.status_code == 200 and response.json()["usage"]["parser"] == "llm"
    assert executions == [["2018-08-01", "2018-09-01", "SP"]]
    assert calls[0]["response_format"] == {"type": "json_object"} and calls[0]["max_tokens"] == 800
    assert "test-key-no-production" not in response.text


@pytest.mark.parametrize("content,status", [
    ({"metric": "gmv", "period": "this_month", "sql": "DROP TABLE orders"}, 200),
    ({"metric": "gmv", "period": "this_month"}, 500),
])
def test_model_failure_never_executes_or_falls_back(client, monkeypatch, content, status):
    mock_model(monkeypatch, content, status)
    monkeypatch.setattr(runtime, "execute", lambda *args: pytest.fail("Invalid provider response executed"))
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=request())
    assert response.status_code == 503
    assert "DROP TABLE" not in response.text


def test_manual_input_uses_rules_even_when_model_enabled(client, monkeypatch):
    calls = mock_model(monkeypatch, {})
    monkeypatch.setattr(runtime, "execute", lambda *args: [])
    body = request()
    body["input"]["metric"] = "gmv"
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=body)
    assert response.status_code == 200 and response.json()["usage"]["parser"] == "rules"
    assert calls == []
