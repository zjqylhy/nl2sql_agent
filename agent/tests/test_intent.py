from datetime import date
import pytest
from pydantic import ValidationError
from runtime.intent import Intent, period_bounds


def test_calendar_week_month_and_leap_year():
    anchor = date(2024, 3, 1)
    assert period_bounds(Intent(metric="gmv", period="last_month"), anchor) == (date(2024, 2, 1), date(2024, 3, 1))
    assert period_bounds(Intent(metric="gmv", period="this_week"), anchor) == (date(2024, 2, 26), date(2024, 3, 2))


@pytest.mark.parametrize("data", [
    {"metric": "gmv", "period": "custom", "start": "2018-08-20", "end": "2018-08-01"},
    {"metric": "gmv", "period": "this_month", "states": ["CN"]},
    {"metric": "gmv", "period": "this_month", "sql": "DROP TABLE users"},
])
def test_model_payload_has_strict_schema(data):
    with pytest.raises(ValidationError):
        Intent.model_validate(data)
