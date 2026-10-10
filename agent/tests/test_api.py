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


def test_error_telemetry_keeps_execution_and_unknown_usage(client, monkeypatch):
    import pymysql
    body = request()
    body["execution"] = {"task_id": "a" * 48, "execution_version": 3}
    def fail_sql(*args):
        raise pymysql.OperationalError(3024, "sensitive database content")
    monkeypatch.setattr(runtime, "execute", fail_sql)
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=body)
    trace = response.json()["telemetry"]
    assert response.status_code == 503 and trace["error_code"] == "sql_timeout"
    assert trace["execution_version"] == 3 and trace["model_calls"] == 0
    assert "sensitive database content" not in response.text
    monkeypatch.setenv("LLM_API_KEY", "test-secret")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.example")
    monkeypatch.setenv("LLM_MODEL", "model")
    original = httpx.Client
    def timeout(req): raise httpx.ReadTimeout("sensitive provider content", request=req)
    monkeypatch.setattr(runtime.httpx, "Client", lambda **opts: original(transport=httpx.MockTransport(timeout), **opts))
    body["input"]["question"] = "本月 GMV，只看圣保罗州"
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=body)
    trace = response.json()["telemetry"]
    assert trace["error_code"] == "model_timeout" and trace["model_calls"] == 1
    assert trace["total_tokens"] is None
    assert "sensitive provider content" not in response.text


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
        return [{"d_state": "SP", "m0_value": "123.45", "m0_sample": 1, "total_orders": 1}]
    monkeypatch.setattr(runtime, "execute", execute)
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=request(question="本月圣保罗州销售额，按州统计"))
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
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=request(question="本月 GMV，只看圣保罗州"))
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


@pytest.mark.parametrize("question", ["本月 GMV", "本月 GMV 按州统计", "上周有效订单量是多少？", "本月 GMV，只看 SP、RJ 州"])
def test_rules_hit_skips_configured_model(client, monkeypatch, question):
    calls = mock_model(monkeypatch, {})
    monkeypatch.setattr(runtime, "execute", lambda *args: [])
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=request(metrics=["gmv", "orders"], question=question))
    assert response.status_code == 200
    assert response.json()["usage"] == {"parser": "rules", "tokens": 0}
    assert calls == []


def test_named_state_filter_falls_back_with_manual_constraint(client, monkeypatch):
    calls = mock_model(monkeypatch, {"metric": "gmv", "period": "this_month", "states": ["SP"]})
    executions = []
    monkeypatch.setattr(runtime, "execute", lambda source, sql, params: executions.append(params) or [])
    body = request(question="本月 GMV，只看圣保罗州")
    body["input"]["metric"] = "gmv"
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=body)
    assert response.json()["usage"]["parser"] == "llm"
    assert len(calls) == 1
    assert executions == [["2018-08-01", "2018-09-01", "SP"]]
    assert "2018-08-31" in calls[0]["messages"][0]["content"]
    assert "requires_filter" not in calls[0]["messages"][0]["content"]
    assert "states_complete" not in calls[0]["messages"][0]["content"]


def test_rule_miss_without_model_needs_clarification(client, monkeypatch):
    monkeypatch.setattr(runtime, "execute", lambda *args: pytest.fail("unresolved filter executed"))
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=request(question="本月 GMV，只看圣保罗州"))
    assert response.json()["status"] == "needs_clarification"


@pytest.mark.parametrize("question", [
    "GMV", "本月", "本月上月 GMV", "本月 GMV 只看", "本月 GMV 只看 ZZ 州",
    "本月 GMV 按渠道", "本月 GMV 按支付方式和州统计", "本月 GMV 按未知分类", "本月 GMV 只看金额超过 100 的订单",
    "2018-02-30 至 2018-03-01 GMV", "2018-08-31 至 2018-08-01 GMV",
])
def test_ambiguous_or_unsupported_queries_skip_model_and_database(client, monkeypatch, question):
    calls = mock_model(monkeypatch, {})
    monkeypatch.setattr(runtime, "execute", lambda *args: pytest.fail("invalid query executed"))
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=request(question=question))
    assert response.json()["status"] == "needs_clarification"
    assert calls == []


@pytest.mark.parametrize("body", [
    request(tenant="beta", question="本月 GMV，只看圣保罗州"),
    request(metrics=["orders"], question="本月 GMV，只看圣保罗州"),
    request(metrics=[], question="本月销售额"),
])
def test_denied_queries_skip_model_before_parsing(client, monkeypatch, body):
    calls = mock_model(monkeypatch, {})
    monkeypatch.setattr(runtime, "execute", lambda *args: pytest.fail("denied query executed"))
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=body)
    assert response.status_code == 403 and calls == []


def test_known_future_dates_skip_model(client, monkeypatch):
    calls = mock_model(monkeypatch, {})
    monkeypatch.setattr(runtime, "execute", lambda *args: pytest.fail("future query executed"))
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=request(question="2019-01-01 至 2019-02-01 GMV，只看圣保罗州"))
    assert response.status_code == 422 and calls == []


