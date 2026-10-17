"""独立预期覆盖否定范围、完整原文兜底和不会推进上下文的失败。"""
from datetime import date
import json

import httpx
import pytest
from fastapi import HTTPException

from runtime import app as runtime
from runtime.intent import Clarification, RuleMiss
from runtime.multiturn import Pending, QueryContext, resolve_followup
from runtime.plan import QueryPlan


ANCHOR = date(2018, 8, 31)


def plan(**changes):
    return QueryPlan.model_validate({"metrics": ["gmv"], "dimensions": [],
        "time_range": {"start": "2018-08-01", "end_exclusive": "2018-09-01"}, **changes})


def request(question, base=None, pending=None, **choices):
    context = QueryContext(binding="a" * 64, plan=base, pending=pending, version=1) if base is not None or pending else None
    return runtime.QueryRequest(tenant_id="alpha", role="ops", allowed_metrics=["gmv", "orders"],
        input={"question": question, **choices}, context=context)


def model(monkeypatch, content, finish_reason="stop"):
    monkeypatch.setenv("LLM_API_KEY", "fixture-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.example")
    monkeypatch.setenv("LLM_MODEL", "fixture-model")
    calls = []
    original = httpx.Client
    def respond(req):
        calls.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": finish_reason,
            "message": {"content": json.dumps(content)}}], "usage": {"total_tokens": 12}})
    monkeypatch.setattr(runtime.httpx, "Client", lambda **options: original(transport=httpx.MockTransport(respond), **options))
    return calls


@pytest.mark.parametrize("suffix,op,values", [
    ("只看 SP", "in", ["SP"]), ("只看 sp", "in", ["SP"]),
    ("不包含 SP", "not_in", ["SP"]), ("排除 SP", "not_in", ["SP"]),
    ("不看 sp，看其他州", "not_in", ["SP"]),
    ("只看 SP 和 RJ", "in", ["SP", "RJ"]),
    ("不包含 SP 和 RJ", "not_in", ["SP", "RJ"]),
])
def test_complete_state_clauses_keep_polarity_without_model(monkeypatch, suffix, op, values):
    calls = model(monkeypatch, {})
    actual, usage = runtime.parse_query_plan(request("本月 GMV，" + suffix), ANCHOR)
    assert actual == plan(filters=[{"field": "state", "op": op, "values": values}])
    assert usage == {"parser": "rules", "tokens": 0} and calls == []


def test_explicit_all_states_is_not_state_exclusion(monkeypatch):
    calls = model(monkeypatch, {})
    actual, _ = runtime.parse_query_plan(request("本月 GMV，不要只看 SP，查看全部州"), ANCHOR)
    assert actual == plan() and calls == []


def test_all_states_clause_does_not_leave_earlier_filter(monkeypatch):
    calls = model(monkeypatch, {})
    actual, _ = runtime.parse_query_plan(request("本月 GMV，只看 SP，查看全部州"), ANCHOR)
    assert actual == plan() and calls == []


def test_named_state_exclusion_keeps_negative_operator(monkeypatch):
    expected = plan(filters=[{"field": "state", "op": "not_in", "values": ["SP"]}])
    calls = model(monkeypatch, expected.model_dump(mode="json"))
    actual, _ = runtime.parse_query_plan(request("本月 GMV，排除圣保罗州"), ANCHOR)
    assert actual == expected and len(calls) == 1


@pytest.mark.parametrize("question", [
    "本月 GMV，千万别只看 sp，剩余州都算上", "本月 GMV，不能只取 SP 的数据",
    "本月 GMV，别仅按州汇总，按月给我", "本月 GMV，别把订单量也算进来",
])
def test_unknown_scope_reaches_model_with_original_question(monkeypatch, question):
    expected = plan(dimensions=["month"] if "按月" in question else [])
    calls = model(monkeypatch, expected.model_dump(mode="json"))
    actual, usage = runtime.parse_query_plan(request(question), ANCHOR)
    assert actual == expected and usage["parser"] == "llm" and len(calls) == 1
    assert calls[0]["messages"][1]["content"] == question


