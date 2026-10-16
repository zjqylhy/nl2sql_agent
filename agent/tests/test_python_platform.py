"""新平台真实 PG 集成：API/Worker、授权、图恢复及独立 CSV 数字核对。"""
import csv
import hashlib
import os
from pathlib import Path
import secrets
from uuid import uuid4

import bcrypt
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from agent.platform_app.api import create_app
from agent.platform_app.database import Database, migrate
from agent.platform_app.policy import Policy
from agent.platform_app.repository import Repository, binding
from agent.platform_app.worker import execute
from agent.runtime import app as runtime
from agent.runtime.analysis import Spec
from agent.runtime.analysis_fixed import next_action

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "http://testserver"
POLICY = Policy(currency="test", price_version="fixture", model="fixture", tokenizer="byte_bpe_upper_bound", input_price=1000000, output_price=1000000, max_cost=1000000)


@pytest.fixture
def platform(monkeypatch):
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("isolated TEST_DATABASE_URL required")
    name = "test_python_" + secrets.token_hex(6)
    role = name + "_ro"
    password = secrets.token_hex(16)
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    isolated = make_conninfo(dsn, dbname=name)
    # Alembic 接收 URI；测试保留原 DSN 主机信息，替换 database。
    params = conninfo_to_dict(isolated)
    from urllib.parse import quote
    uri = "postgresql://{}:{}@{}:{}/{}".format(quote(params.get("user", "")), quote(params.get("password", "")), params.get("host", "127.0.0.1"), params.get("port", "5432"), name)
    migrate(uri)
    migrate(uri)  # 再执行不重复 DDL。
    database = Database(uri)
    repo = Repository(database)
    with database.transaction() as conn:
        conn.execute("INSERT INTO tenants VALUES('demo','fixture'),('other','other')")
        for tenant, username, user_id, user_role in [("demo", "admin", "admin", "admin"), ("demo", "ops", "ops", "ops"), ("other", "admin", "other", "admin")]:
            conn.execute("INSERT INTO users(id,tenant_id,username,password_hash,role,allowed_metrics) VALUES(%s,%s,%s,%s,%s,%s)", (user_id, tenant, username, bcrypt.hashpw(b"fixture-password", bcrypt.gensalt(rounds=4)), user_role, list(runtime.catalog().metrics)))
    with psycopg.connect(isolated) as conn:
        conn.execute("CREATE SCHEMA analytics_fixture")
        conn.execute("CREATE TABLE analytics_fixture.order_facts(order_id text PRIMARY KEY,purchased_at timestamp,customer_state text,order_status text,item_amount numeric(18,2),delivery_days numeric,review_score integer)")
        with (ROOT / "evals/analysis/orders.csv").open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                conn.execute("INSERT INTO analytics_fixture.order_facts VALUES(%s,%s,%s,%s,%s,NULL,NULL)", (row["order_id"], row["date"], row["state"], row["status"], row["amount"] or None))
        conn.execute("CREATE VIEW analytics_fixture.metric_orders AS SELECT * FROM analytics_fixture.order_facts")
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA analytics_fixture TO {}").format(sql.Identifier(role)))
        conn.execute(sql.SQL("GRANT SELECT ON analytics_fixture.metric_orders TO {}").format(sql.Identifier(role)))
    source = runtime.Datasource(driver="postgresql", host=params.get("host", "127.0.0.1"), port=int(params.get("port", 5432)), database=name, user=role, password=password,
        schema_name="analytics_fixture", analysis_date="2018-08-31", dataset_revision=hashlib.sha256((ROOT/"evals/analysis/orders.csv").read_bytes()).hexdigest())
    monkeypatch.setattr(runtime, "sources", lambda: {"demo:admin": source, "demo:ops": source, "other:admin": source})
    monkeypatch.setenv("LLM_MODEL", "fixture")
    monkeypatch.setenv("LLM_BASE_URL", "https://fixture.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-only-no-network")
    monkeypatch.delenv("CHECKPOINT_DATABASE_URL", raising=False)
    with TestClient(create_app(database, POLICY, origin=ORIGIN)) as client:
        client.headers["Origin"] = ORIGIN
        assert client.post("/api/login", json={"tenant": "demo", "username": "admin", "password": "fixture-password"}).status_code == 200
        try:
            yield client, repo, source
        finally:
            database.close()
            with psycopg.connect(dsn, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def spec(goal="top_n_total"):
    value = {"goal": goal, "current": {"start": "2018-07-01", "end_exclusive": "2018-08-01"}, "n": 3, "checks": ["orders", "aov", "share"] if goal == "top_n_total" else [], "filters": []}
    if goal == "compare_declines":
        value.update(baseline={"start": "2018-06-01", "end_exclusive": "2018-07-01"}, ranking="absolute")
    return Spec.model_validate(value).model_dump(mode="json")


def deterministic_model(monkeypatch, goal="top_n_total"):
    from agent.platform_app import graph
    calls = []
    def decide(request):
        calls.append(request)
        current = request.spec or Spec.model_validate(spec(goal))
        return {"status": "completed", "model_sent": True, "usage": {"prompt_tokens": 100, "completion_tokens": 50}, "decision": next_action(current, request.artifacts)}
    monkeypatch.setattr(graph, "decide", decide)
    return calls


def run_once(repo):
    task = repo.claim(True)
    assert task
    execute(repo, task)
    return task


@pytest.mark.parametrize("goal", ["top_n_total", "compare_declines"])
def test_graph_real_query_math_and_independent_csv(platform, monkeypatch, goal):
    client, repo, _ = platform
    calls = deterministic_model(monkeypatch, goal)
    request_id = str(uuid4())
    response = client.post("/api/analysis-runs", json={"question": "独立数据工具验证", "request_id": request_id})
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    assert client.post("/api/analysis-runs", json={"question": "独立数据工具验证", "request_id": request_id}).json()["id"] == run_id
    run_once(repo)
    run = client.get("/api/analysis-runs/"+run_id).json()
    assert run["status"] == "completed", run
    report = next(a["data"] for a in run["artifacts"] if a["kind"] == "report")
    from evals.analysis.oracle import expected, score
    assert score(report, expected(ROOT/"evals/analysis/orders.csv", spec(goal)))
    assert run["state"]["model_calls"] == len(calls)
    assert run["state"]["sql_calls"] == (2 if goal == "compare_declines" else 1)
    assert run["usage"]["known_tokens"] == 150*len(calls)
    assert repo.claim(True) is None
    stream = client.get(f"/api/tasks/{run_id}/events", headers={"Last-Event-ID": "1"})
    assert stream.status_code == 200 and "event: done" in stream.text and "id: 1\n" not in stream.text


def test_single_query_and_multiturn_contract(platform):
    client, repo, _ = platform
    conversation = client.post("/api/conversations", json={})
    assert conversation.status_code == 201, conversation.text
    c = conversation.json()
    body = {"question": "2018年7月 GMV 按州统计", "request_id": str(uuid4()), "expected_version": c["version"]}
    response = client.post(f"/api/conversations/{c['id']}/turns", json=body)
    assert response.status_code == 202, response.text
    run_once(repo)
    task = client.get("/api/tasks/"+response.json()["id"]).json()
    assert task["status"] == "completed", task
    detail = client.get("/api/conversations/"+c["id"]).json()
    c = detail["conversation"]
    assert c["plan"]["dimensions"] == ["state"] and c["active_task_id"] is None
    response = client.post(f"/api/conversations/{c['id']}/turns", json={"question": "改成按月", "request_id": str(uuid4()), "expected_version": c["version"]})
    assert response.status_code == 202, response.text
    run_once(repo)
    task = client.get("/api/tasks/"+response.json()["id"]).json()
    assert task["status"] == "completed", task
    assert task["result"]["plan"]["dimensions"] == ["month"]
    assert client.get("/api/operations/summary").status_code == 200


def test_api_auth_origin_tenant_and_privacy(platform, monkeypatch):
    client, repo, _ = platform
    deterministic_model(monkeypatch)
    assert client.post("/api/tasks", json={"question": "GMV"}, headers={"Origin": "https://evil.invalid"}).status_code == 403
    assert client.post("/api/tasks", json={"question": "GMV", "tenant_id": "other"}).status_code == 400
    run_id = client.post("/api/analysis-runs", json={"question": "GMV", "request_id": str(uuid4())}).json()["id"]
    run_once(repo)
    assert client.post("/api/login", json={"tenant": "demo", "username": "ops", "password": "fixture-password"}).status_code == 200
    assert client.get("/api/analysis-runs/"+run_id).status_code == 404
    assert client.get("/api/users").status_code == 403
    assert client.post("/api/login", json={"tenant": "other", "username": "admin", "password": "fixture-password"}).status_code == 200
    assert client.get("/api/tasks/"+run_id).status_code == 404


def test_clarification_resume_idempotency_and_budget(platform, monkeypatch):
    client, repo, _ = platform
    from agent.platform_app import graph
    original_calls = deterministic_model(monkeypatch)
    normal = graph.decide
    def decide(request):
        if not request.answers:
            return {"status": "completed", "model_sent": True, "usage": {"prompt_tokens": 100, "completion_tokens": 50}, "decision": {"spec": None, "action": {"action": "request_clarification", "arguments": {"missing_fields": ["current"], "message": "请补充月份"}}}}
        return normal(request)
    monkeypatch.setattr(graph, "decide", decide)
    run_id = client.post("/api/analysis-runs", json={"question": "Top N 合计", "request_id": str(uuid4())}).json()["id"]
    run_once(repo)
    run = client.get("/api/analysis-runs/"+run_id).json()
    assert run["status"] == "needs_clarification", run
    assert repo.claim(True) is None
    body = {"request_id": str(uuid4()), "expected_version": run["version"], "answer": "2018 年 7 月，前 3 州，核查订单、客单价和占比"}
    first = client.post(f"/api/analysis-runs/{run_id}/answers", json=body)
    assert first.status_code == 200, first.text
    assert client.post(f"/api/analysis-runs/{run_id}/answers", json=body).status_code == 200
    assert client.post(f"/api/analysis-runs/{run_id}/answers", json={**body, "answer": "不同内容"}).status_code == 409
    run_once(repo)
    run = client.get("/api/analysis-runs/"+run_id).json()
    assert run["status"] == "completed", run
    assert run["state"]["model_calls"] == len(original_calls)+1
    assert len(run["state"]["answers"]) == 1 and run["state"]["sql_calls"] == 1


def test_receipt_saved_before_checkpoint_failure_replays_without_query(platform, monkeypatch):
    client, repo, _ = platform
    deterministic_model(monkeypatch)
    from agent.platform_app.ledger import Ledger
    original_complete = Ledger.complete
    crashed = []
    def complete(self, op_id, output):
        result = original_complete(self, op_id, output)
        if output.get("artifact", {}).get("kind") == "query" and not crashed:
            crashed.append(True)
            raise RuntimeError("injected crash after durable receipt before graph write")
        return result
    monkeypatch.setattr(Ledger, "complete", complete)
    run_id = client.post("/api/analysis-runs", json={"question": "Top N 合计", "request_id": str(uuid4())}).json()["id"]
    run_once(repo)
    assert client.get("/api/analysis-runs/"+run_id).json()["status"] == "querying"
    with repo.db.transaction() as conn:
        conn.execute("UPDATE tasks SET lease_until=now()-interval '1 second' WHERE id=%s", (run_id,))
    run_once(repo)
    run = client.get("/api/analysis-runs/"+run_id).json()
    assert run["status"] == "completed", run
    assert run["state"]["sql_calls"] == 1
    assert len([s for s in run["steps"] if s["action"] == "query_metrics"]) == 1


@pytest.mark.parametrize("unknown_after_crash", [False, True])
def test_unknown_model_never_resent(platform, monkeypatch, unknown_after_crash):
    client, repo, _ = platform
    from agent.platform_app import graph
    calls = []
    def unknown(request):
        calls.append(1)
        if unknown_after_crash:
            raise RuntimeError("injected model result lost")
        return {"status": "error", "error_code": "unknown_execution", "model_sent": True, "message": "模型调用结果未知"}
    monkeypatch.setattr(graph, "decide", unknown)
    run_id = client.post("/api/analysis-runs", json={"question": "Top N 合计", "request_id": str(uuid4())}).json()["id"]
    run_once(repo)
    if unknown_after_crash:
        with repo.db.transaction() as conn:
            conn.execute("UPDATE tasks SET lease_until=now()-interval '1 second' WHERE id=%s", (run_id,))
        run_once(repo)
    run = client.get("/api/analysis-runs/"+run_id).json()
    assert run["status"] == "failed", run
    assert calls == [1] and run["state"]["sql_calls"] == 0
    assert run["usage"]["unknown_calls"] == 1 and run["state"]["tokens"] == 7024
    assert client.post(f"/api/analysis-runs/{run_id}/resume", json={}).status_code == 409


@pytest.mark.parametrize("always_timeout", [False, True])
def test_sql_timeout_backoff_one_global_retry(platform, monkeypatch, always_timeout):
    client, repo, _ = platform
    deterministic_model(monkeypatch)
    execute_sql = runtime.execute
    calls = []
    def execute_fixture(*args, **kwargs):
        calls.append(1)
        if always_timeout or len(calls) == 1:
            from fastapi import HTTPException
            raise HTTPException(503, "受控 SQL 超时") from psycopg.errors.QueryCanceled("fixture timeout")
        return execute_sql(*args, **kwargs)
    monkeypatch.setattr(runtime, "execute", execute_fixture)
    run_id = client.post("/api/analysis-runs", json={"question": "Top N 合计", "request_id": str(uuid4())}).json()["id"]
    run_once(repo)
    run = client.get("/api/analysis-runs/"+run_id).json()
    assert run["status"] == "pending" and run["state"]["phase"] == "backoff", run
    with repo.db.transaction() as conn:
        conn.execute("UPDATE tasks SET ready_at=now()-interval '1 second' WHERE id=%s", (run_id,))
    run_once(repo)
    run = client.get("/api/analysis-runs/"+run_id).json()
    assert run["status"] == ("failed" if always_timeout else "completed"), run
    assert len(calls) == 2 and run["state"]["sql_calls"] == 2 and run["state"]["sql_retries"] == 1


def test_cancel_during_query_fences_late_receipt(platform, monkeypatch):
    client, repo, _ = platform
    deterministic_model(monkeypatch)
    execute_sql = runtime.execute
    run_id = client.post("/api/analysis-runs", json={"question": "Top N 合计", "request_id": str(uuid4())}).json()["id"]
    def cancel(*args, **kwargs):
        result = execute_sql(*args, **kwargs)
        assert client.post(f"/api/tasks/{run_id}/stop", json={}).status_code == 200
        return result
    monkeypatch.setattr(runtime, "execute", cancel)
    run_once(repo)
    run = client.get("/api/analysis-runs/"+run_id).json()
    assert run["status"] == "canceled", run
    assert not any(a["kind"] == "report" for a in run["artifacts"])
    assert repo.claim(True) is None


def test_metric_revocation_hides_detail_history_and_sse(platform, monkeypatch):
    client, repo, _ = platform
    deterministic_model(monkeypatch)
    run_id = client.post("/api/analysis-runs", json={"question": "Top N 合计", "request_id": str(uuid4())}).json()["id"]
    run_once(repo)
    with repo.db.transaction() as conn:
        conn.execute("UPDATE users SET allowed_metrics=ARRAY['gmv'] WHERE id='admin'")
    assert client.get("/api/analysis-runs/"+run_id).status_code == 404
    assert client.get("/api/tasks/"+run_id).status_code == 404
    assert client.get(f"/api/tasks/{run_id}/events").status_code == 404
    assert client.get("/api/analysis-runs").json() == []


def test_database_query_role_cannot_read_platform_or_raw_facts(platform):
    _, _, source = platform
    with runtime.connect(source) as conn:
        assert conn.execute("SELECT count(*) FROM metric_orders").fetchone()["count"] > 0
        for statement in ["SELECT * FROM platform.users", "SELECT * FROM analytics_fixture.order_facts", "DELETE FROM metric_orders"]:
            with pytest.raises(psycopg.Error):
                conn.execute(statement)


def test_retention_preserves_report_usage_and_deletes_graph(platform, monkeypatch):
    client, repo, _ = platform
    deterministic_model(monkeypatch)
    run_id = client.post("/api/analysis-runs", json={"question": "Top N 合计", "request_id": str(uuid4())}).json()["id"]
    run_once(repo)
    old = client.get("/api/analysis-runs/"+run_id).json()
    with repo.db.transaction() as conn:
        conn.execute("UPDATE tasks SET updated_at=now()-interval '31 days' WHERE id=%s", (run_id,))
    from agent.platform_app.retention import prune
    prune(repo)
    prune(repo)
    run = client.get("/api/analysis-runs/"+run_id).json()
    assert run["status"] == "completed" and run["usage"] == old["usage"]
    assert run["steps"] == [] and [a["kind"] for a in run["artifacts"]] == ["report"]
    assert run["state"]["feedback"]["evidence_expired"]
    with repo.db.transaction() as conn:
        assert conn.execute("SELECT count(*) n FROM agent_checkpoint.checkpoints WHERE thread_id=%s", (run_id,)).fetchone()["n"] == 0


def test_postgres_olist_import_and_all_six_metric_definitions(platform, tmp_path):
    _, repo, source = platform
    from agent.tools.import_olist_postgres import provision
    files = {
        "olist_orders_dataset.csv": "order_id,customer_id,order_status,order_purchase_timestamp,order_delivered_customer_date\na,c,delivered,2018-07-01 00:00:00,2018-07-03 12:00:00\nb,d,canceled,2018-07-02 00:00:00,\ne,f,unavailable,2018-07-03 00:00:00,\n",
        "olist_customers_dataset.csv": "customer_id,customer_state\nc,SP\nd,RJ\nf,MG\n",
        "olist_order_items_dataset.csv": "order_id,order_item_id,price\na,1,100.10\na,2,20.20\nb,1,99.99\n",
        "olist_order_reviews_dataset.csv": "review_id,order_id,review_score,review_creation_date,review_answer_timestamp\nr1,a,1,2018-07-04 00:00:00,2018-07-04 01:00:00\nr2,a,5,2018-07-05 00:00:00,2018-07-05 01:00:00\n",
    }
    for name, value in files.items():
        (tmp_path/name).write_text(value, encoding="utf-8")
    with psycopg.connect(repo.db.dsn, autocommit=True) as conn:
        digest = provision(conn, tmp_path, "analytics_imported", source.user, "", create_role=False)
        with pytest.raises(psycopg.errors.DuplicateSchema):
            provision(conn, tmp_path, "analytics_imported", source.user, "", create_role=False)
    imported = source.model_copy(update={"schema_name": "analytics_imported", "dataset_revision": digest})
    from agent.runtime.plan import QueryPlan
    values = {}
    for metrics in [["gmv", "orders", "aov"], ["cancellation_rate", "delivery_days", "review_score"]]:
        result = runtime.execute_plan(imported, QueryPlan.model_validate({"metrics": metrics, "time_range": {"start": "2018-07-01", "end_exclusive": "2018-08-01"}}), list(runtime.catalog().metrics), {})
        values.update({m: row["value"] for m, row in result["rows"][0]["values"].items()})
    from decimal import Decimal
    assert Decimal(values["gmv"]) == Decimal("120.30")
    assert Decimal(values["aov"]) == Decimal("120.30")
    assert int(values["orders"]) == 1
    assert abs(Decimal(values["cancellation_rate"])-Decimal(100)/3) < Decimal("0.000000001")
    assert Decimal(values["delivery_days"]) == Decimal("2.5") and Decimal(values["review_score"]) == 5


@pytest.mark.parametrize("fixture", ["orders.csv", "edge-orders.csv"])
def test_postgres_all_explicit_specs_against_independent_csv(platform, monkeypatch, fixture):
    _, repo, source = platform
    from agent.runtime.analysis_api import ToolRequest, tool
    from evals.analysis.dataset import load
    from evals.analysis.oracle import expected, score
    if fixture == "edge-orders.csv":
        with repo.db.transaction() as conn:
            conn.execute("TRUNCATE analytics_fixture.order_facts")
            with (ROOT/"evals/analysis"/fixture).open(encoding="utf-8", newline="") as stream:
                for row in csv.DictReader(stream):
                    conn.execute("INSERT INTO analytics_fixture.order_facts VALUES(%s,%s,%s,%s,%s,NULL,NULL)", (row["order_id"], row["date"], row["state"], row["status"], row["amount"] or None))
    identity = {"tenant_id": "demo", "role": "admin", "allowed_metrics": ["gmv", "orders", "aov"]}
    from agent.runtime.analysis_api import Identity, metadata
    meta = metadata(Identity.model_validate(identity))[1]
    cases = [c for c in load()["cases"] if c["fixture"] == fixture and c.get("spec") and c["id"].endswith("-1")]
    for case in cases:
        current, artifacts, report = Spec.model_validate(case["spec"]), [], None
        for _ in range(12):
            output = tool(ToolRequest.model_validate({**identity, "binding": meta["binding"], "question": case["question"], "spec": case["spec"], "artifacts": artifacts, "action": next_action(current, artifacts)["action"]}))
            if output["status"] != "completed":
                assert output["error_code"] == "missing_evidence", (case["id"], output)
                break
            artifacts.append(output["artifact"])
            if artifacts[-1]["kind"] == "report":
                report = artifacts[-1]["data"]
                break
        gold = expected(ROOT/"evals/analysis"/fixture, case["spec"])
        assert score(report, gold) if gold["complete"] else report is None, (case["id"], report, gold)
