"""只从原始订单 CSV 计算预期，不导入任何 runtime 模块。"""
import csv
from decimal import Decimal, localcontext
from pathlib import Path


def aggregate(path: Path, period: dict, filters: list[dict]):
    groups = {}
    with path.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            if not period["start"] <= row["date"] < period["end_exclusive"]:
                continue
            matched = True
            for f in filters:
                value = row[{"state": "state", "status": "status", "amount": "amount"}[f["field"]]]
                values = f["values"]
                op = f["op"]
                if f["field"] == "amount":
                    if not value:
                        matched = False
                        break
                    a, b = Decimal(value), Decimal(values[0])
                    ok = {"eq": a == b, "ne": a != b, "gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
                else:
                    ok = value in values if op in {"in", "eq"} else value not in values
                if not ok:
                    matched = False
                    break
            if matched:
                groups.setdefault(row["state"], []).append(row)
    result = {}
    for state, rows in groups.items():
        valid = [r for r in rows if r["status"] not in {"canceled", "unavailable"}]
        gmv = sum((Decimal(r["amount"]) for r in valid), Decimal(0)) if valid and all(r["amount"] for r in valid) else None
        result[state] = {"gmv": gmv, "orders": Decimal(len(valid)), "aov": gmv / len(valid) if gmv is not None and valid else None}
    return result


def expected(path: Path, spec: dict):
    with localcontext() as ctx:
        ctx.prec = 38
        now = aggregate(path, spec["current"], spec.get("filters", []))
        if spec["goal"] == "top_n_total":
            if not now or any(r["gmv"] is None for r in now.values()):
                return {"complete": False, "reason": "no_data_or_unknown_amount"}
            states = sorted(now, key=lambda s: (-now[s]["gmv"], s))[:spec["n"]]
            total = sum((now[s]["gmv"] for s in states), Decimal(0))
            result = {"complete": True, "goal": "top_n_total", "states": sorted(states), "total": str(total), "extras": {}}
            orders = sum((now[s]["orders"] for s in states), Decimal(0))
            for check in spec.get("checks", []):
                value = orders if check == "orders" else total / orders if check == "aov" and orders else total / sum(r["gmv"] for r in now.values()) if check == "share" and sum(r["gmv"] for r in now.values()) else None
                result["extras"][check] = str(value) if value is not None else None
                if value is None:
                    result["complete"] = False
            return result
        before = aggregate(path, spec["baseline"], spec.get("filters", []))
        rows, excluded = [], []
        for state in sorted(now.keys() | before.keys()):
            a, b = before.get(state, {}).get("gmv"), now.get(state, {}).get("gmv")
            if a is None or b is None:
                excluded.append(state)
                continue
            delta = b-a
            rate = delta/a*100 if a else None
            if spec['ranking']=='rate' and rate is None:
                excluded.append(state)
                continue
            if delta < 0 and (spec["ranking"] == "absolute" or rate is not None):
                values = {}
                for m in ["gmv", "orders", "aov"]:
                    x, y = before[state][m], now[state][m]
                    if x is None or y is None:
                        return {"complete": False, "reason": "missing_check"}
                    values[m] = {"baseline": str(x), "current": str(y), "delta": str(y-x), "rate": str((y-x)/x*100) if x else None}
                rows.append({"state": state, "values": values})
        key = "delta" if spec["ranking"] == "absolute" else "rate"
        rows.sort(key=lambda r: (Decimal(r["values"]["gmv"][key]), r["state"]))
        comparable = (before.keys() & now.keys()) - set(excluded)
        return {"complete": bool(comparable), "goal": "compare_declines", "states": [r["state"] for r in rows[:spec["n"]]], "rows": rows[:spec["n"]], "excluded_states": excluded}


def same_number(a, b):
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(Decimal(str(a))-Decimal(str(b))) <= Decimal("0.0001")
    except Exception:
        return False


def score(report: dict | None, gold: dict):
    if not report or not report.get("complete") or not gold.get("complete"):
        return False
    if gold["goal"] == "top_n_total":
        return report.get("total", {}).get("states") == gold["states"] and same_number(report.get("total", {}).get("value"), gold["total"]) and all(same_number(report.get("extras", {}).get(k, {}).get("value"), v) for k, v in gold["extras"].items())
    rows = report.get("rows", [])
    if [r.get("state") for r in rows] != gold["states"] or sorted(report.get("excluded_states", [])) != sorted(gold["excluded_states"]):
        return False
    for row, expected in zip(rows, gold["rows"]):
        for metric, cells in expected["values"].items():
            if any(not same_number(row.get("values", {}).get(metric, {}).get(k), v) for k, v in cells.items()):
                return False
    return True
