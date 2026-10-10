"""扩展表达解析为同一查询计划，已确定条件独立约束模型兜底。"""
import re
from dataclasses import dataclass
from datetime import date, timedelta

from .catalog import catalog
from .intent import ALIASES, Clarification, RuleMiss, Unsupported, Intent, question_constraints, period_bounds, rule_remainder, STATES
from .plan import QueryPlan, TimeRange, Filter, Ordering

STATUS_ALIASES = {"created": "已创建", "approved": "已批准", "invoiced": "已开票", "processing": "处理中", "shipped": "已发货", "delivered": "已交付|已送达", "unavailable": "不可履约", "canceled": "已取消"}
NEW_PATTERN = r"最近|近\s*\d|按月|按日|每天|每月|月份|排名|最高|最低|前\s*\d|升序|降序|金额|订单状态|不含|排除|除了|已取消|已交付|已发货|已送达|不可履约|已批准|已创建|已开票|处理中"


def requires_plan(question: str, overrides: dict) -> bool:
    return bool(re.search(NEW_PATTERN, question)) or overrides.get("group_by") in ("day", "month") or sum(bool(re.search(pattern, question)) for pattern in ALIASES.values()) > 1


def filter_key(condition: Filter):
    # 枚举单值的等于与集合包含等价，模型可使用任一种受控表示。
    op = {"eq": "in", "ne": "not_in"}.get(condition.op, condition.op) if condition.field in ("state", "status") else condition.op
    return condition.field, op, tuple(sorted(condition.values))


@dataclass
class PlanConstraints:
    metrics: list[str]
    dimensions: list[str] | None
    time_range: TimeRange
    filters: list[Filter]
    order_by: list[Ordering]
    top_n: int | None
    requires_state: bool
    filters_complete: bool
    remainder: str

    def validate(self, plan: QueryPlan):
        if self.metrics and set(plan.metrics) != set(self.metrics):
            raise Clarification("解析结果改变了已明确的指标，请确认。")
        if plan.time_range != self.time_range or (self.dimensions is not None and set(plan.dimensions) != set(self.dimensions)):
            raise Clarification("解析结果改变了已明确的时间或分组，请确认。")
        expected, actual = {filter_key(f) for f in self.filters}, {filter_key(f) for f in plan.filters}
        preserved = all(any(filter_key(f)[:2] == filter_key(a)[:2] and (set(f.values).issubset(a.values) if f.field == "state" and self.requires_state else set(f.values) == set(a.values)) for a in plan.filters) for f in self.filters)
        if not preserved or (self.filters_complete and expected != actual):
            raise Clarification("解析结果改变了已明确的筛选，请确认。")
        if not self.filters_complete and any(filter_key(f) not in expected and not (self.requires_state and f.field == "state" and filter_key(f)[1] == "in") for f in plan.filters):
            raise Clarification("解析结果新增了未明确的筛选，请确认。")
        if self.requires_state and not any(f.field == "state" for f in plan.filters):
            raise Clarification("解析结果遗漏地区筛选，请明确州范围。")
        if plan.top_n != self.top_n or any(o not in plan.order_by for o in self.order_by):
            raise Clarification("解析结果改变了已明确的排序或 Top N，请确认。")
        if not self.order_by and plan.order_by and any(o.field not in plan.dimensions or o.direction != "asc" for o in plan.order_by):
            raise Clarification("解析结果新增了未要求的指标排序，请确认。")
        if self.order_by and any(o not in self.order_by and not (o.field in plan.dimensions and o.direction == "asc") for o in plan.order_by):
            raise Clarification("解析结果新增了未要求的指标排序，请确认。")

    def prompt_values(self):
        return {"metrics": self.metrics or None, "dimensions": self.dimensions, "time_range": self.time_range.model_dump(mode="json"), "filters": [f.model_dump() for f in self.filters], "filters_complete": self.filters_complete, "requires_state": self.requires_state, "order_by": [o.model_dump() for o in self.order_by], "top_n": self.top_n}

    def rules_plan(self) -> QueryPlan:
        if not self.metrics:
            if not self.remainder:
                raise Clarification("请指定已发布指标。")
            raise RuleMiss("规则未完整识别指标，请明确指标或配置模型。")
        if self.remainder or self.dimensions is None or not self.filters_complete:
            raise RuleMiss("规则未完整识别条件，请明确条件或配置模型。")
        return QueryPlan(metrics=self.metrics, dimensions=self.dimensions, time_range=self.time_range, filters=self.filters, order_by=self.order_by, top_n=self.top_n)


