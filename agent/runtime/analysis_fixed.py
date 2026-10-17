"""对照组固定流程：仅首次模型提取规格，后续按固定依赖顺序调用同一工具。"""
from .analysis import AnalysisError, Spec


def next_action(spec: Spec, artifacts):
    def result(action, args):
        return {"spec": spec.model_dump(mode="json"), "action": {"action": action, "arguments": args}}

    def query(period, metrics):
        return result("query_metrics", {"plan": {"version": 1, "metrics": metrics, "dimensions": ["state"],
                                               "time_range": period.model_dump(mode="json"),
                                               "filters": [f.model_dump(mode="json") for f in spec.filters],
                                               "order_by": [{"field": "state", "direction": "asc"}], "top_n": None}})

    queries = [a for a in artifacts if a["kind"] == "query"]
    if spec.goal == "compare_declines":
        for period in [spec.baseline, spec.current]:
            if not any(q["data"]["plan"]["time_range"] == period.model_dump(mode="json") for q in queries):
                return query(period, ["gmv", "orders", "aov"])
        comp = next((a for a in artifacts if a["kind"] == "comparison"), None)
        if not comp:
            left, right = [next(q for q in queries if q["data"]["plan"]["time_range"] == p.model_dump(mode="json")) for p in [spec.baseline, spec.current]]
            return result("compare_periods", {"baseline_ref": left["result_ref"], "current_ref": right["result_ref"], "metrics": ["gmv", "orders", "aov"]})
        chosen = next((a for a in artifacts if a["kind"] == "selection"), None)
        if not chosen:
            return result("rank_declines", {"result_ref": comp["result_ref"]})
        return result("finish", {"evidence_refs": [chosen["result_ref"]]})
    if not queries:
        return query(spec.current, ["gmv", "orders"] if set(spec.checks) & {"aov", "orders"} else ["gmv"])
    source = queries[0]
    scalars = [a for a in artifacts if a["kind"] == "scalar"]
    totals = {}
    for metric in ["gmv"] + (["orders"] if set(spec.checks) & {"aov", "orders"} else []):
        totals[metric] = next((a for a in scalars if a["data"]["op"] == "sum" and a["data"]["metric"] == metric and len(a["data"]["states"]) <= spec.n), None)
        if not totals[metric]:
            return result("calculate", {"op": "sum", "input": {"result_ref": source["result_ref"], "metric": metric}, "scope": "top_n"})
    refs = [a["result_ref"] for a in totals.values()]
    if "aov" in spec.checks:
        avg = next((a for a in scalars if a["data"]["metric"] == "aov"), None)
        if not avg:
            return result("calculate", {"op": "divide", "left_ref": totals["gmv"]["result_ref"], "right_ref": totals["orders"]["result_ref"]})
        refs.append(avg["result_ref"])
    if "share" in spec.checks:
        total = next((a for a in scalars if a["data"]["op"] == "sum" and len(a["data"]["states"]) == len(source["data"]["candidate_rows"]) and a["data"]["metric"] == "gmv"), None)
        if not total:
            return result("calculate", {"op": "sum", "input": {"result_ref": source["result_ref"], "metric": "gmv"}, "scope": "returned_groups"})
        share = next((a for a in scalars if a["data"]["metric"] == "ratio"), None)
        if not share:
            return result("calculate", {"op": "divide", "left_ref": totals["gmv"]["result_ref"], "right_ref": total["result_ref"]})
        refs.append(share["result_ref"])
    return result("finish", {"evidence_refs": refs})
