"""从手工订单与独立预期验证计算，覆盖不会被 schema 检查发现的业务错误。"""
import json
from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from runtime import app as runtime
from runtime.analysis import (AnalysisError, BinaryArgs, CompareArgs, Decision, FinishArgs, RankArgs, Spec,
                              SumArgs, artifact, calculate, compare, finish, rank)
from runtime.analysis_api import Identity, context, decision_prompt, Snapshot
from runtime.analysis_fixed import next_action
from runtime.plan import QueryPlan

BINDING = "b" * 64
PERIODS = [{"start": "2018-06-01", "end_exclusive": "2018-07-01"}, {"start": "2018-07-01", "end_exclusive": "2018-08-01"}]


def spec(goal="compare_declines", **changes):
    return Spec.model_validate({"goal": goal, "current": PERIODS[1], "baseline": PERIODS[0] if goal == "compare_declines" else None,
                               "ranking": "absolute" if goal == "compare_declines" else None, "n": 3, **changes})


def query(period, values, top=None):
    plan = QueryPlan.model_validate({"metrics": ["gmv", "orders", "aov"], "dimensions": ["state"], "time_range": period}).model_dump(mode="json")
    rows = [{"dimensions": {"state": state}, "values": {m: {"value": v, "sample_size": 1} for m, v in zip(["gmv", "orders", "aov"], cells)}} for state, cells in values]
    return artifact("query", {"plan": plan, "rows": rows[:top] if top else rows, "candidate_rows": rows,
                              "coverage": "selected_top_n" if top else "complete", "quality_complete": all(r["values"]["gmv"]["value"] is not None for r in rows)}, ["gmv", "orders", "aov"], BINDING)


def test_compare_rank_check_evidence_and_descriptive_report():
    a = query(PERIODS[0], [("SP", ["1000", "10", "100"]), ("RJ", ["900", "10", "90"]), ("MG", ["900", "10", "90"]), ("BA", ["0", "1", "0"])])
    b = query(PERIODS[1], [("SP", ["720", "8", "90"]), ("RJ", ["800", "10", "80"]), ("MG", ["800", "10", "80"]), ("BA", ["0", "1", "0"]), ("AC", [None, "2", None])])
    artifacts = [a, b]
    c = compare(CompareArgs(baseline_ref=a["result_ref"], current_ref=b["result_ref"], metrics=["gmv", "orders", "aov"]), artifacts, spec(), BINDING)
    assert c["data"]["rows"][-1]["values"]["gmv"]["rate"] == "-28.00"
    artifacts.append(c)
    chosen = rank(RankArgs(result_ref=c["result_ref"]), artifacts, spec(), BINDING)
    assert chosen["data"]["states"] == ["SP", "MG", "RJ"]
    assert chosen["data"]["excluded_states"] == ["AC"]
    artifacts.append(chosen)
    report = finish(FinishArgs(evidence_refs=[chosen["result_ref"]]), artifacts, spec(), BINDING)
    assert report["data"]["rows"][0]["values"]["orders"]["rate"] == "-20.0"
    assert "不代表因果" in report["data"]["summary"]
    assert "两期天数不同" in report["data"]["summary"]
    explained = finish(FinishArgs(evidence_refs=[chosen["result_ref"]], findings=[
        {"kind": "orders_declined", "state": "SP", "result_ref": c["result_ref"]},
        {"kind": "gmv_declined", "state": "SP", "result_ref": chosen["result_ref"]}]), artifacts, spec(), BINDING)
    assert not explained["data"]["narrative_fallback"]
    assert explained["data"]["narrative"][0]["kind"] == "orders_declined"
    assert "1000.00" in explained["data"]["narrative"][1]["text"]
    for kind in ["marketing_caused_decline", "aov_increased"]:
        unsafe = finish(FinishArgs(evidence_refs=[chosen["result_ref"]], findings=[
            {"kind": kind, "state": "SP", "result_ref": c["result_ref"]}]), artifacts, spec(), BINDING)
        assert unsafe["data"]["narrative_fallback"]
        assert all(kind != p["kind"] for p in unsafe["data"]["narrative"])
    with pytest.raises(AnalysisError):
        compare(CompareArgs(baseline_ref=b["result_ref"], current_ref=a["result_ref"], metrics=["gmv"]), artifacts, spec(), BINDING)


