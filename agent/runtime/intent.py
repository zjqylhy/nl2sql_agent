import calendar
import re
from dataclasses import dataclass, field
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


def comparable_month_windows(anchor: date) -> tuple[tuple[date, date], tuple[date, date]]:
    """双方都存在的日期参与环比，避免月长与未完月造成误导。"""
    current = anchor.replace(day=1)
    previous = (current - timedelta(days=1)).replace(day=1)
    days = min(anchor.day, calendar.monthrange(previous.year, previous.month)[1])
    return (current, current + timedelta(days=days)), (previous, previous + timedelta(days=days))


ALIASES = {
    "gmv": r"(?i)(?<![a-z])GMV(?![a-z])|成交总额|商品交易额",
    "orders": r"订单量|订单数|多少\s*(?:笔|个)?\s*订单",
    "aov": r"客单价",
    "cancellation_rate": r"取消率",
    "delivery_days": r"交付时长|配送时长|交付天数",
    "review_score": r"平均评分|评价均分|平均评价",
}
PERIODS = {"today": "今天|今日", "yesterday": "昨天|昨日", "this_week": "本周|这周", "last_week": "上周", "this_month": "本月|这个月", "last_month": "上月|上个月"}
STATE_PATTERN = r"(?<![A-Za-z])([A-Z]{2})(?![A-Za-z])"
GROUP_PATTERN = r"按(?:照)?(?:巴西)?(?:各)?(?:州|地区)(?:分组)?|各州|各地区|分地区|分州"
FILTER_PATTERN = r"只看|仅看|限定|筛选|过滤|地区为|州为|位于|来自"
UNSUPPORTED_PATTERN = r"为何|为什么|归因|下降|增长|同比|环比|相比|变化|差异|对比|比较|最高|最低|排名|报告|利润|营收|渠道|品类|趋势|按日|每天|按月|华东|北京|上海|中国|城市|卖家|客户|支付方式|门店|物流|订单状态|已(?:支付|交付|取消|完成|评价)|未(?:支付|交付|取消|完成|评价)|金额[大小]|价格|大于|小于|超过|不足|不含|排除|除了|不要|而非|不是|不按|不分|不看|从.*州.*到.*州"


@dataclass
class IntentConstraints:
    metric: str | None = None
    period: str | None = None
    start: str | None = None
    end: str | None = None
    group_by: str | None = None
    states: list[str] = field(default_factory=list)
    requires_filter: bool = False
    states_complete: bool = True

    def validate(self, intent: Intent):
        # 模型只能补齐未识别的表达，不能覆盖手动参数或明确识别的业务条件。
        for name in ("metric", "period", "group_by", "start", "end"):
            expected = getattr(self, name)
            actual = getattr(intent, name)
            if expected is not None and str(actual) != expected:
                raise Clarification("解析结果与已明确的指标、时间或维度不一致，请确认。")
        if not set(self.states).issubset(intent.states) or (self.requires_filter and not intent.states):
            raise Clarification("解析结果未保留地区筛选，请明确巴西州范围。")
        if self.states_complete and set(self.states) != set(intent.states):
            raise Clarification("解析结果改变了已明确的地区范围，请确认。")

    def prompt_values(self) -> dict:
        # 提示只使用意图字段，避免模型把内部判定标志复制成响应字段。
        values = {name: getattr(self, name) for name in ("metric", "period", "start", "end", "group_by") if getattr(self, name) is not None}
        if self.states_complete or self.states:
            values["states"] = self.states
        return values


