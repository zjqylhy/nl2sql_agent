"""在已有本地/测试部署验证 HTTP 全链路；不用于生产账号。"""
import argparse
import csv
import os
import time
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import httpx

METRICS = ["gmv", "orders", "aov", "cancellation_rate", "delivery_days", "review_score"]
TOLERANCE = {metric: Decimal("0.0001") for metric in METRICS}
TOLERANCE.update(gmv=Decimal(0), orders=Decimal(0))


def golden(csv_dir: Path) -> dict:
    """独立从原始 CSV 计算，避免用运行时 SQL 验证自身。"""
    def read(name):
        with (csv_dir / ("olist_" + name + "_dataset.csv")).open(encoding="utf-8-sig", newline="") as source:
            return list(csv.DictReader(source))
    orders = [r for r in read("orders") if "2018-08-01" <= r["order_purchase_timestamp"] < "2018-09-01"]
    amounts = defaultdict(Decimal)
    for row in read("order_items"):
        amounts[row["order_id"]] += Decimal(row["price"])
    latest = {}
    for row in read("order_reviews"):
        score = int(row["review_score"])
        key = (row["review_creation_date"], row["review_answer_timestamp"], row["review_id"], score)
        if 1 <= score <= 5 and (row["order_id"] not in latest or key > latest[row["order_id"]][0]):
            latest[row["order_id"]] = (key, score)
    valid = [r for r in orders if r["order_status"] not in ("canceled", "unavailable")]
    assert all(r["order_id"] in amounts for r in valid), "CSV 有效订单存在缺失金额"
    gmv = sum(amounts[r["order_id"]] for r in valid)
    deliveries = []
    for row in orders:
        if row["order_status"] != "delivered" or not row["order_delivered_customer_date"]:
            continue
        seconds = (datetime.fromisoformat(row["order_delivered_customer_date"]) - datetime.fromisoformat(row["order_purchase_timestamp"])).total_seconds()
        if seconds >= 0:
            deliveries.append(Decimal(int(seconds)) / Decimal(86400))
    reviews = [Decimal(latest[r["order_id"]][1]) for r in orders if r["order_id"] in latest]
    return {
        "gmv": gmv, "orders": Decimal(len(valid)), "aov": gmv / len(valid),
        "cancellation_rate": Decimal(100 * sum(r["order_status"] == "canceled" for r in orders)) / len(orders),
        "delivery_days": sum(deliveries) / len(deliveries), "review_score": sum(reviews) / len(reviews),
    }


def run(args):
    password = os.environ["SMOKE_PASSWORD"]
    def login(tenant, username):
        client = httpx.Client(base_url=args.base_url, headers={"Origin": args.origin}, timeout=40)
        response = client.post("/api/login", json={"tenant": tenant, "username": username, "password": password})
        assert response.status_code == 200, ("登录失败", response.status_code)
        assert "HttpOnly" in response.headers["set-cookie"]
        return client, response.json()
    admin, _ = login(args.tenant, args.admin)
    operator, person = login(args.tenant, args.operator)
    beta, _ = login(args.other_tenant, args.other_admin)
    expected = golden(args.csv_dir)
    def task(client, question, **fields):
        response = client.post("/api/tasks", json={"question": question, **fields})
        assert response.status_code == 202, response.text
        task_id = response.json()["id"]
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            response = client.get("/api/tasks/" + task_id)
            response.raise_for_status()
            value = response.json()
            if value["status"] not in ("pending", "querying"):
                return value
            time.sleep(0.15)
        raise AssertionError("任务超时")
    results = {}
    historical = None
    try:
        for metric in METRICS:
            value = task(operator, "查询本月指标", metric=metric, period="this_month")
            assert value["status"] == "completed", value
            result = value["result"]
            actual = Decimal(str(result["rows"][0]["value"]))
            # MySQL 整数 AVG 默认保留四位小数；总额与计数要求精确一致。
            assert abs(actual - expected[metric]) <= TOLERANCE[metric], (metric, actual, expected[metric])
            assert result["period"] == {"start": "2018-08-01", "end_exclusive": "2018-09-01"}
            results[metric] = str(actual)
            if metric == "gmv":
                historical = value["id"]
        grouped = task(beta, "本月 GMV 按州统计")
        assert grouped["status"] == "completed" and [r["label"] for r in grouped["result"]["rows"]] == ["SP"]
        for path in ["", "/events"]:
            assert beta.get("/api/tasks/" + historical + path).status_code == 404
        assert beta.post("/api/tasks/" + historical + "/stop", json={}).status_code == 404
        assert operator.get("/api/users").status_code == 403
        assert operator.post("/api/tasks", json={"question": "本月GMV", "tenant_id": args.other_tenant}).status_code == 400
        assert operator.post("/api/tasks", json={"question": "本月GMV"}, headers={"Origin": "https://untrusted.example"}).status_code == 403
        replay = operator.get("/api/tasks/" + historical + "/events", headers={"Last-Event-ID": "1"})
        assert replay.status_code == 200 and "event: done" in replay.text and "\nid: 1\n" not in "\n" + replay.text
        clarification = task(operator, "看看经营情况")
        assert clarification["status"] == "needs_clarification"
        unknown_filter = task(operator, "本月GMV只看ZZ州")
        assert unknown_filter["status"] == "needs_clarification"
        empty = task(operator, "指定时间GMV", metric="gmv", period="custom", start="2015-01-01", end="2015-02-01")
        assert empty["status"] == "completed" and empty["result"]["rows"][0]["value"] is None
        response = admin.patch("/api/users/" + person["id"], json={"enabled": True, "allowed_metrics": [m for m in METRICS if m != "gmv"]})
        response.raise_for_status()
        assert operator.get("/api/tasks/" + historical).status_code == 404
        assert operator.post("/api/tasks", json={"question": "本月GMV", "metric": "gmv"}).status_code == 403
        response = admin.patch("/api/users/" + person["id"], json={"enabled": False, "allowed_metrics": METRICS})
        response.raise_for_status()
        assert operator.get("/api/me").status_code == 401
    finally:
        # 恢复测试账号，供后续界面验收；不改任何生产账号。
        admin.patch("/api/users/" + person["id"], json={"enabled": True, "allowed_metrics": METRICS}).raise_for_status()
        for client in (admin, operator, beta):
            client.close()
    print("六项指标与独立 CSV 结果一致：", results)
    print("跨租户/角色/权限撤销、SSE 续传、澄清、无数据、CSRF 检查通过")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--origin", default="http://localhost:5173")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--admin", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--other-tenant", required=True)
    parser.add_argument("--other-admin", required=True)
    parser.add_argument("--csv-dir", type=Path, default=Path("data/olist"))
    run(parser.parse_args())