def test_sum_and_weighted_aov_not_mean_of_means():
    q = query(PERIODS[1], [("SP", ["100", "10", "10"]), ("RJ", ["80", "2", "40"])])
    artifacts = [q]
    for metric in ["gmv", "orders"]:
        artifacts.append(calculate(SumArgs(op="sum", input={"result_ref": q["result_ref"], "metric": metric}, scope="returned_groups"), artifacts, spec("top_n_total"), BINDING))
    avg = calculate(BinaryArgs(op="divide", left_ref=artifacts[1]["result_ref"], right_ref=artifacts[2]["result_ref"]), artifacts, spec("top_n_total"), BINDING)
    assert Decimal(avg["data"]["value"]) == 15 and avg["data"]["unit"] == "BRL/单"
    with pytest.raises(AnalysisError):
        calculate(SumArgs(op="sum", input={"result_ref": q["result_ref"], "metric": "aov"}, scope="returned_groups"), artifacts, spec("top_n_total"), BINDING)
    with pytest.raises(AnalysisError):
        calculate(BinaryArgs(op="add", left_ref=artifacts[1]["result_ref"], right_ref=artifacts[1]["result_ref"]), artifacts, spec("top_n_total"), BINDING)


def test_unknown_amount_outside_top_n_prevents_false_complete_total():
    q = query(PERIODS[1], [("SP", ["100", "1", "100"]), ("RJ", [None, "1", None])], top=1)
    scalar = calculate(SumArgs(op="sum", input={"result_ref": q["result_ref"], "metric": "gmv"}, scope="returned_groups"), [q], spec("top_n_total", n=1), BINDING)
    assert scalar["data"]["value"] == "100" and not scalar["data"]["quality_complete"]
    with pytest.raises(AnalysisError, match="完整"):
        finish(FinishArgs(evidence_refs=[scalar["result_ref"]]), [q, scalar], spec("top_n_total", n=1), BINDING)


@pytest.mark.parametrize("values", [[], [("SP", [None, "1", None])]])
def test_empty_and_null_are_not_zero(values):
    q = query(PERIODS[1], values)
    s = calculate(SumArgs(op="sum", input={"result_ref": q["result_ref"], "metric": "gmv"}, scope="returned_groups"), [q], spec("top_n_total"), BINDING)
    assert s["data"]["value"] is None


def test_protocol_rejects_sql_identity_and_unknown_math():
    for action in [{"action": "query_metrics", "arguments": {"sql": "SELECT 1"}}, {"action": "calculate", "arguments": {"op": "eval", "value": "1+1"}}]:
        with pytest.raises(ValidationError):
            Decision.model_validate({"spec": None, "action": action})
    with pytest.raises(ValidationError):
        Spec.model_validate({**spec().model_dump(), "tenant_id": "other"})


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SERVICE_KEY", "s" * 40)
    monkeypatch.setenv("TENANT_DATASOURCES", json.dumps({"alpha:ops": {"host": "mysql", "database": "olist_alpha", "user": "alpha_ro", "password": "fixture", "analysis_date": "2018-08-31", "dataset_revision": "d"*64}}))
    runtime.sources.cache_clear()
    with TestClient(runtime.app) as client:
        yield client
    runtime.sources.cache_clear()


def test_tool_query_skips_parser_checks_full_candidates_and_preserves_identity(client, monkeypatch):
    identity = {"tenant_id": "alpha", "role": "ops", "allowed_metrics": ["gmv", "orders", "aov"]}
    binding = context(Identity.model_validate(identity))["binding"]
    calls = []
    def execute(source, sql, params):
        calls.append((source.database, sql, params))
        return [{"d_state": "SP", "m0_value": "100", "m0_sample": 1, "total_orders": 1}, {"d_state": "RJ", "m0_value": None, "m0_sample": 0, "total_orders": 1}]
    monkeypatch.setattr(runtime, "execute", execute)
    monkeypatch.setattr(runtime, "parse_query_plan", lambda *a: pytest.fail("tool called parser"))
    body = {**identity, "binding": binding, "question": "GMV 前一州合计", "spec": spec("top_n_total", n=1).model_dump(mode="json"),
            "action": {"action": "query_metrics", "arguments": {"plan": {"version": 1, "metrics": ["gmv"], "dimensions": ["state"], "time_range": PERIODS[1], "order_by": [{"field": "gmv", "direction": "desc"}], "top_n": 1}}}}
    response = client.post("/internal/analysis/tool", headers={"X-Service-Key": "s"*40}, json=body)
    assert response.status_code == 200
    result = response.json()["artifact"]["data"]
    assert len(result["rows"]) == 1 and len(result["candidate_rows"]) == 2 and not result["quality_complete"]
    assert len(calls) == 1 and calls[0][0] == "olist_alpha" and calls[0][1].endswith(" LIMIT 1001")
    body["action"]["arguments"]["plan"]["time_range"] = PERIODS[0]
    assert client.post("/internal/analysis/tool", headers={"X-Service-Key": "s"*40}, json=body).json()["error_code"] == "invalid_arguments"
    assert len(calls) == 1


