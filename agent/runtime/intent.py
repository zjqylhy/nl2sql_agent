from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MetricID = Literal["gmv", "orders", "aov", "cancellation_rate", "delivery_days", "review_score"]
Period = Literal["today", "yesterday", "this_week", "last_week", "this_month", "last_month", "custom"]
STATES = set("AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO".split())


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: MetricID
    period: Period
    group_by: Literal["", "state"] = ""
    start: date | None = None
    end: date | None = None
    states: list[str] = Field(default_factory=list, max_length=27)

    @model_validator(mode="after")
    def semantic_validation(self):
        if self.period == "custom" and (not self.start or not self.end or self.start >= self.end):
            raise ValueError("显式日期必须为左闭右开的有效区间")
        if self.period != "custom" and (self.start or self.end):
            raise ValueError("相对时间不能同时携带显式日期")
        if any(state not in STATES for state in self.states):
            raise ValueError("地区必须是巴西州代码")
        return self


class Clarification(ValueError):
    def __init__(self, message: str, *, usage: dict | None = None):
        super().__init__(message)
        self.usage = usage


class RuleMiss(Clarification):
    """表达超出规则模板，可交给模型解析；不表示业务条件已经满足。"""


class Unsupported(Clarification):
    """明确超出当前查询范围，不允许通过模型绕过限制。"""


def period_bounds(intent: Intent, anchor: date) -> tuple[date, date]:
    period = intent.period
    if period == "custom":
        return intent.start, intent.end  # type: ignore[return-value]
    if period == "today":
        return anchor, anchor + timedelta(days=1)
    if period == "yesterday":
        return anchor - timedelta(days=1), anchor
    week = anchor - timedelta(days=anchor.weekday())
    if period == "this_week":
        return week, anchor + timedelta(days=1)
    if period == "last_week":
        return week - timedelta(days=7), week
    month = anchor.replace(day=1)
    if period == "this_month":
        return month, anchor + timedelta(days=1)
    return (month - timedelta(days=1)).replace(day=1), month


ALIASES = {
    "gmv": r"(?i)(?<![a-z])GMV(?![a-z])|成交总额|商品交易额",
    "orders": r"订单量|订单数|多少\s*(?:笔|个)?\s*订单",
    "aov": r"客单价",
    "cancellation_rate": r"取消率",
    "delivery_days": r"(?:平均)?(?:交付时长|配送时长|交付天数)",
    "review_score": r"平均评分|评价均分|平均评价",
}
PERIODS = {"today": "今天|今日", "yesterday": "昨天|昨日", "this_week": "本周|这周", "last_week": "上周", "this_month": "本月|这个月", "last_month": "上月|上个月"}
