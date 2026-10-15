"""完整句式生成计划；未覆盖文字只作提示，不能锁定猜测条件。"""
import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .catalog import catalog
from .intent import ALIASES, PERIODS, Clarification, RuleMiss, Unsupported, Intent, period_bounds
from .plan import QueryPlan, TimeRange, Filter, Ordering
from .query_grammar import OPERATORS, STATUS, Recognition, recognize, state_values, state_negative


def filter_key(condition):
    op = {"eq": "in", "ne": "not_in"}.get(condition.op, condition.op) if condition.field in ("state", "status") else condition.op
    return condition.field, op, tuple(sorted(condition.values))


def time_clause(text, anchor):
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", text)
    if dates:
        return TimeRange(start=dates[0], end_exclusive=dates[1])
    month = re.fullmatch(r"(\d{4})年(\d{1,2})月", text)
    if month:
        first = date(int(month[1]), int(month[2]), 1)
        return TimeRange(start=first, end_exclusive=first + timedelta(days=calendar.monthrange(first.year, first.month)[1]))
    recent = re.fullmatch(r"(?:最近|近)\s*(\d+)\s*个?月", text)
    if recent or re.fullmatch(r"(?:最近|近)半年", text):
        count = int(recent[1]) if recent else 6
        if not 1 <= count <= 12: raise Clarification("最近月份须为 1–12。")
        index = anchor.year * 12 + anchor.month - count
        return TimeRange(start=date(index // 12, index % 12 + 1, 1), end_exclusive=anchor + timedelta(days=1))
    alternatives = {"上个星期": "last_week", "上个礼拜": "last_week", "这个星期": "this_week", "这个礼拜": "this_week"}
    key = alternatives.get(text) or next((k for k, p in PERIODS.items() if re.fullmatch(p, text)), None)
    start, end = period_bounds(Intent(metric="gmv", period=key), anchor)
    return TimeRange(start=start, end_exclusive=end)


def dimension(text):
    return "month" if "月" in text else "day" if "日" in text or "天" in text else "state"


@dataclass
class PlanConstraints:
    metrics: list[str]
    dimensions: list[str] | None
    time_range: TimeRange | None
    filters: list[Filter]
    order_by: list[Ordering]
    top_n: int | None
    requires_state: bool
    filters_complete: bool
    remainder: str
    trusted: set[str] = field(default_factory=set)
    recognition: Recognition = field(default_factory=Recognition)
    state_op: str = "in"

    def validate(self, plan):
        if "metrics" in self.trusted and self.metrics and set(plan.metrics) != set(self.metrics):
            raise Clarification("解析结果改变了已明确的指标，请确认。")
        if "time_range" in self.trusted and self.time_range is not None and plan.time_range != self.time_range:
            raise Clarification("解析结果改变了已明确的时间，请确认。")
        if "dimensions" in self.trusted and self.dimensions is not None and set(plan.dimensions) != set(self.dimensions):
            raise Clarification("解析结果改变了已明确的分组，请确认。")
        if "filters" in self.trusted:
            expected, actual = {filter_key(f) for f in self.filters}, {filter_key(f) for f in plan.filters}
            preserved = all(any(filter_key(f)[:2] == filter_key(a)[:2] and (set(f.values) <= set(a.values)
                if f.field == "state" and self.requires_state else set(f.values) == set(a.values)) for a in plan.filters) for f in self.filters)
            if not preserved or self.filters_complete and expected != actual:
                raise Clarification("解析结果改变了已明确的筛选，请确认。")
            if not self.filters_complete and any(filter_key(f) not in expected and not
                (self.requires_state and f.field == "state" and filter_key(f)[1] == self.state_op) for f in plan.filters):
                raise Clarification("解析结果新增了未明确的筛选，请确认。")
            if self.requires_state and not any(f.field == "state" for f in plan.filters):
                raise Clarification("解析结果遗漏地区筛选，请明确州范围。")
        if "top_n" in self.trusted and plan.top_n != self.top_n:
            raise Clarification("解析结果改变了已明确的 Top N，请确认。")
        if "order_by" in self.trusted and (any(o not in plan.order_by for o in self.order_by) or
            any(o not in self.order_by and not (o.field in plan.dimensions and o.direction == "asc") for o in plan.order_by)):
            raise Clarification("解析结果改变了已明确的排序，请确认。")

    def prompt_values(self):
        values = {"metrics": self.metrics or None, "dimensions": self.dimensions,
            "time_range": self.time_range.model_dump(mode="json") if self.time_range else None,
            "filters": [f.model_dump() for f in self.filters], "order_by": [o.model_dump() for o in self.order_by], "top_n": self.top_n}
        return {"confirmed": {k: v for k, v in values.items() if k in self.trusted}, "hints": values,
            "requires_state": self.requires_state if "filters" in self.trusted else False,
            "filters_complete": self.filters_complete if "filters" in self.trusted else False}

    def rules_plan(self):
        if "metrics" in self.trusted and not self.metrics: raise Clarification("请指定已发布指标。")
        if "time_range" in self.trusted and self.time_range is None: raise Clarification("请指定时间范围。")
        if self.remainder or not self.recognition.complete:
            raise RuleMiss("规则未完整识别表达，请改写或配置模型解析。")
        if not self.metrics: raise Clarification("请指定已发布指标。")
        if self.time_range is None: raise Clarification("请指定时间范围。")
        if self.top_n and (not self.dimensions or not self.order_by):
            raise Clarification("前 N 名请明确分组和排名方向。")
        return QueryPlan(metrics=self.metrics, dimensions=self.dimensions or [], time_range=self.time_range,
            filters=self.filters, order_by=self.order_by, top_n=self.top_n)


def plan_constraints(question, overrides, anchor):
    parsed = recognize(question)
    metrics, dimensions, filters, order_by, windows, top = [], [], [], [], [], []
    requires_state, unknown_group, overall = False, False, False
    directions, sort_fields = [], []
    for clause in parsed.clauses:
        kind, text = clause.kind, clause.text
        # 未覆盖子句可能修饰先前匹配；整句交模型，不能提前根据局部结果判冲突。
        if parsed.unknown: continue
        if kind == "unsupported": raise Unsupported("当前尚未支持该维度、关联、比较或分析。")
        if kind == "invalid_time": raise Clarification("请提供唯一且有效的起止日期，结束日不包含。")
        if kind == "ambiguous_amount": raise Clarification("请明确订单商品金额（不含运费）、比较条件及数值。")
        if kind == "time":
            try: windows.append(time_clause(text, anchor))
            except (ValueError, IndexError) as exc: raise Clarification("请提供有效的时间范围。") from exc
        if kind == "metric":
            metrics.extend(k for k in sorted((k for k, p in ALIASES.items() if re.search(p, text)),
                key=lambda k: re.search(ALIASES[k], text).start()) if k not in metrics)
        if kind == "group":
            dimensions.extend(d for d in [dimension(text)] + (["state"] if "和州" in text else []) if d not in dimensions)
        if kind == "overall": overall = True
        if kind == "unknown_group": unknown_group = True
        if kind in ("state", "named_state"):
            values, named, invalid = state_values(text)
            if invalid: raise Clarification("地区代码无法识别，请使用有效巴西州代码。")
            requires_state |= named
            if values: filters.append(Filter(field="state", op="not_in" if state_negative(text) else "in", values=values))
        if kind == "state_missing": raise Clarification("请明确需要筛选的巴西州。")
        if kind == "clear":
            cleared = "amount" if "金额" in text else "status" if "状态" in text else "state"
            filters = [f for f in filters if f.field != cleared]
            if cleared == "state": requires_state = False
        if kind == "amount":
            match = re.search(r"商品金额\s*(.+?)\s*(-?\d+(?:\.\d+)?)$", text)
            try: filters.append(Filter(field="amount", op=OPERATORS[match[1].strip()], values=[match[2]]))
            except ValueError as exc: raise Clarification("商品金额必须为非负且最多两位小数。") from exc
        if kind == "status":
            match = re.search(r"订单状态\s*(不等于|不包含|不含|排除|!=|不是|为|是|=)\s*(.+)$", text)
            if match:
                values = re.split(r"\s*[,、]\s*", match[2])
                op = "in" if match[1] in ("为", "是", "=") else "not_in"
            else: values, op = [next(v for k, v in STATUS.items() if k in text)], "eq"
            try: filters.append(Filter(field="status", op=op, values=values))
            except ValueError as exc: raise Clarification("订单状态无法识别，请使用 Olist 状态。") from exc
        if kind == "top":
            value = re.search(r"\d+|[一二三四五六七八九十]", text).group()
            top.append(int(value) if value.isdigit() else "一二三四五六七八九十".index(value) + 1)
        if kind in ("sort", "direction"):
            directions.append("asc" if re.search("升序|最低|从低到高", text) else "desc")
            if kind == "sort": sort_fields.append(next((k for k, p in ALIASES.items() if re.search(p, text)), None) or dimension(text))
    if not parsed.unknown:
        if len(windows) > 1: raise Clarification("多个时间范围冲突，请保留一种。")
        if len(metrics) > 3: raise Clarification("一次最多查询三个指标。")
        if len(dimensions) > 2 or {"day", "month"} <= set(dimensions) or overall and dimensions:
            raise Clarification("请明确分组组合，日期与月份不能同时分组。")
        if len(top) > 1 or any(not 1 <= n <= 100 for n in top): raise Clarification("Top N 必须是 1–100 的唯一数量。")
        if len(set(directions)) > 1: raise Clarification("排序方向冲突。")
        if sum(f.field == "state" for f in filters) > 1 or sum(f.field == "status" for f in filters) > 1:
            raise Clarification("同一字段有多种筛选，请明确包含、排除及枚举集合。")
    window = windows[0] if windows else None
    context_required = any(c.kind in ("metric_change", "clear_rank", "prefix") for c in parsed.clauses)
    trusted = {"metrics", "dimensions", "filters", "order_by", "top_n", "time_range"} if not parsed.unknown and not context_required else set()
    if any(c.kind == "unknown_metric" for c in parsed.clauses): trusted.discard("metrics")
    if unknown_group: trusted.discard("dimensions")
    for name, field_name in (("metric", "metrics"), ("group_by", "dimensions")):
        value = overrides.get(name)
        if value:
            options = catalog().metrics if name == "metric" else catalog().dimensions
            current = metrics if name == "metric" else dimensions
            if value not in options or current and current != [value]: raise Clarification("问题中的指标或维度与手动选择不一致，请确认。")
            if name == "metric": metrics = [value]
            else: dimensions = [value]
            trusted.add(field_name)
    if any(overrides.get(k) for k in ("period", "start", "end")):
        try:
            start, end = period_bounds(Intent(metric="gmv", period=overrides.get("period") or "custom",
                start=overrides.get("start") or None, end=overrides.get("end") or None), anchor)
            manual = TimeRange(start=start, end_exclusive=end)
        except ValueError as exc: raise Clarification("手动时间范围无效，请确认。") from exc
        if window and window != manual: raise Clarification("问题中的时间与手动选择不一致，请确认。")
        window = manual
        trusted.add("time_range")
    if directions:
        selected = list(dict.fromkeys(sort_fields)) or (metrics if len(metrics) == 1 else [])
        if not dimensions and len(selected) == 1 and selected[0] in catalog().dimensions:
            dimensions = [selected[0]]
        if len(selected) != 1 or not dimensions: raise Clarification("排名请明确单个排序指标及分组维度。")
        order_by = [Ordering(field=selected[0], direction=directions[0])]
    state_op = "not_in" if any(c.kind == "state" and state_negative(c.text) for c in parsed.clauses) else "in"
    return PlanConstraints(metrics, None if unknown_group else dimensions, window, filters, order_by,
        top[0] if top else None, requires_state, not requires_state,
        "unknown_clause" if parsed.unknown or context_required else "", trusted, parsed, state_op)