def plan_constraints(question: str, overrides: dict, anchor: date) -> PlanConstraints:
    remaining = question
    sorting_metrics = [key for key, pattern in ALIASES.items() if re.search(rf"(?:按|以)\s*(?:{pattern.replace('(?i)', '')})\s*(?:升序|降序|排名|从高到低|从低到高)", question, flags=re.I)]
    sort_dimensions = {"month":r"下单月份|月份|月", "day":r"下单日期|日期|日", "state":r"州代码|州|地区"}
    sorting_dimensions = [key for key, pattern in sort_dimensions.items() if re.search(rf"(?:按|以)\s*(?:{pattern})\s*(?:升序|降序)", question)]
    for pattern in sort_dimensions.values():
        remaining = re.sub(rf"(?:按|以)\s*(?:{pattern})\s*(?=升序|降序)", "", remaining)
    for pattern in ALIASES.values():
        remaining = re.sub(rf"(?:按|以)\s*(?:{pattern.replace('(?i)', '')})\s*(?=升序|降序|排名|从高到低|从低到高)", "", remaining, flags=re.I)
    metrics = sorted([key for key, pattern in ALIASES.items() if re.search(pattern, question)], key=lambda key: re.search(ALIASES[key], question).start())
    if len(metrics) > 3:
        raise Clarification("一次最多查询三个指标。")
    manual_metric = overrides.get("metric")
    if manual_metric:
        if manual_metric not in catalog().metrics or (metrics and metrics != [manual_metric]):
            raise Clarification("问题中的指标与手动选择不一致，请确认。")
        metrics = [manual_metric]
    for pattern in ALIASES.values():
        remaining = re.sub(pattern, "", remaining)
    dimensions = []
    for key, pattern in {"month": r"按(?:照)?(?:下单)?月(?:份)?|每月", "day": r"按(?:照)?(?:下单)?日(?:期)?|每天", "state": r"按(?:照)?(?:巴西)?(?:各)?(?:州|地区)(?:分组)?|各州|各地区|分地区|分州"}.items():
        if re.search(pattern, remaining):
            dimensions.append(key)
            remaining = re.sub(pattern, "", remaining)
    # 联合分组中的“按月和州”也消费第二维度，但不把孤立州名当作分组。
    if dimensions and re.search(r"(?:和|及|与)(?:巴西)?州(?:分组|统计)", remaining):
        dimensions.append("state")
        remaining = re.sub(r"(?:和|及|与)(?:巴西)?州(?=分组|统计)", "", remaining)
    manual_group = overrides.get("group_by")
    if manual_group:
        if manual_group not in catalog().dimensions or (dimensions and dimensions != [manual_group]):
            raise Clarification("问题中的维度与手动选择不一致，请确认。")
        dimensions = [manual_group]
    if len(dimensions) > 2 or {"day", "month"}.issubset(dimensions) or (dimensions and re.search("整体|总体|合计", remaining)):
        raise Clarification("请明确唯一的分组组合，日期与月份不能同时分组。")

    filters = []
    amount_pattern = r"(?:订单)?商品金额\s*(大于等于|小于等于|不等于|大于|小于|超过|至少|至多|等于|>=|<=|!=|>|<|=)\s*(-?\d+(?:\.\d+)?)"
    operators = {"大于等于":"gte","至少":"gte",">=":"gte","小于等于":"lte","至多":"lte","<=":"lte","不等于":"ne","!=":"ne","大于":"gt","超过":"gt",">":"gt","小于":"lt","<":"lt","等于":"eq","=":"eq"}
    for match in re.finditer(amount_pattern, remaining):
        try:
            filters.append(Filter(field="amount", op=operators[match[1]], values=[match[2]]))
        except ValueError as exc:
            raise Clarification("商品金额必须为非负且最多两位小数。") from exc
    remaining = re.sub(amount_pattern, "", remaining)
    if re.search("金额|价格|付款|消费|运费", remaining):
        raise Clarification("请明确订单商品金额（不含运费）、比较条件及数值。")

    enum_status = re.search(r"订单状态\s*(不等于|不含|排除|!=|不是|为|是|=)\s*([a-z]+(?:\s*[、,]\s*[a-z]+)*)", remaining)
    if enum_status:
        values = re.split(r"\s*[、,]\s*", enum_status[2])
        try:
            filters.append(Filter(field="status", op="not_in" if enum_status[1] in ("不等于", "不含", "排除", "!=", "不是") else "in", values=values))
        except ValueError as exc:
            raise Clarification("订单状态无法识别，请使用 Olist 状态。") from exc
        remaining = remaining[:enum_status.start()] + remaining[enum_status.end():]
    for key, pattern in STATUS_ALIASES.items():
        combined = rf"(?:订单状态(?:为|是|=)\s*{key}(?![a-z])|(?:只看|仅看|限定|筛选)?(?:{pattern})(?:的)?(?:订单)?)"
        if re.search(combined, remaining):
            filters.append(Filter(field="status", op="eq", values=[key]))
            remaining = re.sub(combined, "", remaining)
    if sum(f.field == "status" for f in filters) > 1:
        raise Clarification("多个状态请明确枚举集合，例如订单状态为 delivered,shipped。")
    if re.search("订单状态|未支付|已支付|未交付|未取消|未完成|已完成|未评价|已评价", remaining):
        raise Clarification("请指定明确的 Olist 订单状态。")

    state_pattern = r"(?<![A-Za-z])([A-Z]{2})(?![A-Za-z])"
    states = list(dict.fromkeys(re.findall(state_pattern, remaining)))
    if any(s not in STATES for s in states):
        raise Clarification("地区代码无法识别，请使用有效巴西州代码。")
    exclusion = bool(re.search(r"不含|排除|除了", remaining))
    if exclusion and re.search(r"只看|仅看|限定|地区为|州为", remaining):
        raise Clarification("包含与排除州筛选请分别明确，本阶段不解析混合州表达。")
    named_state = bool(re.search(r"[\u4e00-\u9fff]{2,}州|圣保罗|里约热内卢", remaining.replace("巴西", "")))
    if states:
        filters.append(Filter(field="state", op="not_in" if exclusion else "in", values=states))
        remaining = re.sub(state_pattern, "", remaining)
    if exclusion and (not states or named_state):
        # 不同州的包含/排除混合需要额外语法，不能猜测操作符作用范围。
        raise Clarification("排除筛选请使用明确州代码，例如不含 SP、RJ 州。")
    if exclusion:
        remaining = re.sub(r"不含|排除|除了", "", remaining)

    top_matches = re.findall(r"(?:取)?前\s*(\d+)\s*(?:名|组)?", remaining)
    if len(top_matches) > 1 or (top_matches and not 1 <= int(top_matches[0]) <= 100):
        raise Clarification("Top N 必须是 1–100 的唯一数量。")
    top_n = int(top_matches[0]) if top_matches else None
    remaining = re.sub(r"(?:取)?前\s*\d+\s*(?:名|组)?", "", remaining)
    descending = bool(re.search("排名|最高|降序|从高到低", remaining))
    ascending = bool(re.search("最低|升序|从低到高", remaining))
    if descending and ascending:
        raise Clarification("排序方向冲突，请明确升序或降序。")
    order_by = []
    if descending or ascending:
        explicit_sort = sorting_metrics + sorting_dimensions
        sort_metric = explicit_sort[0] if len(explicit_sort) == 1 else metrics[0] if len(metrics) == 1 and not explicit_sort else None
        if not sort_metric or not dimensions or len(explicit_sort) > 1 or sort_metric not in metrics + dimensions:
            raise Clarification("排名请明确单个排序指标及分组维度。")
        order_by = [Ordering(field=sort_metric, direction="asc" if ascending else "desc")]
        remaining = re.sub(r"排名|最高|最低|降序|升序|从高到低|从低到高", "", remaining)
    if top_n and (not dimensions or not order_by):
        raise Clarification("前 N 名请明确分组和排名方向。")

    recent = re.findall(r"(?:最近|近)\s*(\d+)\s*个?月", remaining)
    if recent:
        if len(recent) != 1 or not 1 <= int(recent[0]) <= 12 or any(overrides.get(k) for k in ("period", "start", "end")):
            raise Clarification("最近月份须为 1–12，且不能与其他时间选择冲突。")
        remaining = re.sub(r"(?:最近|近)\s*\d+\s*个?月", "", remaining)
        if re.search(r"本月|上月|本周|上周|今天|昨天|昨日|\d{4}[-年]", remaining):
            raise Clarification("多个时间范围冲突，请保留一种。")
        index = anchor.year * 12 + anchor.month - int(recent[0])
        start = date(index // 12, index % 12 + 1, 1)
        time_range = TimeRange(start=start, end_exclusive=anchor + timedelta(days=1))
    else:
        # 已消费的新增条件不影响原有日期冲突检查；占位指标只用于日期解析。
        time_question = remaining + " GMV"
        time_overrides = {k:v for k,v in overrides.items() if k in ("period", "start", "end")}
        constraints = question_constraints(time_question, time_overrides)
        start, end = period_bounds(Intent(metric="gmv", period=constraints.period, start=constraints.start, end=constraints.end), anchor)
        time_range = TimeRange(start=start, end_exclusive=end)
        remaining = re.sub(r"\d{4}年\d{1,2}月|上个(?:星期|礼拜)|这个(?:星期|礼拜)", "", remaining)

    if re.search(r"归因|为什么|为何|同比|环比|相比|增长|下降|对比|比较|变化|差异|报告|品类|卖家|客户|支付方式|渠道|城市|利润|营收|不按|不分|不看|不要|而非|不是|或者|或|从.*州.*到.*州", remaining):
        raise Unsupported("当前支持已发布指标、州与日/月分组及订单筛选；该关联、比较或分析尚未支持。")
    filter_marker = bool(re.search(r"只看|仅看|限定|筛选|过滤|州为|地区为", remaining))
    if filter_marker and not filters and not named_state:
        raise Clarification("请明确筛选字段和值。")
    remainder = rule_remainder(re.sub(r"只看|仅看|限定|筛选|过滤|州为|地区为|订单|分组|趋势|统计|和|及|与", "", remaining))
    unknown_group = bool(re.search(r"按|维度|分布|分别|每州|逐州|各地", remainder))
    return PlanConstraints(metrics, None if unknown_group else dimensions, time_range, filters, order_by, top_n, named_state, not named_state and not (filter_marker and remainder), remainder)
