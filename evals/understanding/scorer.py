"""分离计划语义、条件和数值，避免执行成功掩盖条件丢失。"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from evals.oracle import compare


def canonical_filter(value):
    field, op = value["field"], value["op"]
    if field != "amount":
        op = {"eq": "in", "ne": "not_in"}.get(op, op)
        values = tuple(sorted(set(value["values"])))
    else:
        values = tuple(str(Decimal(v).normalize()) for v in value["values"])
    return field, op, values


def effective_order(plan):
    # 默认稳定分组排序属于执行语义，显式写出同一默认值不算新增条件。
    order = [(o["field"], o["direction"]) for o in plan["order_by"]]
    for dimension in sorted(plan["dimensions"], key=lambda d: d == "state"):
        if dimension not in [field for field, _ in order]:
            order.append((dimension, "asc"))
    return order


def plan_diff(actual, expected):
    differences = []
    for field in ("metrics", "dimensions", "time_range", "top_n"):
        a, e = actual[field], expected[field]
        equal = set(a) == set(e) if field in ("metrics", "dimensions") else a == e
        if not equal:
            differences.append({"field": field, "kind": "mismatch", "expected": e, "actual": a})
    if effective_order(actual) != effective_order(expected):
        differences.append({"field": "order_by", "kind": "mismatch", "expected": expected["order_by"], "actual": actual["order_by"]})
    a_filters = {canonical_filter(f) for f in actual["filters"]}
    e_filters = {canonical_filter(f) for f in expected["filters"]}
    missing, extra = e_filters - a_filters, a_filters - e_filters
    for value in sorted(missing):
        match = next((other for other in extra if other[0] == value[0]), None)
        kind = "missing"
        if match is not None:
            opposites = {("in", "not_in"), ("not_in", "in"), ("eq", "ne"), ("ne", "eq")}
            kind = "negation_reversed" if (value[1], match[1]) in opposites and value[2] == match[2] else "operator_or_value"
            extra.remove(match)
        differences.append({"field": "filters", "kind": kind, "expected": value, "actual": match})
    for value in sorted(extra):
        differences.append({"field": "filters", "kind": "extra", "expected": None, "actual": value})
    return differences


def score(case, task, trace, rows):
    result = task.get("result") or {}
    verdict = {"passed": False, "intent_correct": None, "conditions_complete": None,
               "result_correct": None, "boundary_correct": None, "differences": []}
    if case["expected_status"] != "completed":
        reason_ok = trace.get("error_code") in case["error_codes"]
        if case["message_any"]:
            message = result.get("message", "") + task.get("error", "")
            reason_ok = reason_ok and any(word in message for word in case["message_any"])
        denied = task.get("http_status") in (403, 404) or (task.get("status") == "failed" and trace.get("error_code") == "permission")
        status_ok = denied if case["expected_status"] == "denied" else task.get("status") == "needs_clarification"
        verdict["boundary_correct"] = status_ok and reason_ok and trace.get("sql_calls") == 0
        verdict["passed"] = verdict["boundary_correct"]
        return verdict
    if task.get("status") != "completed" or not isinstance(result.get("plan"), dict):
        return verdict
    expected = case["expected"]
    try:
        differences = plan_diff(result["plan"], expected)
        verdict.update(differences=differences, intent_correct=not differences, conditions_complete=not differences)
        # 数值判定独立于实际计划，防止计划错误但数值巧合一致时漏报意图错误。
        numeric = deepcopy(result)
        numeric["plan"] = expected
        verdict["result_correct"] = compare(numeric, expected, rows)
        verdict["passed"] = verdict["intent_correct"] and verdict["result_correct"] and trace.get("sql_calls", 0) > 0
    except (KeyError, TypeError, ValueError, InvalidOperation, ArithmeticError):
        verdict.update(intent_correct=False, conditions_complete=False, result_correct=False,
                       differences=[{"field": "result", "kind": "invalid_protocol"}])
    return verdict
