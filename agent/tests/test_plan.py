"""独立样本覆盖组合查询、条件完整性、受信任语义目录和模型边界。"""
import json
import sqlite3
from datetime import date
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from runtime import app as runtime
from runtime.catalog import Catalog, catalog
from runtime.intent import Clarification, RuleMiss
from runtime.plan import QueryPlan, compile_plan, normalize_result
from runtime.plan_parser import plan_constraints
from runtime.security import UnsafeSQL, validate_sql

ANCHOR = date(2018, 8, 31)


def rules(question, overrides=None, anchor=ANCHOR):
    return plan_constraints(question, overrides or {}, anchor).rules_plan()


@pytest.fixture
def facts():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.create_function("DATE_FORMAT", 2, lambda value, fmt: value[:7])
    db.execute("CREATE TABLE metric_orders(order_status TEXT,item_amount REAL,delivery_days REAL,review_score REAL,customer_state TEXT,purchased_at TEXT)")
    db.executemany("INSERT INTO metric_orders VALUES(?,?,?,?,?,?)", [
        ("delivered", 150, 5, 4, "SP", "2018-07-02"),
        ("shipped", 50, None, 2, "RJ", "2018-07-03"),
        ("delivered", 300, 2, None, "SP", "2018-08-02"),
        ("delivered", 0, 1, None, "SP", "2018-08-03"),
        ("canceled", 900, None, 1, "SP", "2018-08-04"),
        ("unavailable", None, None, None, "RJ", "2018-08-05"),
        ("delivered", 9999, 100, 1, "SP", "2018-09-01"),
    ])
    yield db
    db.close()


def run(db, plan):
    sql, params = compile_plan(plan)
    validate_sql(sql, params, overflow_probe=plan.top_n is None)
    rows = [dict(row) for row in db.execute(sql, [str(v) if isinstance(v, Decimal) else v for v in params])]
    return normalize_result(plan, rows, ANCHOR, {"parser":"rules","tokens":0}, sql, params)


def test_multimetric_months_recalculate_ratio_and_keep_distinct_samples(facts):
    result = run(facts, rules("最近 2 个月 GMV 和客单价和平均评分按月统计"))
    july, august = result["rows"]
    assert july["dimensions"] == {"month":"2018-07"}
    assert july["values"]["gmv"]["value"] == 200
    assert july["values"]["aov"]["value"] == 100
    assert august["values"]["aov"]["value"] == 150
    assert august["values"]["review_score"]["sample_size"] == 1
    assert august["values"]["aov"]["sample_size"] == 2
    assert result["metrics"][0]["unit"] == "BRL"


def test_filter_amount_then_metric_and_status_zero(facts):
    result = run(facts, rules("本月商品金额大于 100 的有效订单量"))
    assert result["rows"][0]["values"]["orders"]["value"] == 1
    assert result["rows"][0]["total_orders"] == 2
    canceled = run(facts, rules("本月只看已取消订单的有效订单量"))
    assert canceled["rows"][0]["values"]["orders"]["value"] == 0
    assert canceled["rows"][0]["total_orders"] == 1
    empty = run(facts, rules("本月商品金额大于 10000 的订单量"))
    assert empty["rows"][0]["values"]["orders"]["value"] is None


def test_joint_group_and_exclusion_and_top_n(facts):
    joint = run(facts, rules("最近 2 个月 GMV 按月和州统计"))
    assert len(joint["rows"]) == 4
    excluded = run(facts, rules("本月 GMV 不含 SP 州"))
    assert excluded["rows"][0]["total_orders"] == 1
    top = run(facts, rules("最近 2 个月 GMV 按州排名，取前 1 名"))
    assert top["rows"][0]["label"] == "SP" and top["rows"][0]["values"]["gmv"]["value"] == 450
    assert top["truncated"] and "不代表全范围" in top["message"]


def test_recent_partial_month_and_leap_year():
    p = rules("最近 3 个月 GMV 按月统计", anchor=date(2024,3,15))
    assert str(p.time_range.start) == "2024-01-01" and str(p.time_range.end_exclusive) == "2024-03-16"
    assert rules("本月 GMV 按日统计").dimensions == ["day"]
    assert rules("本月 GMV 和订单量按州，按 GMV 降序取前5名").order_by[0].field == "gmv"


@pytest.mark.parametrize("question", [
    "本月 GMV 和订单量和客单价和平均评分", "本月 GMV 按日和按月统计", "本月 GMV 按月和整体统计",
    "最近 13 个月 GMV", "最近 3 个月本月 GMV", "本月 GMV 按州前5名", "本月 GMV 按州排名取前101名",
    "本月 GMV 和订单量按州排名", "本月商品金额大于 -1 的订单量", "本月商品金额大于 1.001 的订单量",
    "本月付款金额大于 100 的订单量", "本月 GMV 和订单量按品类统计", "本月 GMV 按月同比增长",
    "本月 GMV 只看 SP 不含 RJ 州", "本月订单状态为 invalid 的订单量", "本月订单量按城市排名",
    "本月 GMV 按月 只看", "本月商品金额大于100 或小于10 的订单量",
    "本月已交付和已发货订单的订单量",
])
def test_unsupported_or_ambiguous_requests_never_produce_partial_plan(question):
    with pytest.raises((Clarification, ValidationError)):
        rules(question)


@pytest.mark.parametrize("change", [
    {"metrics":["gmv","gmv"]}, {"metrics":["profit"]}, {"dimensions":["city"]}, {"dimensions":["day","month"]},
    {"order_by":[{"field":"profit","direction":"desc"}]}, {"top_n":0}, {"top_n":True}, {"sql":"SELECT 1"},
    {"filters":[{"field":"amount","op":"in","values":["1"]}]}, {"filters":[{"field":"state","op":"in","values":["SP' OR 1=1"]}]},
])
def test_plan_rejects_injection_and_invalid_combinations(change):
    payload=rules("本月 GMV 按月统计").model_dump(mode="json")
    payload.update(change)
    with pytest.raises(ValidationError): QueryPlan.model_validate(payload)


