"""A/B/C 共用 Python 平台、PG 数据和授权；每组新建隔离数据库。"""
from contextlib import contextmanager
import csv
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile

import bcrypt
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from agent.platform_app.database import migrate
from agent.runtime.catalog import catalog

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT/"evals/analysis"
HIDDEN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def uri_for(dsn, database):
    from urllib.parse import quote
    values = conninfo_to_dict(dsn)
    host = values.get("host", "127.0.0.1")
    if ":" in host:
        host = "["+host+"]"
    return "postgresql://{}:{}@{}:{}/{}".format(quote(values.get("user", ""), safe=""), quote(values.get("password", ""), safe=""), host, values.get("port", "5432"), database)


@contextmanager
def stack(fixture, strategy, max_calls):
    if fixture not in {"orders.csv", "edge-orders.csv"} or strategy not in {"adaptive", "fixed"}:
        raise ValueError("invalid_test_fixture")
    from scripts.evaluate_agent import wait_health
    admin_dsn = os.environ["TEST_DATABASE_URL"]
    prefix = secrets.token_hex(8)
    name, role, tenant = "test_analysis_py_"+prefix, "test_analysis_ro_"+prefix, "ea_"+prefix
    readonly_password, login_password, key = [secrets.token_hex(24) for _ in range(3)]
    processes, created = [], False
    old = {k: os.environ.get(k) for k in ("ANALYSIS_EVAL_TENANT", "ANALYSIS_EVAL_USER", "ANALYSIS_EVAL_PASSWORD", "SERVICE_KEY")}
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            created = True
        dsn = uri_for(admin_dsn, name)
        migrate(dsn)
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(readonly_password)))
            conn.execute("CREATE SCHEMA analytics_fixture")
            conn.execute("CREATE TABLE analytics_fixture.order_facts(order_id text PRIMARY KEY,purchased_at timestamp,customer_state text,order_status text,item_amount numeric(18,2),delivery_days numeric,review_score integer)")
            with (DATA/fixture).open(encoding="utf-8", newline="") as stream:
                with conn.cursor() as cursor:
                    cursor.executemany("INSERT INTO analytics_fixture.order_facts VALUES(%s,%s,%s,%s,%s,NULL,NULL)", [(r["order_id"], r["date"], r["state"], r["status"], r["amount"] or None) for r in csv.DictReader(stream)])
            conn.execute("CREATE VIEW analytics_fixture.metric_orders AS SELECT * FROM analytics_fixture.order_facts")
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA analytics_fixture TO {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("GRANT SELECT ON analytics_fixture.metric_orders TO {}").format(sql.Identifier(role)))
            conn.execute("INSERT INTO platform.tenants VALUES(%s,%s)", (tenant, tenant))
            conn.execute("INSERT INTO platform.users(id,tenant_id,username,password_hash,role,allowed_metrics) VALUES(%s,%s,'admin',%s,'admin',%s)", (prefix, tenant, bcrypt.hashpw(login_password.encode(), bcrypt.gensalt(rounds=4)), list(catalog().metrics)))
            conn.execute("CREATE TABLE platform.evaluation_budget(id integer PRIMARY KEY,issued integer NOT NULL,maximum integer NOT NULL)")
            conn.execute("INSERT INTO platform.evaluation_budget VALUES(1,0,%s)", (max_calls,))
        params = conninfo_to_dict(admin_dsn)
        source = dict(driver="postgresql", host=params.get("host", "127.0.0.1"), port=int(params.get("port", 5432)), database=name,
            user=role, password=readonly_password, schema_name="analytics_fixture", analysis_date="2018-08-31", dataset_revision=hashlib.sha256((DATA/fixture).read_bytes()).hexdigest())
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        env = {**os.environ, "DATABASE_URL": dsn, "CHECKPOINT_DATABASE_URL": dsn, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1",
            "TENANT_DATASOURCES": json.dumps({tenant+":admin": source}), "PUBLIC_ORIGIN": base, "COOKIE_SECURE": "false", "SERVICE_KEY": key,
            "ANALYSIS_ENABLED": "true", "ANALYSIS_STRATEGY": strategy}
        with tempfile.TemporaryFile() as log:
            api = subprocess.Popen([sys.executable, "-m", "uvicorn", "scripts.python_analysis_eval_api:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"], cwd=ROOT, env=env, stdout=log, stderr=log, **HIDDEN)
            processes.append(api)
            wait_health(base, api)
            processes.append(subprocess.Popen([sys.executable, "-m", "scripts.python_analysis_eval_worker"], cwd=ROOT, env=env, stdout=log, stderr=log, **HIDDEN))
            os.environ.update(ANALYSIS_EVAL_TENANT=tenant, ANALYSIS_EVAL_USER="admin", ANALYSIS_EVAL_PASSWORD=login_password, SERVICE_KEY=key)
            yield base, base
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=15)
        if created:
            # 名称只来自本次随机生成值，绝不清理用户传入的数据库。
            with psycopg.connect(admin_dsn, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
                if conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                    conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
