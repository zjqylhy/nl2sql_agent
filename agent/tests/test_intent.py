from datetime import date
import pytest
from pydantic import ValidationError
from runtime.intent import Intent, Clarification, RuleMiss, Unsupported, period_bounds, comparable_month_windows, rules_intent


def test_calendar_week_month_and_leap_year():
    anchor = date(2024, 3, 1)
    assert period_bounds(Intent(metric="gmv", period="last_month"), anchor) == (date(2024, 2, 1), date(2024, 3, 1))
    assert period_bounds(Intent(metric="gmv", period="this_week"), anchor) == (date(2024, 2, 26), date(2024, 3, 2))
    windows = comparable_month_windows(date(2024, 3, 31))
    assert windows == ((date(2024, 3, 1), date(2024, 3, 30)), (date(2024, 2, 1), date(2024, 3, 1)))


def test_rules_recognize_single_metric():
    intent = rules_intent("本月 GMV 按州统计", {})
    assert intent.metric == "gmv"
    assert intent.group_by == "state"
    assert intent.period == "this_month"


def test_chinese_adjacent_metric_and_state():
    intent = rules_intent("本月GMV只看SP州", {})
    assert intent.metric == "gmv"
    assert intent.states == ["SP"]


@pytest.mark.parametrize("question", ["查询 GMV", "本月 GMV 和客单价", "华东营收为何下降", "上周订单量和本周订单量", "本月 GMV 按渠道", "本月订单量按城市"])
def test_ambiguous_requests_do_not_silently_execute(question):
    with pytest.raises(Clarification):
        rules_intent(question, {})


def test_explicit_choices_cannot_contradict_question():
    with pytest.raises(Clarification):
        rules_intent("本月 GMV", {"metric": "orders"})


@pytest.mark.parametrize("question,overrides", [
    ("本月 GMV 只看 ZZ 州", {}),
    ("本月 GMV 只看圣保罗", {}),
    ("本月 GMV 从 2018-08-03 起", {}),
    ("本月 GMV", {"period": "custom", "start": "2018-08-01", "end": "2018-08-05"}),
    ("2018-08-01 至 2018-08-05 GMV", {"period": "custom", "start": "2018-07-01", "end": "2018-07-05"}),
])
def test_unrecognized_filters_and_conflicting_dates_are_not_ignored(question, overrides):
    with pytest.raises(Clarification):
        rules_intent(question, overrides)


@pytest.mark.parametrize("data", [
    {"metric": "gmv", "period": "custom", "start": "2018-08-20", "end": "2018-08-01"},
    {"metric": "gmv", "period": "this_month", "states": ["CN"]},
    {"metric": "gmv", "period": "this_month", "sql": "DROP TABLE users"},
])
def test_model_payload_has_strict_schema(data):
    with pytest.raises(ValidationError):
        Intent.model_validate(data)


@pytest.mark.parametrize("question", ["本月 GMV，只看圣保罗州", "本月 GMV，只看 SP 和圣保罗州", "本月圣保罗州销售额"])
def test_unknown_expression_is_rule_miss_not_partial_success(question):
    with pytest.raises(RuleMiss):
        rules_intent(question, {})


@pytest.mark.parametrize("question", ["本月 GMV 按支付方式和州统计", "本月 GMV 按未知维度", "本月 GMV 不含 SP 州"])
def test_out_of_scope_is_distinct_from_rule_miss(question):
    with pytest.raises(Unsupported):
        rules_intent(question, {})


def test_supported_group_and_explicit_dates_are_fully_recognized():
    intent = rules_intent("2018-08-01 至 2018-09-01 GMV，按照巴西州分组统计，只看 SP、RJ", {})
    assert intent.group_by == "state" and intent.states == ["SP", "RJ"]


def test_manual_complete_input_needs_no_language_model():
    intent = rules_intent("请查询", {"metric": "gmv", "period": "this_month", "group_by": "state"})
    assert intent.group_by == "state" and intent.metric == "gmv"
    assert rules_intent("查询本月指标", {"metric": "gmv", "period": "this_month"}).metric == "gmv"
    assert rules_intent("指定时间GMV", {"metric": "gmv", "period": "custom", "start": "2015-01-01", "end": "2015-02-01"}).period == "custom"


@pytest.mark.parametrize("question,overrides", [
    ("2018年13月 GMV", {}),
    ("2018年8月 GMV", {"period": "this_month"}),
    ("上个星期本周 GMV", {}),
    ("本月 GMV 从 SP 州到 RJ 州", {}),
    ("本月 GMV 不按州统计", {}),
])
def test_additional_time_conflicts_and_negations_are_not_rule_misses(question, overrides):
    with pytest.raises(Clarification) as error:
        rules_intent(question, overrides)
    assert not isinstance(error.value, RuleMiss)


def test_alternative_time_preserves_constraints_for_model():
    from runtime.intent import question_constraints
    constraints = question_constraints("2018年8月的 GMV，只看圣保罗州", {})
    assert constraints.period == "custom"
    assert (constraints.start, constraints.end) == ("2018-08-01", "2018-09-01")
    assert constraints.requires_filter
