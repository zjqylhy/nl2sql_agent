"""查询计划只携带业务 ID 和绑定值，SQL 表达式由语义目录提供。"""
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator
from .catalog import StrictModel, catalog
from .intent import Intent, period_bounds


def validate_filters(filters):
    """AND 筛选不能形成空枚举集合或矛盾金额区间，首轮和追问共用检查。"""
    for name in ("state", "status"):
        possible = set(catalog().filters[name].values)
        for condition in filters:
            if condition.field != name: continue
            if condition.op in ("eq", "in"): possible &= set(condition.values)
            else: possible -= set(condition.values)
        if not possible: raise ValueError("筛选条件相互矛盾，请明确保留哪些条件")
    low, high, excluded = Decimal(0), None, set()
    for condition in filters:
        if condition.field != "amount": continue
        value = Decimal(condition.values[0])
        if condition.op == "ne": excluded.add(value)
        if condition.op in ("eq", "gt", "gte"):
            low = max(low, value + (Decimal("0.01") if condition.op == "gt" else 0))
        if condition.op in ("eq", "lt", "lte"):
            ceiling = value - (Decimal("0.01") if condition.op == "lt" else 0)
            high = ceiling if high is None else min(high, ceiling)
    if high is not None and (low > high or low == high and low in excluded):
        raise ValueError("商品金额筛选相互矛盾，请明确区间")


class TimeRange(StrictModel):
    start: date
    end_exclusive: date

    @model_validator(mode="after")
    def valid_range(self):
        if not self.start < self.end_exclusive or (self.end_exclusive - self.start).days > 3660:
            raise ValueError("时间区间必须有效且不超过十年")
        return self


class Filter(StrictModel):
    field: str
    op: Literal["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte"]
    values: list[str] = Field(min_length=1, max_length=27)

    @model_validator(mode="after")
    def valid_filter(self):
        definition = catalog().filters.get(self.field)
        if definition is None:
            raise ValueError("未知筛选字段")
        if self.op not in ("in", "not_in") and len(self.values) != 1:
            raise ValueError("比较条件只能有一个值")
        if len(set(self.values)) != len(self.values):
            raise ValueError("筛选值不能重复")
        if definition.type == "enum":
            if self.op not in ("eq", "ne", "in", "not_in") or any(v not in definition.values for v in self.values):
                raise ValueError("枚举值或运算符无效")
        elif self.op in ("in", "not_in") or any(not re.fullmatch(r"\d{1,16}(?:\.\d{1,2})?", v) for v in self.values):
            raise ValueError("金额必须是非负且最多两位小数的十进制数")
        return self


class Ordering(StrictModel):
    field: str
    direction: Literal["asc", "desc"]


class QueryPlan(StrictModel):
    version: Literal[1] = 1
    metrics: list[str] = Field(min_length=1, max_length=3)
    dimensions: list[str] = Field(default_factory=list, max_length=2)
    time_range: TimeRange
    filters: list[Filter] = Field(default_factory=list, max_length=8)
    order_by: list[Ordering] = Field(default_factory=list, max_length=3)
    top_n: int | None = Field(default=None, ge=1, le=100, strict=True)

    @model_validator(mode="after")
    def valid_combinations(self):
        c = catalog()
        validate_filters(self.filters)
        if len(set(self.metrics)) != len(self.metrics) or any(m not in c.metrics for m in self.metrics):
            raise ValueError("指标无效或重复")
        if len(set(self.dimensions)) != len(self.dimensions) or any(d not in c.dimensions for d in self.dimensions):
            raise ValueError("维度无效或重复")
        if {"day", "month"}.issubset(self.dimensions):
            raise ValueError("日期与月份不能同时分组")
        if len({o.field for o in self.order_by}) != len(self.order_by) or any(o.field not in self.dimensions + self.metrics for o in self.order_by):
            raise ValueError("排序字段必须是已选择的指标或维度")
        if self.top_n and not self.dimensions:
            raise ValueError("Top N 必须有分组维度")
        return self

    def check_anchor(self, anchor: date):
        if self.time_range.end_exclusive > anchor + timedelta(days=1):
            raise ValueError("时间范围超出分析基准")


def from_intent(intent: Intent, anchor: date) -> QueryPlan:
    start, end = period_bounds(intent, anchor)
    return QueryPlan(metrics=[intent.metric], dimensions=["state"] if intent.group_by else [], time_range=TimeRange(start=start, end_exclusive=end), filters=[Filter(field="state", op="in", values=intent.states)] if intent.states else [])


