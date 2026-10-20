"""有限分析协议与确定性计算；模型只能引用服务端保存的证据。"""
import hashlib
import json
from datetime import date
from decimal import Decimal, localcontext
from typing import Annotated, Literal, Union

from pydantic import Field, model_validator

from .catalog import StrictModel, catalog
from .plan import Filter, QueryPlan, TimeRange, validate_filters

METRICS = {"gmv", "orders", "aov"}
VERSION = "analysis-1"


class AnalysisError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def reject(code="invalid_arguments", message="参数、范围或证据不符合分析要求"):
    raise AnalysisError(code, message)


class Spec(StrictModel):
    goal: Literal["compare_declines", "top_n_total"]
    current: TimeRange
    baseline: TimeRange | None = None
    ranking: Literal["absolute", "rate"] | None = None
    n: int = Field(default=3, ge=1, le=10, strict=True)
    filters: list[Filter] = Field(default_factory=list, max_length=7)
    checks: list[Literal["orders", "aov", "share"]] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def valid(self):
        validate_filters(self.filters)
        if len(set(self.checks)) != len(self.checks):
            raise ValueError("附加核查不能重复")
        for period in [self.current, self.baseline]:
            if period and (period.end_exclusive - period.start).days > 366:
                raise ValueError("每期不得超过 366 天")
        if self.goal == "compare_declines":
            if not self.baseline or not self.ranking:
                raise ValueError("比较需要两期日期及下降金额/比例口径")
            if max(self.current.start, self.baseline.start) < min(self.current.end_exclusive, self.baseline.end_exclusive):
                raise ValueError("比较区间不得重叠")
        elif self.baseline or self.ranking:
            raise ValueError("单期合计不接受比较基期或下降口径")
        return self

    def check_anchor(self, anchor: date):
        if any(p and p.end_exclusive > anchor.fromordinal(anchor.toordinal() + 1) for p in [self.current, self.baseline]):
            reject(message="日期超出分析基准")


class Lookup(StrictModel):
    action: Literal["lookup_metrics"]
    arguments: dict[Literal["metrics"], list[Literal["gmv", "orders", "aov"]]]


class QueryArgs(StrictModel):
    plan: QueryPlan


class Query(StrictModel):
    action: Literal["query_metrics"]
    arguments: QueryArgs


class CompareArgs(StrictModel):
    baseline_ref: str = Field(min_length=1, max_length=64)
    current_ref: str = Field(min_length=1, max_length=64)
    metrics: list[Literal["gmv", "orders", "aov"]] = Field(min_length=1, max_length=3)


class Compare(StrictModel):
    action: Literal["compare_periods"]
    arguments: CompareArgs


class RankArgs(StrictModel):
    result_ref: str = Field(min_length=1, max_length=64)


class Rank(StrictModel):
    action: Literal["rank_declines"]
    arguments: RankArgs


class Column(StrictModel):
    result_ref: str = Field(min_length=1, max_length=64)
    metric: Literal["gmv", "orders", "aov"]


class SumArgs(StrictModel):
    op: Literal["sum"]
    input: Column
    scope: Literal["returned_groups", "selection", "top_n"]
    selection_ref: str | None = Field(default=None, max_length=64)


class BinaryArgs(StrictModel):
    op: Literal["add", "subtract", "multiply", "divide", "percent_change"]
    left_ref: str = Field(min_length=1, max_length=64)
    right_ref: str = Field(min_length=1, max_length=64)


class Calculate(StrictModel):
    action: Literal["calculate"]
    arguments: Annotated[Union[SumArgs, BinaryArgs], Field(discriminator="op")]


class ClarifyArgs(StrictModel):
    missing_fields: list[Literal["goal", "current", "baseline", "ranking", "n", "filters", "expression"]] = Field(min_length=1, max_length=7)
    message: str = Field(min_length=1, max_length=200)


class Clarify(StrictModel):
    action: Literal["request_clarification"]
    arguments: ClarifyArgs


class Finding(StrictModel):
    kind: str = Field(min_length=1, max_length=40)
    result_ref: str = Field(min_length=1, max_length=64)
    state: str | None = Field(default=None, max_length=2)


class FinishArgs(StrictModel):
    evidence_refs: list[str] = Field(min_length=1, max_length=12)
    findings: list[Finding] = Field(default_factory=list, max_length=9)


class Finish(StrictModel):
    action: Literal["finish"]
    arguments: FinishArgs


Action = Annotated[Union[Lookup, Query, Compare, Rank, Calculate, Clarify, Finish], Field(discriminator="action")]