@pytest.mark.parametrize("content", [
    {"metric": "gmv", "period": "last_month", "states": ["SP"]},
    {"metric": "orders", "period": "this_month", "states": ["SP"]},
    {"metric": "gmv", "period": "this_month", "states": []},
])
def test_model_cannot_overwrite_known_conditions_or_drop_filter(client, monkeypatch, content):
    calls = mock_model(monkeypatch, content)
    monkeypatch.setattr(runtime, "execute", lambda *args: pytest.fail("conflicting model intent executed"))
    body = request(question="本月 GMV，只看圣保罗州")
    body["input"]["metric"] = "gmv"
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=body)
    assert response.json()["status"] == "needs_clarification" and len(calls) == 1


def test_model_cannot_overwrite_manual_group_or_explicit_dates(client, monkeypatch):
    calls = mock_model(monkeypatch, {"metric": "gmv", "period": "custom", "start": "2018-08-02", "end": "2018-08-31", "states": ["SP"]})
    monkeypatch.setattr(runtime, "execute", lambda *args: pytest.fail("manual constraint overwritten"))
    body = request(question="2018-08-01 至 2018-08-31 GMV，只看圣保罗州")
    body["input"]["group_by"] = "state"
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=body)
    assert response.json()["status"] == "needs_clarification" and len(calls) == 1


@pytest.mark.parametrize("question,content", [
    ("本月圣保罗州 GMV", {"metric": "gmv", "period": "this_month"}),
    ("本月 SP 州销售额", {"metric": "gmv", "period": "this_month", "states": ["SP", "RJ"]}),
    ("本月销售额", {"metric": "gmv", "period": "this_month", "states": ["SP"]}),
    ("本月销售额", {"metric": "gmv", "period": "this_month", "group_by": "state"}),
])
def test_model_preserves_implicit_filter_and_does_not_invent_scope(client, monkeypatch, question, content):
    calls = mock_model(monkeypatch, content)
    monkeypatch.setattr(runtime, "execute", lambda *args: pytest.fail("invented or omitted condition executed"))
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=request(question=question))
    assert response.json()["status"] == "needs_clarification" and len(calls) == 1


def test_manual_question_conflict_skips_model(client, monkeypatch):
    calls = mock_model(monkeypatch, {})
    monkeypatch.setattr(runtime, "execute", lambda *args: pytest.fail("conflicting choices executed"))
    body = request(question="本月 GMV，只看圣保罗州")
    body["input"]["period"] = "last_month"
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=body)
    assert response.json()["status"] == "needs_clarification" and calls == []


def test_model_requested_clarification_does_not_execute(client, monkeypatch):
    calls = mock_model(monkeypatch, {"clarification": "请明确要查询的巴西州"})
    monkeypatch.setattr(runtime, "execute", lambda *args: pytest.fail("clarification executed"))
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=request(question="本月 GMV，只看圣保罗州"))
    assert response.json()["status"] == "needs_clarification" and len(calls) == 1
    assert response.json()["usage"] == {"parser": "llm", "model": "test-model", "tokens": 123}


def test_model_cannot_expand_recognized_filter_when_only_metric_is_unknown(client, monkeypatch):
    calls = mock_model(monkeypatch, {"metric": "gmv", "period": "this_month", "states": ["SP", "RJ"]})
    monkeypatch.setattr(runtime, "execute", lambda *args: pytest.fail("expanded filter executed"))
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=request(question="本月销售额，只看 SP 州"))
    assert response.json()["status"] == "needs_clarification" and len(calls) == 1


def test_partial_state_recognition_can_add_named_state_from_question(client, monkeypatch):
    calls = mock_model(monkeypatch, {"metric": "gmv", "period": "this_month", "states": ["SP", "RJ"]})
    executions = []
    monkeypatch.setattr(runtime, "execute", lambda source, sql, params: executions.append(params) or [])
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=request(question="本月 GMV，只看 SP 州和里约热内卢州"))
    assert response.json()["usage"]["parser"] == "llm" and len(calls) == 1
    assert executions == [["2018-08-01", "2018-09-01", "SP", "RJ"]]


@pytest.mark.parametrize("field,value", [("group_by", "state"), ("start", "2018-08-01"), ("end", "2018-08-31")])
def test_model_must_preserve_each_manual_or_explicit_constraint(client, monkeypatch, field, value):
    content = {"metric": "gmv", "period": "custom", "start": "2018-08-01", "end": "2018-08-31", "group_by": "state", "states": ["SP"]}
    content[field] = "" if field == "group_by" else "2018-08-02"
    calls = mock_model(monkeypatch, content)
    monkeypatch.setattr(runtime, "execute", lambda *args: pytest.fail("constraint overwritten"))
    body = request(question="2018-08-01 至 2018-08-31 GMV，只看圣保罗州")
    body["input"]["group_by"] = "state"
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=body)
    assert response.json()["status"] == "needs_clarification" and len(calls) == 1


def test_model_timeout_has_no_internal_retry_or_execution(client, monkeypatch):
    client_class = httpx.Client
    calls = mock_model(monkeypatch, {})
    def timeout(req):
        calls.append({"timeout": True})
        raise httpx.ReadTimeout("test timeout", request=req)
    monkeypatch.setattr(runtime.httpx, "Client", lambda **options: client_class(transport=httpx.MockTransport(timeout), **options))
    monkeypatch.setattr(runtime, "execute", lambda *args: pytest.fail("timed out query executed"))
    response = client.post("/internal/query", headers={"X-Service-Key": "s" * 40}, json=request(question="本月 GMV，只看圣保罗州"))
    assert response.status_code == 503 and len(calls) == 1
