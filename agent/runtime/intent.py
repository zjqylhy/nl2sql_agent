import calendar
import re
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
    pass


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


def comparable_month_windows(anchor: date) -> tuple[tuple[date, date], tuple[date, date]]:
    """双方都存在的日期参与环比，避免月长与未完月造成误导。"""
    current = anchor.replace(day=1)
    previous = (current - timedelta(days=1)).replace(day=1)
    days = min(anchor.day, calendar.monthrange(previous.year, previous.month)[1])
    return (current, current + timedelta(days=days)), (previous, previous + timedelta(days=days))


ALIASES = {
    "gmv": r"(?i)(?<![a-z])GMV(?![a-z])|成交总额|商品交易额",
    "orders": r"订单量|订单数|多少.*订单",
    "aov": r"客单价",
    "cancellation_rate": r"取消率",
    "delivery_days": r"交付时长|配送时长|交付天数",
    "review_score": r"平均评分|评价均分|平均评价",
}
PERIODS = {"today": "今天|今日", "yesterday": "昨天|昨日", "this_week": "本周|这周", "last_week": "上周", "this_month": "本月|这个月", "last_month": "上月|上个月"}


def rules_intent(question: str, overrides: dict) -> Intent:
    if re.search(r"为何|为什么|归因|下降|增长|同比|环比|相比|报告|利润|营收|渠道|品类|趋势|按日|每天|按月|华东", question):
        raise Clarification("M1 支持单指标及巴西州分组查询；归因、对比与报告将在 M2/M3 提供。请明确要查询的指标和时间。")
    metrics = [metric for metric, pattern in ALIASES.items() if re.search(pattern, question)]
    metric = overrides.get("metric")
    if not metric:
        if len(metrics) != 1:
            raise Clarification("请选择一个已发布指标：GMV、有效订单量、客单价、取消率、平均交付时长、平均评分。")
        metric = metrics[0]
    elif any(candidate != metric for candidate in metrics):
        raise Clarification("问题中的指标与手动选择不一致，请确认。")
    period = overrides.get("period")
    found = [p for p, pattern in PERIODS.items() if re.search(pattern, question)]
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", question)
    start, end = overrides.get("start"), overrides.get("end")
    if dates and len(dates) != 2:
        raise Clarification("请提供唯一的起止日期（结束日不包含），不要只提供单个日期或多个区间。")
    if (dates or start or end) and found:
        raise Clarification("相对时间与指定日期同时出现，请保留一种时间表达。")
    if dates and (start or end) and dates != [start, end]:
        raise Clarification("问题中的日期与手动选择不一致，请确认。")
    if start or end:
        period = "custom"
    elif len(dates) == 2:
        period, start, end = "custom", dates[0], dates[1]
    elif not period:
        if len(found) != 1:
            raise Clarification("请指定时间：今天、昨日、本周、上周、本月、上月，或明确的起止日期（结束日不包含）。")
        period = found[0]
    if found and period != "custom" and any(p != period for p in found):
        raise Clarification("问题中的时间与选择不一致，请确认。")
    states = re.findall(r"(?<![A-Za-z])([A-Z]{2})(?![A-Za-z])", question)
    if any(state not in STATES for state in states):
        raise Clarification("地区代码无法识别，请使用有效巴西州代码，如 SP 或 RJ。")
    if re.search(r"只看|仅看|限定|筛选|过滤|地区为|州为|位于|来自", question) and not states:
        raise Clarification("筛选范围尚不明确，M1 支持巴西州代码筛选，如只看 SP 州。")
    group = overrides.get("group_by") or ("state" if re.search(r"按.*(?:州|地区)|各州|各地区|分地区", question) else "")
    if re.search(r"北京|上海|中国|城市|卖家|客户", question):
        raise Clarification("M1 地区维度为巴西州，暂不支持所请求的地区或维度。")
    return Intent(metric=metric, period=period, start=start, end=end, group_by=group, states=states)