def compile_plan(plan: QueryPlan, *, dialect="mysql") -> tuple[str, list]:
    c = catalog()
    fields = [f"{c.dimensions[d].expression} AS d_{d}" for d in plan.dimensions]
    if not fields:
        fields.append("'整体' AS label")
    for i, key in enumerate(plan.metrics):
        metric = c.metrics[key]
        fields.extend([f"{metric.formula} AS m{i}_value", f"{metric.sample} AS m{i}_sample"])
    fields.extend(["COUNT(*) AS total_orders", "SUM(CASE WHEN order_status = 'unavailable' THEN 1 ELSE 0 END) AS unavailable_orders"])
    conditions = ["purchased_at >= ?", "purchased_at < ?"]
    params = [str(plan.time_range.start), str(plan.time_range.end_exclusive)]
    operators = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
    for condition in plan.filters:
        definition = c.filters[condition.field]
        if condition.op in ("in", "not_in"):
            conditions.append(f"{definition.column} {'NOT IN' if condition.op == 'not_in' else 'IN'} ({', '.join('?' for _ in condition.values)})")
        else:
            conditions.append(f"{definition.column} {operators[condition.op]} ?")
        params.extend(Decimal(v) if definition.type == "decimal" else v for v in condition.values)
    sql = f"SELECT {', '.join(fields)} FROM metric_orders WHERE {' AND '.join(conditions)}"
    if plan.dimensions:
        sql += " GROUP BY " + ", ".join(c.dimensions[d].expression for d in plan.dimensions)
        order = list(plan.order_by)
        # 同值排名稳定；时间默认升序，联合分组可复现且不改变业务排序。
        for d in sorted(plan.dimensions, key=lambda d: d == "state"):
            if d not in [o.field for o in order]:
                order.append(Ordering(field=d, direction="asc"))
        clauses = []
        for o in order:
            alias = f"d_{o.field}" if o.field in plan.dimensions else f"m{plan.metrics.index(o.field)}_value"
            if o.field in plan.metrics and dialect == "postgres":
                # PG 只允许排序项直接引用输出别名，不能将别名用于 IS NULL 表达式。
                clauses.append(alias + " " + o.direction.upper() + " NULLS LAST")
                continue
            if o.field in plan.metrics:
                clauses.append(alias + " IS NULL ASC")
            clauses.append(alias + " " + o.direction.upper())
        sql += " ORDER BY " + ", ".join(clauses)
    sql += f" LIMIT {plan.top_n or 1001}"
    if dialect == "postgres":
        # 金额保持 NUMERIC 运算，避免通用转译器给除法插入浮点 CAST。
        sql = sql.replace("DATE_FORMAT(purchased_at, '%Y-%m')", "TO_CHAR(purchased_at, 'YYYY-MM')")
    return sql, params


def normalize_result(plan: QueryPlan, raw_rows: list[dict], anchor: date, usage: dict, sql: str, params: list) -> dict:
    c = catalog()
    rows = []
    for raw in raw_rows:
        dimensions = {d: str(raw[f"d_{d}"]) for d in plan.dimensions}
        cells = {}
        total = int(raw.get("total_orders") or 0)
        for i, key in enumerate(plan.metrics):
            sample = int(raw.get(f"m{i}_sample") or 0)
            cells[key] = {"value": raw.get(f"m{i}_value"), "sample_size": sample, "excluded_orders": total - sample}
        rows.append({"dimensions": dimensions, "label": " / ".join(dimensions.values()) or "整体", "values": cells, "total_orders": total, "unavailable_orders": int(raw.get("unavailable_orders") or 0)})
    return {"status": "completed", "result_version": 2, "catalog_version": c.version, "plan": plan.model_dump(mode="json"), "metrics": [{"id": key, "name": c.metrics[key].name, "unit": c.metrics[key].unit, "definition": c.metrics[key].definition, "version": c.metrics[key].version} for key in plan.metrics], "dimensions": [{"id": d, "name": c.dimensions[d].name} for d in plan.dimensions], "analysis_date": str(anchor), "period": {"start": str(plan.time_range.start), "end_exclusive": str(plan.time_range.end_exclusive)}, "rows": rows, "sql": sql, "parameters": [str(v) for v in params], "usage": usage, "truncated": plan.top_n is not None, "message": f"仅显示前 {plan.top_n} 组，不代表全范围合计。" if plan.top_n else "结果按指标口径 v1 计算；未自动填补无样本日期，末期可能不完整。"}