def question_constraints(question: str, overrides: dict) -> IntentConstraints:
    if re.search(UNSUPPORTED_PATTERN, question):
        raise Unsupported("M1 支持单指标及巴西州分组/筛选；暂不支持所请求的维度、条件或分析能力。")
    if re.search(r"按(?!(?:照)?(?:巴西)?(?:各)?(?:州|地区))[^\s，,。；;？?]+", question):
        raise Unsupported("M1 分组维度为巴西州，请使用按州或按地区统计。")
    metrics = [metric for metric, pattern in ALIASES.items() if re.search(pattern, question)]
    metric = overrides.get("metric")
    if len(metrics) > 1:
        raise Clarification("请一次查询一个指标，不要同时指定多个指标。")
    if metric and (metric not in ALIASES or any(candidate != metric for candidate in metrics)):
        raise Clarification("问题中的指标与手动选择不一致，请确认。")
    metric = metric or (metrics[0] if metrics else None)
    period = overrides.get("period")
    found = [p for p, pattern in PERIODS.items() if re.search(pattern, question)]
    if len(found) > 1:
        raise Clarification("问题包含多个时间范围，请一次指定一个范围。")
    if period and period not in (*PERIODS, "custom"):
        raise Clarification("时间选择无效，请确认。")
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", question)
    named_months = re.findall(r"(\d{4})年(\d{1,2})月(?!\d)", question)
    alternative_periods = [p for p, pattern in {"last_week": r"上个(?:星期|礼拜)", "this_week": r"这个(?:星期|礼拜)"}.items() if re.search(pattern, question)]
    if named_months:
        if len(named_months) != 1 or dates or found or alternative_periods:
            raise Clarification("问题包含多个时间表达，请保留唯一的时间范围。")
        try:
            year, month = map(int, named_months[0])
            first = date(year, month, 1)
            after = first + timedelta(days=calendar.monthrange(year, month)[1])
            dates = [str(first), str(after)]
        except (ValueError, OverflowError) as exc:
            raise Clarification("指定月份无效，请确认年月。") from exc
    if alternative_periods:
        found = list(dict.fromkeys([*found, *alternative_periods]))
        if len(found) != 1:
            raise Clarification("问题包含多个时间范围，请一次指定一个范围。")
    start, end = overrides.get("start"), overrides.get("end")
    if dates and len(dates) != 2:
        raise Clarification("请提供唯一的起止日期（结束日不包含），不要只提供单个日期或多个区间。")
    if (dates or start or end) and found:
        raise Clarification("相对时间与指定日期同时出现，请保留一种时间表达。")
    if dates and (start or end) and dates != [start, end]:
        raise Clarification("问题中的日期与手动选择不一致，请确认。")
    if start or end:
        if period and period != "custom":
            raise Clarification("相对时间与指定日期不能同时选择。")
        period = "custom"
    elif len(dates) == 2:
        if period and period != "custom":
            raise Clarification("相对时间与指定日期不能同时选择。")
        period, start, end = "custom", dates[0], dates[1]
    elif not period:
        if not found:
            raise Clarification("请指定时间：今天、昨日、本周、上周、本月、上月，或明确的起止日期（结束日不包含）。")
        period = found[0]
    if found and period != "custom" and any(p != period for p in found):
        raise Clarification("问题中的时间与选择不一致，请确认。")
    if period == "custom":
        try:
            if not start or not end or date.fromisoformat(start) >= date.fromisoformat(end):
                raise ValueError()
        except ValueError as exc:
            raise Clarification("请提供有效的起止日期，结束日不包含且必须晚于开始日。") from exc
    states = list(dict.fromkeys(re.findall(STATE_PATTERN, question)))
    if any(state not in STATES for state in states):
        raise Clarification("地区代码无法识别，请使用有效巴西州代码，如 SP 或 RJ。")
    filters = list(re.finditer(FILTER_PATTERN, question))
    if filters and any(not question[match.end():].strip(" ，,。；;？?！!") for match in filters):
        raise Clarification("请明确需要筛选的巴西州。")
    group = overrides.get("group_by")
    if group and group != "state":
        raise Unsupported("M1 分组维度为巴西州。")
    if re.search(r"整体|总体|合计", question):
        if group or re.search(GROUP_PATTERN, question):
            raise Clarification("整体与州分组同时出现，请确认分组维度。")
        group = ""
    elif re.search(GROUP_PATTERN, question):
        group = "state"
    elif not group and not re.search(r"维度|分组|分布|分别|各地|不同州|逐州|每个州|每州|州别", question):
        group = ""
    remainder = unconsumed_terms(question)
    named_state = bool(re.search(r"[\u4e00-\u9fff]{2,}州|圣保罗|里约热内卢", remainder.replace("巴西", "")))
    filter_targets = [re.split(r"[，,。；;？?！!]", question[match.end():], maxsplit=1)[0] for match in filters]
    unknown_filter = any(rule_remainder(target) for target in filter_targets)
    return IntentConstraints(metric, period, start, end, group, states, bool(filters) or named_state, not (named_state or unknown_filter))


def unconsumed_terms(question: str) -> str:
    remainder = question
    for pattern in [*ALIASES.values(), *PERIODS.values(), r"\d{4}-\d{2}-\d{2}", GROUP_PATTERN, STATE_PATTERN, FILTER_PATTERN]:
        remainder = re.sub(pattern, "", remainder)
    return remainder


def rule_remainder(question: str) -> str:
    # 完整消费受限表达，未知维度、筛选和否定条件不能静默变成整体查询。
    remainder = unconsumed_terms(question)
    remainder = re.sub(r"帮我|给我|请问|请|查询|查一下|查看|统计|计算|有效|巴西|整体|总体|合计|是多少|有多少|多少|如何|怎么样|分组|指定时间|指标|的|为|是|和|与|及|或|州|从|到|至|起|止|不包含|结束日", "", remainder)
    return re.sub(r"[\s，,。；;？?！!、：:（）()]+", "", remainder)


def rules_intent(question: str, overrides: dict, constraints: IntentConstraints | None = None) -> Intent:
    constraints = constraints or question_constraints(question, overrides)
    remainder = rule_remainder(question)
    if not constraints.metric and not remainder:
        raise Clarification("请选择一个已发布指标：GMV、有效订单量、客单价、取消率、平均交付时长、平均评分。")
    if not constraints.metric or not constraints.period or remainder or (constraints.requires_filter and not constraints.states):
        raise RuleMiss("规则尚未完整识别表达，请明确指标、时间及巴西州代码，或配置模型解析。")
    return Intent(metric=constraints.metric, period=constraints.period, start=constraints.start, end=constraints.end, group_by=constraints.group_by or "", states=constraints.states)
