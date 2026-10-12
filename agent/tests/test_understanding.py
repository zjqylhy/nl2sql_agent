"""用独立预期和变异案例验证判定，尤其是数字正确但意图错误。"""
import copy
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from evals.oracle import facts
from evals.understanding.dataset import Dataset, load
from evals.understanding.scorer import plan_diff, score


def example():
    case = load().cases[0].model_dump()
    # 原始 CSV 的八月有效订单：金额 330、有效订单 5，无不可履约订单。
    result = {"plan": copy.deepcopy(case["expected"]), "rows": [{"dimensions": {}, "total_orders": 7,
              "unavailable_orders": 1, "values": {"gmv": {"value": "330", "sample_size": 5, "excluded_orders": 2}}}]}
    return case, {"status": "completed", "result": result}, {"sql_calls": 1, "model_calls": 0}


def test_dataset_groups_counts_and_review_attribution():
    data = load()
    assert len(data.cases) == 90 and len({c.group for c in data.cases}) >= 45
    assert Counter(c.category for c in data.cases) == {"common": 30, "unfamiliar": 30, "missing": 8, "conflict": 6, "unsupported": 8, "permission": 8}
    assert data.annotation_status == "draft" and data.reviewer is None
    value = data.model_dump()
    value["annotation_status"] = "human_reviewed"
    with pytest.raises(ValueError):
        Dataset.model_validate(value)
    value = data.model_dump()
    pair = next((a, b) for a in value["cases"] for b in value["cases"] if a["id"] != b["id"] and a["group"] == b["group"])
    pair[1]["split"] = "acceptance" if pair[0]["split"] == "development" else "development"
    with pytest.raises(ValueError):
        Dataset.model_validate(value)


@pytest.mark.parametrize("mutation,kind", [
    (lambda p: p["filters"].clear(), "missing"),
    (lambda p: p["filters"].append({"field": "status", "op": "in", "values": ["delivered"]}), "extra"),
    (lambda p: p["filters"][0].update(op="not_in"), "negation_reversed"),
    (lambda p: p["filters"][0].update(values=["RJ"]), "operator_or_value"),
    (lambda p: p.update(top_n=3), "mismatch"),
    (lambda p: p["order_by"].append({"field": "gmv", "direction": "desc"}), "mismatch"),
    (lambda p: p.update(time_range={"start": "2018-07-01", "end_exclusive": "2018-09-01"}), "mismatch"),
])
def test_plan_detects_missing_extra_negation_values_and_order(mutation, kind):
    expected = load().cases[12].expected.model_dump()
    actual = copy.deepcopy(expected)
    mutation(actual)
    assert any(d["kind"] == kind for d in plan_diff(actual, expected))


def test_equivalent_enums_decimal_metric_order_and_default_order():
    expected = load().cases[24].expected.model_dump()
    actual = copy.deepcopy(expected)
    actual["filters"].reverse()
    next(f for f in actual["filters"] if f["field"] == "state")["op"] = "eq"
    next(f for f in actual["filters"] if f["field"] == "amount")["values"] = ["50.00"]
    actual["order_by"] = [{"field": "day", "direction": "asc"}]
    assert plan_diff(actual, expected) == []
    expected = load().cases[7].expected.model_dump()
    actual = copy.deepcopy(expected)
    actual["metrics"].reverse()
    assert plan_diff(actual, expected) == []


def test_correct_numbers_cannot_hide_wrong_plan():
    case, task, trace = example()
    rows = facts(ROOT / "evals/fixture.csv")
    assert score(case, task, trace, rows)["passed"]
    task["result"]["plan"]["filters"] = [{"field": "state", "op": "in", "values": ["SP"]}]
    verdict = score(case, task, trace, rows)
    assert verdict["result_correct"] is True
    assert verdict["intent_correct"] is False and verdict["passed"] is False


def test_wrong_counts_samples_and_zero_are_rejected():
    rows = facts(ROOT / "evals/fixture.csv")
    case, task, trace = example()
    for field in ("value", "sample_size", "excluded_orders"):
        invalid = copy.deepcopy(task)
        invalid["result"]["rows"][0]["values"]["gmv"][field] = 0
        assert not score(case, invalid, trace, rows)["result_correct"]
    case["expected"]["time_range"] = {"start": "2016-01-01", "end_exclusive": "2016-02-01"}
    task["result"]["plan"] = copy.deepcopy(case["expected"])
    task["result"]["rows"][0].update(total_orders=0, unavailable_orders=0)
    task["result"]["rows"][0]["values"]["gmv"].update(value=None, sample_size=0, excluded_orders=0)
    assert score(case, task, trace, rows)["passed"]
    task["result"]["rows"][0]["values"]["gmv"]["value"] = "0"
    assert not score(case, task, trace, rows)["result_correct"]


def test_failures_without_plan_are_unobservable_and_count_as_failure():
    case, _, trace = example()
    verdict = score(case, {"status": "failed"}, trace, [])
    assert not verdict["passed"] and verdict["intent_correct"] is None
    assert verdict["differences"] == []


def test_boundary_requires_reason_and_no_sql():
    case = next(c.model_dump() for c in load().cases if c.category == "missing")
    task = {"status": "needs_clarification", "result": {"message": "请明确时间范围"}}
    trace = {"error_code": "clarification", "sql_calls": 0}
    assert score(case, task, trace, [])["passed"]
    for changed in ({"error_code": "unsupported", "sql_calls": 0}, {"error_code": "clarification", "sql_calls": 1}):
        assert not score(case, task, changed, [])["passed"]
    task["result"]["message"] = "请确认"
    assert not score(case, task, trace, [])["passed"]