class Decision(StrictModel):
    spec: Spec | None = None
    action: Action


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def filters_key(filters):
    return sorted(canonical(f.model_dump(mode="json")) for f in filters)


def metric_ids(spec):
    if getattr(spec, 'domain', None) == 'push':
        return spec.required_metrics()
    if spec and (spec.goal == "compare_declines" or "aov" in spec.checks):
        return ["gmv", "orders", "aov"]
    return ["gmv", "orders"] if spec and "orders" in spec.checks else ["gmv"]


def artifact(kind, data, metrics, binding):
    body = {"kind": kind, "data": data, "metrics_used": sorted(set(metrics)), "binding": binding}
    body["result_ref"] = hashlib.sha256(canonical(body).encode()).hexdigest()
    return body


def evidence(ref, artifacts, binding, kind=None):
    item = next((a for a in artifacts if a.get("result_ref") == ref), None)
    if not item or item.get("binding") != binding or kind and item.get("kind") != kind:
        reject(message="证据不存在、类型错误或不属于当前任务")
    return item


def cohort(plan):
    return {"period": plan["time_range"], "filters": sorted(plan["filters"], key=canonical)}


def validate_query(plan, spec, artifacts, binding):
    if not set(plan.metrics) <= METRICS or plan.dimensions not in (["state"], []):
        reject(message="分析只支持 GMV、有效订单量、客单价和州分组")
    if plan.time_range not in [spec.current, spec.baseline]:
        reject(message="不得改变已经确认的时间范围")
    base, actual = filters_key(spec.filters), filters_key(plan.filters)
    if actual != base:
        choices = [a["data"]["states"] for a in artifacts if a["kind"] == "selection" and a["binding"] == binding]
        if spec.goal == "top_n_total":
            for a in artifacts:
                if a["kind"] == "query" and a["binding"] == binding and "gmv" in a["data"]["plan"]["metrics"] and a["data"].get("quality_complete"):
                    if sorted(canonical(f) for f in a["data"]["plan"]["filters"]) == base:
                        choices.append([r["dimensions"]["state"] for r in sorted_gmv(a["data"]["candidate_rows"])[:spec.n]])
        if not any(actual == filters_key(spec.filters + [Filter(field="state", op="in", values=states)]) for states in choices if states):
            reject(message="补查只能在原筛选上收窄到已选州")
    if spec.goal == "compare_declines" and plan.top_n is not None:
        reject(message="全州下降排名之前不能裁剪 Top N")
    if spec.goal == "top_n_total" and plan.top_n is not None:
        if plan.top_n != spec.n or plan.dimensions != ["state"] or [(o.field, o.direction) for o in plan.order_by] != [("gmv", "desc")]:
            reject(message="Top N 查询必须使用已确认的 N 和 GMV 降序")


def decimal(value):
    if value is None:
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            reject()
        return number
    except ArithmeticError:
        reject()


def change(a, b):
    a, b = decimal(a), decimal(b)
    flags = []
    if a is None or b is None:
        return {"baseline": str(a) if a is not None else None, "current": str(b) if b is not None else None,
                "delta": None, "rate": None, "flags": ["missing_value"]}
    if a == 0:
        flags.append("zero_baseline")
    with localcontext() as ctx:
        ctx.prec = 38
        return {"baseline": str(a), "current": str(b), "delta": str(b-a),
                "rate": str((b-a)/a*100) if a else None, "flags": flags}


def rows_by_state(result):
    rows = result["rows"]
    states = [r["dimensions"].get("state") for r in rows]
    if result["plan"]["dimensions"] != ["state"] or len(set(states)) != len(states) or any(s not in catalog().filters["state"].values for s in states):
        reject(message="计算需要互斥且完整标识的州分组")
    return {s: r for s, r in zip(states, rows)}


def sorted_gmv(rows):
    return sorted(rows, key=lambda r: (decimal(r["values"]["gmv"]["value"]) is None,
                                       -(decimal(r["values"]["gmv"]["value"]) or Decimal(0)), r["dimensions"]["state"]))


