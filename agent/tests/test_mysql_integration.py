"""实际 MySQL 校验导入聚合、受限视图权限及六个指标，使用显式测试夹具。"""
import csv
import os
import secrets

import pymysql
import pytest
from runtime.app import Datasource, connect, execute
from runtime.metrics import compile_query
from tools.import_olist import TABLES, provision


@pytest.fixture(params=[False, True], ids=["provision", "rds-existing"])
def mysql_source(tmp_path, request):
    host = os.getenv("MYSQL_TEST_HOST")
    if not host:
        pytest.skip("MYSQL_TEST_HOST not configured")
    port = int(os.getenv("MYSQL_TEST_PORT", "3306"))
    root = pymysql.connect(host=host, port=port, user=os.getenv("MYSQL_TEST_USER", "root"), password=os.getenv("MYSQL_TEST_PASSWORD", ""), autocommit=True)
    schema = "test_" + secrets.token_hex(8)
    account = "ro_" + secrets.token_hex(8)
    importer = "im_" + secrets.token_hex(8)
    password = secrets.token_hex(16)
    ids = [f"{i:032x}" for i in range(1, 6)]
    customers = [f"{i:032x}" for i in range(11, 16)]
    fixtures = {
        "orders": [
            [ids[0], customers[0], "delivered", "2018-08-02 00:00:00", "2018-08-07 00:00:00"],
            [ids[1], customers[1], "shipped", "2018-08-03 00:00:00", ""],
            [ids[2], customers[2], "canceled", "2018-08-04 00:00:00", ""],
            [ids[3], customers[3], "unavailable", "2018-08-05 00:00:00", ""],
            [ids[4], customers[4], "delivered", "2018-08-06 00:00:00", "2018-08-07 00:00:00"],
        ],
        "customers": [[customers[i], ["SP", "RJ", "SP", "RJ", "SP"][i]] for i in range(5)],
        "items": [[ids[0], 1, 100], [ids[0], 2, 50], [ids[1], 1, 50], [ids[2], 1, 900], [ids[4], 1, 0]],
        "reviews": [[f"{21:032x}", ids[0], 1, "2018-08-08 00:00:00", ""], [f"{22:032x}", ids[0], 4, "2018-08-09 00:00:00", ""], [f"{23:032x}", ids[1], 2, "2018-08-10 00:00:00", ""]],
    }
    for table, (filename, fields, _) in TABLES.items():
        with (tmp_path / filename).open("w", newline="", encoding="utf-8") as output:
            writer = csv.writer(output)
            writer.writerow(fields)
            writer.writerows(fixtures[table])
    try:
        if request.param:
            with root.cursor() as cursor:
                cursor.execute("CREATE DATABASE " + schema)
                cursor.execute("CREATE USER %s@'%%' IDENTIFIED BY %s", (account, password))
                cursor.execute("CREATE USER %s@'%%' IDENTIFIED BY %s", (importer, password))
                cursor.execute(f"GRANT SELECT,INSERT,CREATE,ALTER,INDEX,CREATE VIEW ON {schema}.* TO %s@'%%'", (importer,))
            # 模拟 RDS 普通导入身份，不授予建账号、授权或全局权限。
            with pymysql.connect(host=host, port=port, user=importer, password=password, autocommit=True) as limited:
                provision(limited, tmp_path, schema, account, "", ["SP"], use_existing_database=True, skip_account_provisioning=True)
            with root.cursor() as cursor:
                cursor.execute(f"GRANT SELECT ON {schema}.metric_orders TO %s@'%%'", (account,))
        else:
            provision(root, tmp_path, schema, account, password, ["SP"])
        yield Datasource(host=host, port=port, database=schema, user=account, password=password, analysis_date="2018-08-31")
    finally:
        with root.cursor() as cursor:
            cursor.execute("DROP DATABASE IF EXISTS " + schema)
            cursor.execute("DROP USER IF EXISTS %s@'%%'", (account,))
            cursor.execute("DROP USER IF EXISTS %s@'%%'", (importer,))
        root.close()


def test_mysql_import_deduplicates_and_enforces_row_scope(mysql_source):
    expected = {"gmv": (150, 2), "orders": (2, 2), "aov": (75, 2), "cancellation_rate": (100 / 3, 3), "delivery_days": (3, 2), "review_score": (4, 1)}
    for metric, (value, samples) in expected.items():
        rows = execute(mysql_source, compile_query(metric, False), ["2018-08-01", "2018-09-01"])
        assert float(rows[0]["value"]) == pytest.approx(value, abs=0.0001)
        assert rows[0]["sample_size"] == samples
        assert rows[0]["total_orders"] == 3
    rows = execute(mysql_source, compile_query("gmv", True), ["2018-08-01", "2018-09-01"])
    assert [row["label"] for row in rows] == ["SP"]


def test_mysql_readonly_user_cannot_read_base_or_write(mysql_source):
    connection = connect(mysql_source)
    try:
        with connection.cursor() as cursor:
            with pytest.raises(pymysql.MySQLError):
                cursor.execute("SELECT * FROM orders LIMIT 1")
            with pytest.raises(pymysql.MySQLError):
                cursor.execute("DELETE FROM metric_orders")
    finally:
        connection.close()


def test_mysql_readiness_checks_actual_view_and_tls(mysql_source):
    from runtime.readiness import probe
    assert probe(mysql_source)
    assert not probe(mysql_source.model_copy(update={"ssl_ca": "/nonexistent/ca.pem"}))


def test_mysql_semantic_plan_preserves_grain_and_recalculates_ratios(mysql_source):
    from runtime.plan_parser import plan_constraints
    from runtime.plan import compile_plan, normalize_result
    plan = plan_constraints("本月 GMV 和客单价和平均评分按月和州统计", {}, mysql_source.analysis_date).rules_plan()
    sql, params = compile_plan(plan)
    raw = execute(mysql_source, sql, params)
    result = normalize_result(plan, raw, mysql_source.analysis_date, {"parser":"rules","tokens":0}, sql, params)
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["dimensions"] == {"month":"2018-08", "state":"SP"}
    assert float(row["values"]["gmv"]["value"]) == 150
    assert float(row["values"]["aov"]["value"]) == 75
    assert row["values"]["review_score"]["sample_size"] == 1
    assert row["values"]["aov"]["sample_size"] == 2
    plan = plan_constraints("本月商品金额大于100的订单量", {}, mysql_source.analysis_date).rules_plan()
    sql, params = compile_plan(plan)
    assert execute(mysql_source,sql,params)[0]["m0_value"] == "1"