def test_unknown_negation_does_not_lock_positive_substring(monkeypatch):
    question = "本月 GMV，不是只看 SP，而是排除 SP"
    expected = plan(filters=[{"field": "state", "op": "not_in", "values": ["SP"]}])
    calls = model(monkeypatch, expected.model_dump(mode="json"))
    actual, _ = runtime.parse_query_plan(request(question), ANCHOR)
    assert actual == expected and len(calls) == 1


def test_unknown_expression_cannot_override_manual_choice(monkeypatch):
    calls = model(monkeypatch, plan(metrics=["orders"]).model_dump(mode="json"))
    with pytest.raises(Clarification):
        runtime.parse_query_plan(request("本月给我看看卖得如何", metric="gmv"), ANCHOR)
    assert len(calls) == 1


def test_unknown_expression_still_checks_all_metric_permissions(monkeypatch):
    calls = model(monkeypatch, plan(metrics=["review_score"]).model_dump(mode="json"))
    with pytest.raises(HTTPException) as error:
        runtime.parse_query_plan(request("本月给我看看评价如何"), ANCHOR)
    assert error.value.status_code == 403 and len(calls) == 1


def test_unknown_without_model_is_rule_miss_not_unsupported(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(RuleMiss):
        runtime.parse_query_plan(request("本月 GMV，不要只看 SP"), ANCHOR)


@pytest.mark.parametrize("question", ["本月 GMV 趋势", "本月 GMV 分组", "本月 GMV 和统计", "本月 GMV，并非只看 SP"])
def test_unbound_requirements_are_not_discarded_as_filler(monkeypatch, question):
    calls = model(monkeypatch, {"clarification": "请明确完整条件"})
    with pytest.raises(Clarification): runtime.parse_query_plan(request(question), ANCHOR)
    assert len(calls) == 1 and calls[0]["messages"][1]["content"] == question


def test_named_filter_does_not_allow_model_to_invent_missing_time(monkeypatch):
    calls = model(monkeypatch, plan(filters=[{"field": "state", "op": "in", "values": ["SP"]}]).model_dump(mode="json"))
    with pytest.raises(Clarification): runtime.parse_query_plan(request("GMV，只看圣保罗州"), ANCHOR)
    assert calls == []


def test_followup_explicit_other_states_replaces_old_inclusion(monkeypatch):
    calls = model(monkeypatch, {})
    base = plan(dimensions=["month"], filters=[{"field": "state", "op": "in", "values": ["SP"]}])
    req = request("不看 sp，看其他州", base)
    actual, usage, _ = resolve_followup(req, ANCHOR, runtime.parse_query_plan)
    assert actual == base.model_copy(update={"filters": plan(filters=[{"field": "state", "op": "not_in", "values": ["SP"]}]).filters})
    assert usage["parser"] == "rules" and calls == []


def test_unknown_followup_has_model_fallback_without_rule_targets(monkeypatch):
    base = plan(dimensions=["month"])
    question = "换个角度，用一张州别汇总给我"
    calls = model(monkeypatch, {"operations": [{"op": "set_dimensions", "value": ["state"]}]})
    actual, usage, _ = resolve_followup(request(question, base), ANCHOR, runtime.parse_query_plan)
    assert actual == base.model_copy(update={"dimensions": ["state"]})
    assert usage["parser"] == "llm" and len(calls) == 1
    assert calls[0]["messages"][1]["content"] == question


def test_unknown_followup_negation_does_not_force_bad_operation(monkeypatch):
    base = plan(filters=[{"field": "state", "op": "in", "values": ["SP"]}])
    calls = model(monkeypatch, {"operations": [{"op": "clear_filters", "field": "state"}]})
    actual, _, _ = resolve_followup(request("千万不要只看 SP", base), ANCHOR, runtime.parse_query_plan)
    assert actual == plan() and len(calls) == 1


def test_model_cannot_discard_unknown_followup_with_empty_operations(monkeypatch):
    calls = model(monkeypatch, {"operations": []})
    with pytest.raises(HTTPException) as error:
        resolve_followup(request("换个方式给我看看", plan()), ANCHOR, runtime.parse_query_plan)
    assert error.value.status_code == 503 and len(calls) == 1


def test_half_year_normalization_keeps_original_model_message(monkeypatch):
    base = plan()
    question = "近半年换个说法汇总给我"
    calls = model(monkeypatch, {"clarification": "请明确汇总维度"})
    actual, _, _ = resolve_followup(request(question, base), ANCHOR, runtime.parse_query_plan)
    assert actual is None and len(calls) == 1 and calls[0]["messages"][1]["content"] == question


def test_initial_model_clarification_is_not_called_again_for_draft(monkeypatch):
    calls = model(monkeypatch, {"clarification": "请明确州范围"})
    req = request("本月 GMV，不要只看 SP")
    req.context = QueryContext(binding="a" * 64, version=1)
    actual, _, info = resolve_followup(req, ANCHOR, runtime.parse_query_plan)
    assert actual is None and "expression" in info["pending"]["missing"] and len(calls) == 1


@pytest.mark.parametrize("response", [
    {"operations": [{"op": "set_metrics", "value": ["review_score"]}]},
    {"operations": [{"op": "clear_filters", "field": "sql"}]},
    {"operations": [], "sql": "SELECT 1"},
])
def test_unknown_followup_still_validates_permissions_and_schema(monkeypatch, response):
    base = plan()
    original = base.model_dump(mode="json")
    calls = model(monkeypatch, response)
    with pytest.raises((HTTPException, Clarification)):
        resolve_followup(request("换个方式给我看看", base), ANCHOR, runtime.parse_query_plan)
    assert base.model_dump(mode="json") == original and len(calls) == 1


def test_all_states_followup_keeps_amount_filter(monkeypatch):
    calls = model(monkeypatch, {})
    base = plan(filters=[{"field": "state", "op": "in", "values": ["SP"]},
        {"field": "amount", "op": "gt", "values": ["100"]}])
    actual, _, _ = resolve_followup(request("不要只看 SP，查看全部州", base), ANCHOR, runtime.parse_query_plan)
    assert [f.field for f in actual.filters] == ["amount"] and calls == []


def test_existing_rules_and_boundary_chains_keep_independent_expectations(monkeypatch):
    from evals.multiturn.dataset import load
    from evals.understanding.scorer import plan_diff
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    for chain in load().chains:
        if chain.category == "unfamiliar": continue
        base = pending = None
        for turn in chain.turns:
            if turn.action:
                pending = None
                continue
            req = runtime.QueryRequest(tenant_id="alpha", role=chain.identity,
                allowed_metrics=["gmv", "orders", "aov", "cancellation_rate"] if chain.identity == "ops" else
                    ["gmv", "orders", "aov", "review_score", "delivery_days", "cancellation_rate"],
                input={"question": turn.question}, context=QueryContext(binding="a" * 64, version=1, plan=base, pending=pending))
            try:
                new, _, info = resolve_followup(req, ANCHOR, runtime.parse_query_plan)
                status = "completed" if new else "needs_clarification"
                if new: base, pending = new, None
                elif info.get("pending"): pending = Pending.model_validate(info["pending"])
            except HTTPException as exc:
                assert exc.status_code == 403
                status = "denied"
            except Clarification:
                status = "needs_clarification"
            assert status == turn.status, (chain.id, turn.question)
            assert (base is None) == (turn.expected is None)
            if base:
                assert not plan_diff(base.model_dump(mode="json"), turn.expected.model_dump()), (chain.id, turn.question)