def compare(args, artifacts, spec, binding):
    a, b = [evidence(ref, artifacts, binding, "query")["data"] for ref in [args.baseline_ref, args.current_ref]]
    if a["plan"]["time_range"] != spec.baseline.model_dump(mode="json") or b["plan"]["time_range"] != spec.current.model_dump(mode="json"):
        reject(message="基期和报告期引用顺序错误")
    if sorted(a["plan"]["filters"], key=canonical) != sorted(b["plan"]["filters"], key=canonical) or a["coverage"] != "complete" or b["coverage"] != "complete":
        reject(message="比较需要同筛选且未裁剪的两期结果")
    if not set(args.metrics) <= set(a["plan"]["metrics"]) & set(b["plan"]["metrics"]):
        reject(message="引用缺少请求比较的指标")
    left, right = rows_by_state(a), rows_by_state(b)
    rows = []
    for state in sorted(left.keys() | right.keys()):
        cells = {}
        for m in args.metrics:
            cells[m] = change(left.get(state, {}).get("values", {}).get(m, {}).get("value"), right.get(state, {}).get("values", {}).get(m, {}).get("value"))
        rows.append({"state": state, "values": cells})
    return artifact("comparison", {"rows": rows, "baseline_ref": args.baseline_ref, "current_ref": args.current_ref,
                                   "filters": a["plan"]["filters"], "metrics": args.metrics}, args.metrics, binding)


def rank(args, artifacts, spec, binding):
    data = evidence(args.result_ref, artifacts, binding, "comparison")["data"]
    if "gmv" not in data["metrics"] or filters_key(spec.filters) != sorted(canonical(f) for f in data["filters"]):
        reject(message="下降排名必须覆盖原始授权筛选范围")
    key = "delta" if spec.ranking == "absolute" else "rate"
    valid = [r for r in data["rows"] if r["values"]["gmv"]["delta"] is not None and decimal(r["values"]["gmv"]["delta"]) < 0 and r["values"]["gmv"][key] is not None]
    selected = sorted(valid, key=lambda r: (decimal(r["values"]["gmv"][key]), r["state"]))[:spec.n]
    return artifact("selection", {"states": [r["state"] for r in selected], "rows": selected, "source_ref": args.result_ref,
                                  "excluded_states": [r["state"] for r in data["rows"] if r["values"]["gmv"][key] is None],
                                  "ranking": spec.ranking, "n": spec.n}, ["gmv"], binding)


def calculate(args, artifacts, spec, binding):
    if isinstance(args, SumArgs):
        if args.input.metric not in {"gmv", "orders"}:
            reject(message="客单价和比例不能求和")
        src = evidence(args.input.result_ref, artifacts, binding, "query")
        data, metric = src["data"], args.input.metric
        if metric not in data["plan"]["metrics"]:
            reject(message="引用缺少所请求的指标")
        rows = list(rows_by_state(data).values())
        if args.scope == "selection":
            selected = evidence(args.selection_ref, artifacts, binding, "selection")["data"]["states"]
            if not set(selected) <= set(rows_by_state(data)):
                reject(message="所选州的数据不完整")
            rows = [r for r in rows if r["dimensions"]["state"] in selected]
        elif args.selection_ref is not None:
            reject()
        elif args.scope == "top_n":
            if spec.goal != "top_n_total" or "gmv" not in data["plan"]["metrics"]:
                reject()
            if data['coverage'] == 'selected_top_n':
                # 查询内核保存的完整候选允许安全重选；不能把任意截断结果当成全集。
                if data.get('requested_plan', {}).get('top_n') != spec.n or 'candidate_rows' not in data:
                    reject(message='TopN 求和需要同一 N 的完整候选')
                rows = list(rows_by_state({**data, 'rows':data['candidate_rows']}).values())
            elif data['coverage'] != 'complete':
                reject()
            rows = sorted_gmv(rows)[:spec.n]
        values = [decimal(r["values"][metric]["value"]) for r in rows]
        # 未知金额不能被跳过，否则会把已知小计冒充完整合计。
        value = sum(values, Decimal(0)) if values and all(v is not None for v in values) else None
        payload = {"op": "sum", "value": str(value) if value is not None else None, "unit": catalog().metrics[metric].unit,
                   "metric": metric, "states": sorted(r["dimensions"]["state"] for r in rows), "cohort": {"period": data["plan"]["time_range"], "filters": sorted([f.model_dump(mode="json") for f in spec.filters], key=canonical)},
                   "source_ref": src["result_ref"], "quality_complete": data.get("quality_complete", True)}
        return artifact("scalar", payload, [metric], binding)
    left, right = [evidence(ref, artifacts, binding, "scalar") for ref in [args.left_ref, args.right_ref]]
    a, b = left["data"], right["data"]
    same_scope = a["states"] == b["states"] and a["cohort"] == b["cohort"]
    unit, metric = a["unit"], a["metric"]
    if args.op in {"add", "subtract", "percent_change"}:
        if a["metric"] != b["metric"] or a["unit"] != b["unit"]:
            reject(message="相加或比较必须使用相同指标和单位")
        if args.op == "add":
            if metric not in {"gmv", "orders"} or a["cohort"] != b["cohort"] or set(a["states"]) & set(b["states"]):
                reject(message="只允许汇总互不重叠且同口径的可加指标")
        elif a["states"] != b["states"] or a["cohort"]["filters"] != b["cohort"]["filters"]:
            reject(message="比较范围不一致")
        if args.op == "percent_change":
            unit, metric = "%", "change_rate"
    elif args.op == "divide":
        ratio_scope = a["cohort"] == b["cohort"] and set(a["states"]) <= set(b["states"])
        if (a["metric"], b["metric"]) not in {("gmv", "orders"), ("gmv", "gmv")} or not (same_scope if b["metric"] == "orders" else ratio_scope):
            reject(message="除法的分子、分母口径或单位不匹配")
        unit, metric = ("BRL/单", "aov") if b["metric"] == "orders" else ("ratio", "ratio")
    elif not same_scope or (a["metric"], b["metric"]) != ("aov", "orders"):
        reject(message="乘法仅允许同范围客单价乘有效订单量")
    else:
        unit, metric = "BRL", "gmv"
    x, y = decimal(a["value"]), decimal(b["value"])
    flags, value = [], None
    with localcontext() as ctx:
        ctx.prec = 38
        if x is not None and y is not None:
            if args.op in {"divide", "percent_change"} and (y == 0 if args.op == "divide" else x == 0):
                flags.append("zero_denominator")
            else:
                value = {"add": lambda: x+y, "subtract": lambda: x-y, "multiply": lambda: x*y,
                         "divide": lambda: x/y, "percent_change": lambda: (y-x)/x*100}[args.op]()
    return artifact("scalar", {"op": args.op, "value": str(value) if value is not None else None, "unit": unit, "metric": metric,
                               "states": sorted(set(a["states"]) | set(b["states"])), "cohort": a["cohort"], "flags": flags,
                               "inputs": [args.left_ref, args.right_ref], "quality_complete": a["quality_complete"] and b["quality_complete"]},
                    left["metrics_used"] + right["metrics_used"] + ([metric] if metric in METRICS else []), binding)