def test_catalog_rejects_expression_and_field_injection():
    data=catalog().model_dump()
    data["metrics"]["gmv"]["formula"]="SLEEP(10)"
    with pytest.raises((ValidationError, UnsafeSQL)): Catalog.model_validate(data)
    data=catalog().model_dump()
    data["filters"]["amount"]["column"]="password"
    with pytest.raises(ValidationError): Catalog.model_validate(data)


def test_overflow_probe_is_internal_and_sort_alias_not_allowed_in_filter():
    sql, params=compile_plan(rules("本月 GMV 按月统计"))
    with pytest.raises(UnsafeSQL): validate_sql(sql,params)
    validate_sql(sql,params,overflow_probe=True)
    with pytest.raises(UnsafeSQL): validate_sql("SELECT item_amount AS secret FROM metric_orders WHERE secret > ? LIMIT 10",[1])


def test_common_metric_names_skip_model_and_dimension_sort(facts):
    plan = rules("本月取消率和平均交付时长和平均评分按州统计")
    assert plan.metrics == ["cancellation_rate", "delivery_days", "review_score"]
    descending = run(facts, rules("最近 2 个月 GMV 按月统计，按月份降序"))
    assert [r["dimensions"]["month"] for r in descending["rows"]] == ["2018-08", "2018-07"]


@pytest.mark.parametrize("size", [1000, 1001])
def test_execution_refuses_group_overflow_and_closes_connection(monkeypatch, size):
    class Connection:
        closed = False
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params): pass
        def fetchmany(self, count):
            assert count == 1001
            return [{"d_day": "2018-08-01"}] * size
        def close(self): self.closed = True
    connection = Connection()
    monkeypatch.setattr(runtime, "connect", lambda source: connection)
    sql, params = compile_plan(rules("本月 GMV 按日统计"))
    source = runtime.Datasource(host="unused", database="fixture", user="fixture", password="", analysis_date=ANCHOR)
    if size == 1001:
        with pytest.raises(runtime.ResultTooLarge): runtime.execute(source, sql, params)
    else:
        assert len(runtime.execute(source, sql, params)) == 1000
    assert connection.closed


def model(monkeypatch, payload, finish_reason="stop"):
    monkeypatch.setenv("LLM_API_KEY","test-model-key")
    monkeypatch.setenv("LLM_BASE_URL","https://provider.example")
    monkeypatch.setenv("LLM_MODEL","test-model")
    calls=[]
    client_class=httpx.Client
    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200,json={"choices":[{"finish_reason":finish_reason,"message":{"content":json.dumps(payload)}}],"usage":{"total_tokens":99}})
    monkeypatch.setattr(runtime.httpx,"Client",lambda **options:client_class(transport=httpx.MockTransport(handle),**options))
    return calls


def request(question, metrics=None):
    return runtime.QueryRequest(tenant_id="alpha",role="ops",allowed_metrics=metrics or list(catalog().metrics),input={"question":question})


def test_rules_multi_skip_enabled_model_and_deny_entire_query(monkeypatch):
    calls=model(monkeypatch,{})
    plan,usage=runtime.parse_query_plan(request("本月 GMV 和订单量按州统计"),ANCHOR)
    assert plan.metrics==["gmv","orders"] and usage=={"parser":"rules","tokens":0} and calls==[]
    with pytest.raises(HTTPException) as error:
        runtime.parse_query_plan(request("本月 GMV 和平均评分按州统计",["gmv"]),ANCHOR)
    assert error.value.status_code==403 and calls==[]


@pytest.mark.parametrize("state_op", ["in", "eq"])
def test_new_model_plan_preserves_multimetric_and_named_filter(monkeypatch, state_op):
    payload=rules("本月 GMV 和订单量只看 SP 州").model_dump(mode="json")
    payload["filters"][0]["op"] = state_op
    calls=model(monkeypatch,payload)
    plan,usage=runtime.parse_query_plan(request("本月 GMV 和订单量，只看圣保罗州"),ANCHOR)
    assert plan.filters[0].values==["SP"] and usage["tokens"]==99 and len(calls)==1
    assert set(calls[0]) == {"model", "messages", "max_tokens"} and calls[0]["max_tokens"]==512


@pytest.mark.parametrize("change", [
    {"metrics":["gmv"]}, {"dimensions":["state"]}, {"filters":[]},
    {"filters":[{"field":"state","op":"in","values":["SP"]},{"field":"amount","op":"gt","values":["999"]}]},
    {"top_n":1,"dimensions":["state"]}, {"sql":"DROP TABLE orders"},
])
def test_model_cannot_drop_or_invent_known_conditions(monkeypatch, change):
    payload=rules("本月 GMV 和订单量只看 SP 州").model_dump(mode="json")
    payload.update(change)
    model(monkeypatch,payload)
    with pytest.raises((Clarification,HTTPException)):
        runtime.parse_query_plan(request("本月 GMV 和订单量，只看圣保罗州"),ANCHOR)


def test_truncated_model_plan_is_rejected_even_if_json_valid(monkeypatch):
    payload=rules("本月 GMV 和订单量只看 SP 州").model_dump(mode="json")
    calls=model(monkeypatch,payload,finish_reason="length")
    with pytest.raises(HTTPException) as error:
        runtime.parse_query_plan(request("本月 GMV 和订单量，只看圣保罗州"),ANCHOR)
    assert error.value.status_code==503 and len(calls)==1
