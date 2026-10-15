"""继承、澄清、权限和未修改字段用独立期望核对。"""
from datetime import date
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import app as runtime
from runtime.multiturn import QueryContext, Pending, Delta, apply_operations, plan_hash, resolve_followup, source_metadata
from runtime.plan import QueryPlan

ANCHOR = date(2018, 8, 31)


def base(**changes):
    data = {"metrics":["gmv"], "dimensions":["month"], "time_range":{"start":"2018-03-01","end_exclusive":"2018-09-01"}}
    return QueryPlan.model_validate({**data, **changes})


def turn(question, plan=None, pending=None, metrics=None, **overrides):
    context = QueryContext(binding="a" * 64, version=1, plan=plan, pending=pending)
    request = runtime.QueryRequest(tenant_id="alpha", role="ops", allowed_metrics=metrics or ["gmv","orders","review_score"],
                                   input=runtime.QueryInput(question=question, **overrides), context=context)
    return resolve_followup(request, ANCHOR, runtime.parse_query_plan)


def test_three_steps_clarification_preserves_previous_top_n_and_dates(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    first, usage, _ = turn("最近半年 GMV 按月统计")
    assert first.time_range.start == date(2018,3,1) and usage["tokens"] == 0
    second, _, changes = turn("改成按州", first)
    assert second.dimensions == ["state"] and second.metrics == ["gmv"] and second.time_range == first.time_range
    third, _, info = turn("只看两个州，取前三名", second)
    assert third is None and set(info["pending"]["missing"]) == {"states","order_by"}
    pending = Pending.model_validate(info["pending"])
    fourth, _, _ = turn("SP 和 RJ，按 GMV 从高到低", second, pending)
    assert fourth.top_n == 3 and fourth.filters[0].values == ["SP","RJ"]
    assert fourth.order_by[0].field == "gmv" and fourth.order_by[0].direction == "desc"
    assert fourth.time_range == first.time_range


def test_incremental_clarification_cannot_drop_prior_conditions(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    plan = base(dimensions=["state"])
    _, _, info = turn("只看两个州，取前三名", plan)
    _, _, second = turn("SP、RJ", plan, Pending.model_validate(info["pending"]))
    assert second["pending"]["missing"] == ["order_by"]
    completed, _, _ = turn("从高到低", plan, Pending.model_validate(second["pending"]))
    assert completed.top_n == 3 and completed.filters[0].values == ["SP","RJ"]


def test_initial_missing_time_can_be_filled_without_fake_plan(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    plan, _, info = turn("GMV 按州统计")
    assert plan is None and info["pending"]["initial"]["metrics"] == ["gmv"]
    completed, _, _ = turn("本月", None, Pending.model_validate(info["pending"]))
    assert completed.metrics == ["gmv"] and completed.dimensions == ["state"]
    assert completed.time_range.start == date(2018,8,1)


def test_unresolved_filter_cannot_be_dropped_by_unrelated_followup(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    original=base(dimensions=["state"])
    plan,_,info=turn("只看圣保罗州",original)
    assert plan is None and info["pending"]["missing"]==["expression"]
    plan,_,info=turn("改成整体",original,Pending.model_validate(info["pending"]))
    assert plan is None and "expression" in info["pending"]["missing"]
    # 明确补充原筛选后，草稿里的整体修改一并生效。
    plan,_,_=turn("只看 SP",original,Pending.model_validate(info["pending"]))
    assert plan.dimensions==[] and plan.filters[0].values==["SP"]


def test_unrelated_filters_survive_clear_and_metric_change():
    plan = base(dimensions=["state"], filters=[{"field":"state","op":"in","values":["SP"]},{"field":"amount","op":"gte","values":["100"]}])
    updated, _, _ = turn("取消州筛选", plan)
    assert len(updated.filters) == 1 and updated.filters[0].field == "amount"
    updated, _, _ = turn("把 GMV 改成订单量", updated)
    assert updated.metrics == ["orders"] and updated.filters[0].values == ["100"]
    assert updated.time_range == plan.time_range


def test_invalid_metric_order_requires_clarification_and_dimension_cleans_sort():
    plan = base(order_by=[{"field":"month","direction":"desc"}])
    updated, _, info = turn("改成按州", plan)
    assert updated.order_by == [] and info["notes"]
    plan = base(dimensions=["state"], order_by=[{"field":"gmv","direction":"desc"}], top_n=3)
    updated, _, info = turn("改成订单量", plan)
    assert updated is None and "order_by" in info["pending"]["missing"]
    updated, _, info = turn("改成整体", plan)
    assert updated.dimensions == [] and updated.top_n is None and updated.order_by == []


def test_permission_and_stale_draft_fail_before_model(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "fixture")
    with pytest.raises(runtime.HTTPException) as denied:
        turn("改成按州", base(metrics=["review_score"]), metrics=["gmv"])
    assert denied.value.status_code == 403
    pending = Pending(base_hash="b" * 64, missing=["states"], question="只看两个州")
    with pytest.raises(runtime.HTTPException) as stale:
        turn("SP、RJ", base(), pending)
    assert stale.value.status_code == 409


def test_delta_cannot_replace_field_or_inject_sql():
    from runtime.multiturn import filter_id
    plan = base(filters=[{"field":"state","op":"in","values":["SP"]}])
    delta = Delta.model_validate({"operations":[{"op":"replace_filter","id":filter_id(plan.filters[0]),"value":{"field":"amount","op":"gt","values":["1"]}}]})
    with pytest.raises(Exception): apply_operations(plan, delta.operations, ANCHOR)
    with pytest.raises(ValueError): Delta.model_validate({"operations":[],"sql":"DROP TABLE facts"})


def test_or_and_contradictory_filters_never_become_partial_queries():
    plan=base(filters=[{"field":"state","op":"in","values":["SP"]}])
    with pytest.raises(runtime.Clarification): turn("排除 SP",plan)
    with pytest.raises(runtime.Clarification): turn("商品金额大于100或订单状态为 canceled",plan)
    delta=Delta.model_validate({"operations":[{"op":"add_filter","value":{"field":"amount","op":"gte","values":["100"]}},{"op":"add_filter","value":{"field":"amount","op":"lt","values":["100"]}}]})
    with pytest.raises(runtime.Clarification): apply_operations(plan,delta.operations,ANCHOR)


def test_binding_ignores_password_and_tracks_revision():
    source = runtime.Datasource(host="host",database="test",user="readonly",password="one",analysis_date=ANCHOR)
    original = source_metadata(source,"alpha","ops")
    assert original == source_metadata(source.model_copy(update={"password":"two"}),"alpha","ops")
    assert original != source_metadata(source.model_copy(update={"context_revision":2}),"alpha","ops")


def test_model_delta_keeps_untouched_fields_and_rejects_scope_expansion(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY","fixture-key")
    monkeypatch.setenv("LLM_BASE_URL","https://provider.example")
    monkeypatch.setenv("LLM_MODEL","test")
    replies = [{"operations":[{"op":"add_filter","value":{"field":"state","op":"in","values":["SP"]}}]},
               {"operations":[{"op":"set_metrics","value":["orders"]}]}]
    requests=[]
    original=httpx.Client
    def respond(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200,json={"choices":[{"message":{"content":json.dumps(replies.pop(0))}}],"usage":{"total_tokens":10}})
    monkeypatch.setattr(runtime.httpx,"Client",lambda **options: original(transport=httpx.MockTransport(respond),**options))
    plan=base()
    updated,usage,_=turn("只看圣保罗州",plan)
    assert updated.metrics == plan.metrics and updated.dimensions == plan.dimensions and updated.time_range == plan.time_range
    assert updated.filters[0].values == ["SP"] and usage["tokens"] == 10
    with pytest.raises(Exception): turn("只看圣保罗州",plan)
    assert len(requests)==2 and all(set(r)=={"model","messages","max_tokens"} for r in requests)