def completed_report(args, data, metrics, artifacts, spec, binding):
    """模型选择陈述类型和顺序；代码核对方向，再从已验证单元格填数字。"""
    choices = []
    names = {"gmv": "GMV", "orders": "有效订单量", "aov": "客单价"}
    if spec.goal == "top_n_total":
        total = data["total"]
        for a in artifacts:
            if a["kind"] == "scalar" and a["data"] == total:
                choices.append({"kind": "gmv_total", "result_ref": a["result_ref"], "state": None,
                                "text": f"所选州 {', '.join(total['states'])} 的 GMV 合计为 {decimal(total['value']):.2f} BRL。"})
    else:
        for row in data["rows"]:
            for metric, cells in row["values"].items():
                delta = decimal(cells["delta"])
                direction, text = ("declined", "下降") if delta < 0 else ("increased", "上升") if delta > 0 else ("unchanged", "持平")
                for a in artifacts:
                    if a["kind"] not in {"comparison", "selection"} or a["kind"] == "selection" and metric != "gmv":
                        continue
                    if any(r["state"] == row["state"] and r["values"].get(metric) == cells for r in a["data"]["rows"]):
                        choices.append({"kind": metric + "_" + direction, "result_ref": a["result_ref"], "state": row["state"],
                                        "text": f"{row['state']} 州{names[metric]}由 {decimal(cells['baseline']):.2f} 变为 {decimal(cells['current']):.2f}，{text}，差值 {delta:.2f}。"})
    selected = []
    for finding in args.findings:
        match = next((c for c in choices if all(c[k] == v for k, v in finding.model_dump().items())), None)
        if match is None:
            selected = []
            break
        selected.append(match)
    fallback = not bool(selected)
    if fallback:
        # 无效因果类型、错误方向或无效引用只影响说明质量，不伪造数字，也不重查。
        seen = set()
        for item in choices:
            key = (item["kind"], item["state"])
            if key not in seen:
                seen.add(key)
                selected.append(item)
    data.update(narrative=selected, narrative_fallback=fallback, ranking=spec.ranking,
                current=spec.current.model_dump(mode="json"), baseline=spec.baseline.model_dump(mode="json") if spec.baseline else None,
                current_days=(spec.current.end_exclusive-spec.current.start).days,
                baseline_days=(spec.baseline.end_exclusive-spec.baseline.start).days if spec.baseline else None)
    if spec.baseline and data["current_days"] != data["baseline_days"]:
        data["summary"] += " 两期天数不同，本次比较原始总量，未换算日均。"
    return artifact("report", data, metrics, binding)


