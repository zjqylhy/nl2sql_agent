from datetime import date
import pytest
from pydantic import ValidationError
from runtime.intent import Intent, Clarification, period_bounds, comparable_month_windows, rules_intent


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
