"""黄金结果只读取原始夹具和人工条件，不导入运行时语义公式。"""
import csv
from collections import defaultdict
from decimal import Decimal


def facts(path):
    with path.open(encoding="utf-8", newline="") as source:
        return [{**r, **{k: Decimal(r[k]) if r[k] else None for k in ("amount", "delivery", "review")}} for r in csv.DictReader(source)]


def calculate(rows):
    valid = [r for r in rows if r["status"] not in ("canceled", "unavailable")]
    priced = [r for r in valid if r["amount"] is not None]
    gmv = sum((r["amount"] for r in priced), Decimal(0)) if valid and len(priced) == len(valid) else None
    deliveries = [r["delivery"] for r in rows if r["delivery"] is not None]
    reviews = [r["review"] for r in rows if r["review"] is not None]
    return {
        "gmv": (gmv, len(priced)), "orders": (Decimal(len(valid)) if rows else None, len(valid)),
        "aov": (gmv / len(valid) if gmv is not None else None, len(valid)),
        "cancellation_rate": (Decimal(100 * sum(r["status"] == "canceled" for r in rows)) / len(rows) if rows else None, len(rows)),
        "delivery_days": (sum(deliveries) / len(deliveries) if deliveries else None, len(deliveries)),
        "review_score": (sum(reviews) / len(reviews) if reviews else None, len(reviews)),
    }


def filter_matches(row, condition):
    value = row[condition["field"]]
    targets = [Decimal(v) if condition["field"] == "amount" else v for v in condition["values"]]
    if value is None:
        return False
    op = condition["op"]
    if op == "in": return value in targets
    if op == "not_in": return value not in targets
    return {"eq": lambda: value == targets[0], "ne": lambda: value != targets[0], "gt": lambda: value > targets[0],
            "gte": lambda: value >= targets[0], "lt": lambda: value < targets[0], "lte": lambda: value <= targets[0]}[op]()


def normalize_filters(filters):
    return sorted((f["field"], {"eq": "in", "ne": "not_in"}.get(f["op"], f["op"]) if f["field"] != "amount" else f["op"], tuple(sorted(f["values"]))) for f in filters)


def compare(result, expected, rows):
    actual = result["plan"]
    for key in ("metrics", "dimensions", "time_range", "top_n", "order_by"):
        if actual[key] != expected[key]: return False
    if normalize_filters(actual["filters"]) != normalize_filters(expected["filters"]): return False
    groups = defaultdict(list)
    start, end = expected["time_range"].values()
    dimensions = expected["dimensions"]
    for row in rows:
        if start <= row["purchased_at"] < end and all(filter_matches(row, f) for f in expected["filters"]):
            key = tuple(row["purchased_at"][:7] if d == "month" else row["purchased_at"][:10] if d == "day" else row[d] for d in dimensions)
            groups[key].append(row)
    if not dimensions and not groups: groups[()] = []
    values = {k: calculate(v) for k, v in groups.items()}
    # 日期/州自然顺序及排名同值的稳定维度顺序，与运行时独立实现。
    ordered = sorted(groups)
    for ordering in reversed(expected["order_by"]):
        field, descending = ordering["field"], ordering["direction"] == "desc"
        if field in dimensions:
            ordered.sort(key=lambda k: k[dimensions.index(field)], reverse=descending)
        else:
            defined = [k for k in ordered if values[k][field][0] is not None]
            missing = [k for k in ordered if values[k][field][0] is None]
            ordered = sorted(defined, key=lambda k: values[k][field][0], reverse=descending) + missing
    if expected["top_n"]: ordered = ordered[:expected["top_n"]]
    if [tuple(r["dimensions"][d] for d in dimensions) for r in result["rows"]] != ordered: return False
    for actual_row, key in zip(result["rows"], ordered, strict=True):
        if actual_row["total_orders"] != len(groups[key]) or actual_row["unavailable_orders"] != sum(r["status"] == "unavailable" for r in groups[key]): return False
        for metric in expected["metrics"]:
            golden, samples = values[key][metric]
            cell = actual_row["values"][metric]
            if (cell["value"] is None) != (golden is None): return False
            if golden is not None and abs(Decimal(str(cell["value"])) - golden) > (0 if metric in ("gmv", "orders") else Decimal("0.0001")): return False
            if cell["sample_size"] != samples or cell["excluded_orders"] != len(groups[key]) - samples: return False
    return True
