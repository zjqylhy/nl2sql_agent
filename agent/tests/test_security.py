import pytest
from runtime.security import validate_sql, UnsafeSQL
from runtime.plan import QueryPlan, compile_plan


@pytest.mark.parametrize("sql", [
    "DELETE FROM metric_orders",
    "SELECT COUNT(*) FROM metric_orders WHERE purchased_at>? LIMIT 1000; DROP TABLE orders",
    "SELECT COUNT(*) FROM orders WHERE purchased_at>? LIMIT 1000",
    "SELECT * FROM metric_orders WHERE purchased_at>? LIMIT 1000",
    "SELECT COUNT(*) FROM other_tenant.metric_orders WHERE purchased_at>? LIMIT 1000",
    "SELECT SLEEP(10) FROM metric_orders WHERE purchased_at>? LIMIT 1000",
    "SELECT LOAD_FILE('/etc/passwd') FROM metric_orders WHERE purchased_at>? LIMIT 1000",
    "SELECT COUNT(*) FROM metric_orders WHERE purchased_at>? LIMIT 1001",
    "SELECT COUNT(*) FROM metric_orders WHERE purchased_at>?",
    "SELECT COUNT(*) FROM metric_orders LIMIT 1000",
    "SELECT COUNT(*) FROM metric_orders WHERE purchased_at>? LIMIT 1000 FOR UPDATE",
    "SELECT COUNT(*) FROM metric_orders WHERE purchased_at>? UNION SELECT 1 LIMIT 1000",
    "SELECT COUNT(*) FROM metric_orders WHERE purchased_at>? AND EXISTS (SELECT 1 FROM users) LIMIT 1000",
    "SELECT password FROM metric_orders WHERE purchased_at>? LIMIT 1000",
    "SELECT @@version FROM metric_orders WHERE purchased_at>? LIMIT 1000",
    "SELECT COUNT(*) INTO OUTFILE '/tmp/x' FROM metric_orders WHERE purchased_at>? LIMIT 1000",
    "SELECT /* bypass */ COUNT(*) FROM metric_orders WHERE purchased_at>? LIMIT 1000",
])
def test_attacks_are_rejected(sql):
    with pytest.raises(UnsafeSQL):
        validate_sql(sql, ["2018-01-01"])


def test_parameter_count_is_checked():
    with pytest.raises(UnsafeSQL):
        sql, _ = compile_plan(QueryPlan(metrics=["gmv"], time_range={"start": "2018-01-01", "end_exclusive": "2018-02-01"}))
        validate_sql(sql, ["2018-01-01"], overflow_probe=True)
