"""用独立黄金数值检查口径，而不是把编译器表达式复制成期望结果。"""
import sqlite3

import pytest

from runtime.catalog import catalog
from runtime.plan import QueryPlan, compile_plan
from runtime.security import validate_sql


def compile_query(metric, grouped=False, state_count=0):
    plan = QueryPlan(metrics=[metric], dimensions=["state"] if grouped else [],
        time_range={"start": "2018-08-01", "end_exclusive": "2018-09-01"},
        filters=[{"field": "state", "op": "in", "values": ["SP"]}] if state_count else [])
    return compile_plan(plan)[0]


@pytest.fixture
def facts():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE metric_orders(order_status TEXT,item_amount REAL,delivery_days REAL,review_score REAL,customer_state TEXT,purchased_at TEXT)")
    db.executemany("INSERT INTO metric_orders VALUES(?,?,?,?,?,?)", [
        ("delivered", 150, 5, 4, "SP", "2018-08-02"),
        ("shipped", 50, None, 2, "RJ", "2018-08-03"),
        ("canceled", 900, None, None, "SP", "2018-08-04"),
        ("unavailable", None, None, None, "RJ", "2018-08-05"),
        ("delivered", 0, 1, None, "SP", "2018-08-06"),
        ("delivered", 9999, 100, 1, "SP", "2018-09-01"),
    ])
    yield db
    db.close()


@pytest.mark.parametrize("metric,expected,samples", [
    ("gmv", 200, 3), ("orders", 3, 3), ("aov", 200 / 3, 3),
    ("cancellation_rate", 20, 5), ("delivery_days", 3, 2), ("review_score", 3, 2),
])
def test_golden_metrics(facts, metric, expected, samples):
    sql = compile_query(metric, False)
    params = ["2018-08-01", "2018-09-01"]
    validate_sql(sql, params, overflow_probe=True)
    row = facts.execute(sql, params).fetchone()
    assert row["m0_value"] == pytest.approx(expected)
    assert row["m0_sample"] == samples
    assert row["total_orders"] == 5
    assert row["unavailable_orders"] == 1


@pytest.mark.parametrize("metric", catalog().metrics)
def test_empty_window_is_not_zero(facts, metric):
    row = facts.execute(compile_query(metric, False), ["2019-01-01", "2019-02-01"]).fetchone()
    assert row["m0_value"] is None
    assert row["total_orders"] == 0


def test_actual_zero_and_missing_amount(facts):
    row = facts.execute(compile_query("gmv", False), ["2018-08-06", "2018-08-07"]).fetchone()
    assert row["m0_value"] == 0
    facts.execute("INSERT INTO metric_orders VALUES('delivered',NULL,NULL,NULL,'SP','2018-08-07')")
    row = facts.execute(compile_query("gmv", False), ["2018-08-01", "2018-09-01"]).fetchone()
    assert row["m0_value"] is None  # 部分金额缺失，不能输出不完整总额。


def test_state_filter_and_group(facts):
    sql = compile_query("gmv", True, 1)
    params = ["2018-08-01", "2018-09-01", "SP"]
    validate_sql(sql, params, overflow_probe=True)
    rows = facts.execute(sql, params).fetchall()
    assert len(rows) == 1
    assert rows[0]["d_state"] == "SP"
    assert rows[0]["m0_value"] == 150