def finish(args, artifacts, spec, binding):
    selected = [evidence(ref, artifacts, binding) for ref in args.evidence_refs]
    if spec.goal == "top_n_total":
        for scalar in selected:
            d = scalar["data"]
            if scalar["kind"] != "scalar" or d["op"] != "sum" or d["metric"] != "gmv" or d["value"] is None or not d["quality_complete"]:
                continue
            source = evidence(d["source_ref"], artifacts, binding, "query")["data"]
            if source["plan"]["time_range"] != spec.current.model_dump(mode="json") or filters_key(spec.filters) != sorted(canonical(f) for f in source["plan"]["filters"]):
                continue
            expected = sorted(r["dimensions"]["state"] for r in sorted_gmv(source["candidate_rows"])[:spec.n])
            if d["states"] != expected:
                continue
            extras = {}
            for check in spec.checks:
                match = next((a for a in selected if a["kind"] == "scalar" and a["data"]["metric"] == ({"share": "ratio"}.get(check, check)) and a["data"]["value"] is not None
                              and a["data"]["states"] == (sorted(r["dimensions"]["state"] for r in source["candidate_rows"]) if check == "share" else expected)
                              and a["data"]["quality_complete"] and a["data"]["cohort"] == d["cohort"]), None)
                if not match:
                    reject("missing_evidence", "所选州附加核查或占比依据不完整")
                extra = match["data"]
                if check == "orders" and extra["op"] != "sum":
                    reject("missing_evidence", "订单合计必须来自所选州的求和")
                if check in {"aov", "share"}:
                    inputs = extra.get("inputs", [])
                    if extra["op"] != "divide" or len(inputs) != 2 or inputs[0] != scalar["result_ref"]:
                        reject("missing_evidence", "比值必须引用本次所选州 GMV 合计作为分子")
                extras[check] = match["data"]
            return completed_report(args, {"complete": True, "goal": spec.goal, "summary": "所选州 GMV 合计，不代表全州总额。",
                                       "total": d, "extras": extras, "rows": [r for r in source["rows"] if r["dimensions"]["state"] in d["states"]], "evidence_refs": args.evidence_refs},
                            [m for a in selected for m in a["metrics_used"]], artifacts, spec, binding)
        reject("missing_evidence", "缺少前 N 州的完整 GMV 合计依据")
    selection = next((a for a in selected if a["kind"] == "selection"), None)
    if not selection:
        reject("missing_evidence", "尚未完成 GMV 下降排名")
    ranks = selection["data"]
    ranked_data = evidence(ranks["source_ref"], artifacts, binding, "comparison")["data"]
    if not any(r["values"]["gmv"]["delta"] is not None for r in ranked_data["rows"]):
        reject("missing_evidence", "没有两期可比较的 GMV 数据")
    comparisons = [a for a in artifacts if a["kind"] == "comparison" and a["binding"] == binding]
    rows = []
    for row in ranks["rows"]:
        values = {"gmv": row["values"]["gmv"]}
        for metric in ["orders", "aov"]:
            for comp in comparisons:
                found = next((r for r in comp["data"]["rows"] if r["state"] == row["state"] and metric in r["values"]), None)
                if found and not found["values"][metric]["flags"]:
                    values[metric] = found["values"][metric]
                    break
            if metric not in values:
                reject("missing_evidence", "所选州的订单量或客单价核查尚不完整")
        rows.append({"state": row["state"], "values": values})
    summary = (f"可比较范围内未发现 GMV 下降州。" if not rows else f"列出 {len(rows)} 个 GMV 下降州" + (f"，少于请求的 {spec.n} 个。" if len(rows) < spec.n else "。"))
    summary += " 指标构成变化不代表因果。"
    if ranks["excluded_states"]:
        summary += " 不可比较州：" + ", ".join(ranks["excluded_states"]) + "。"
    return completed_report(args, {"complete": True, "goal": spec.goal, "rows": rows, "excluded_states": ranks["excluded_states"],
                                  "summary": summary,
                                  "evidence_refs": [a["result_ref"] for a in comparisons] + [selection["result_ref"]]}, metric_ids(spec), artifacts, spec, binding)