def test_model_prompt_bound_is_small_for_complete_three_metric_trajectory(client):
    request = Snapshot(tenant_id="alpha", role="ops", allowed_metrics=["gmv", "orders", "aov"], binding="b"*64, question="比较六月和七月，按 GMV 下降金额取前三并核查订单量客单价", spec=spec())
    a = query(PERIODS[0], [(s, ["100", "1", "100"]) for s in ["SP", "RJ", "MG"]])
    b = query(PERIODS[1], [(s, ["50", "1", "50"]) for s in ["SP", "RJ", "MG"]])
    c = compare(CompareArgs(baseline_ref=a["result_ref"], current_ref=b["result_ref"], metrics=["gmv", "orders", "aov"]), [a,b], spec(), BINDING)
    request.artifacts = [a,b,c,rank(RankArgs(result_ref=c["result_ref"]), [a,b,c], spec(), BINDING)]
    prompt, message = decision_prompt(request, date(2018,8,31))
    assert len(prompt.encode())+len(message.encode())+256 <= 6000


def test_model_sees_catalog_definitions_and_scalar_types_within_budget():
    target=spec('top_n_total',n=1,checks=['orders','aov','share'])
    source=query(PERIODS[1],[('SP',['100','10','10']),('RJ',['80','2','40'])])
    artifacts=[artifact('catalog',{'gmv':{'definition':'有效订单商品金额，不含运费','unit':'BRL'}},['gmv'],BINDING),source]
    request=Snapshot(tenant_id='alpha',role='ops',allowed_metrics=['gmv','orders','aov'],binding=BINDING,question='2018年7月 GMV 前一州合计、整体客单价和占比',spec=target)
    for _ in range(10):
        request.artifacts=artifacts
        prompt,message=decision_prompt(request,date(2018,8,31))
        assert len(prompt.encode())+len(message.encode())+256<=6000
        observation=json.loads(message)['evidence']
        assert observation[0]['definitions']['gmv']['unit']=='BRL'
        assert all('op' in a and 'metric' in a and 'unit' in a for a in observation if a['kind']=='scalar')
        decision=Decision.model_validate(next_action(target,artifacts))
        if decision.action.action=='finish':
            assert finish(decision.action.arguments,artifacts,target,BINDING)['data']['complete']
            break
        assert decision.action.action=='calculate'
        artifacts.append(calculate(decision.action.arguments,artifacts,target,BINDING))
    else:
        pytest.fail('bounded complete trajectory did not finish')


@pytest.mark.parametrize('limit', [1024, 4096])
def test_decision_request_sends_bounded_output_limit(client, monkeypatch, limit):
    import httpx
    from runtime.model_transport import ModelTransport
    monkeypatch.setenv('LLM_BASE_URL', 'https://fixture.invalid/v1')
    monkeypatch.setenv('LLM_MODEL', 'fixture')
    monkeypatch.setenv('LLM_API_KEY', 'test-only-no-network')
    observed = []
    def respond(request):
        observed.append(json.loads(request.content)['max_tokens'])
        decision = {'spec': None, 'action': {'action': 'request_clarification',
            'arguments': {'missing_fields': ['current'], 'message': '请补充日期'}}}
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
            'message': {'content': json.dumps(decision)}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 500}})
    monkeypatch.setattr(ModelTransport, 'bounded_client', lambda self: httpx.Client(transport=httpx.MockTransport(respond)))
    identity = {'tenant_id': 'alpha', 'role': 'ops', 'allowed_metrics': ['gmv']}
    body = {**identity, 'binding': context(Identity.model_validate(identity))['binding'],
        'question': '统计 GMV', 'max_output_tokens': limit}
    response = client.post('/internal/analysis/decide', headers={'X-Service-Key': 's'*40}, json=body)
    assert response.status_code == 200 and response.json()['status'] == 'completed'
    assert observed == [limit]
