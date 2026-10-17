"""真实 PostgreSQL + 独立进程验证框架边界，不以内存 saver 证明恢复。"""
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

import psycopg
import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import START, END, StateGraph
from langgraph.types import Command, interrupt
from psycopg.rows import dict_row

from agent.platform_app.checkpoints import FencedPostgresSaver, LeaseLost


def connect(dsn):
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)


@pytest.fixture
def poc_database():
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("requires isolated TEST_DATABASE_URL")
    # 测试不接触平台库：建立独立随机数据库，清理目标固定为刚创建的名称。
    import secrets
    name = "test_agent_poc_" + secrets.token_hex(6)
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    with connect(dsn) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    isolated = make_conninfo(dsn, dbname=name)
    with connect(isolated) as conn:
        conn.execute("CREATE SCHEMA platform; CREATE SCHEMA agent_checkpoint")
        conn.execute("CREATE TABLE platform.tasks(id text PRIMARY KEY, execution_version int, status text, lease_until timestamptz)")
        conn.execute("INSERT INTO platform.tasks VALUES('poc',1,'querying',now()+interval '5 minutes')")
        conn.execute("CREATE TABLE platform.receipts(id text PRIMARY KEY, value jsonb)")
        conn.execute("SET search_path=agent_checkpoint")
        PostgresSaver(conn).setup()
    yield isolated
    with connect(dsn) as admin:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


class State(TypedDict, total=False):
    operation_id: str
    answer: str


def run_graph(dsn, mode):
    with connect(dsn) as conn:
        conn.execute("SET search_path=agent_checkpoint")
        saver = FencedPostgresSaver(conn, "poc", 1)
        def prepare(state):
            return {"operation_id": "stable-operation"}
        def effect(state):
            # 回执先提交；图检查点缺失时，重放不重复执行外部操作。
            with connect(dsn) as ledger:
                row = ledger.execute("SELECT value FROM platform.receipts WHERE id=%s", (state["operation_id"],)).fetchone()
                if row:
                    if row["value"] == {"status": "running"}:
                        raise RuntimeError("unknown_execution: do not resend model")
                    return {}
                ledger.execute("INSERT INTO platform.receipts VALUES(%s,%s::jsonb)", (state["operation_id"], '{"status":"running"}' if mode == "unknown" else '{"status":"completed"}'))
            if mode in ("crash", "unknown"):
                os._exit(71)
            return {}
        def wait(state):
            return {"answer": interrupt({"missing": "date"})}
        builder = StateGraph(State)
        builder.add_node("prepare", prepare)
        builder.add_node("effect", effect)
        builder.add_node("wait", wait)
        builder.add_edge(START, "prepare")
        builder.add_edge("prepare", "effect")
        builder.add_edge("effect", "wait")
        builder.add_edge("wait", END)
        graph = builder.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": "poc"}}
        checkpoint = graph.get_state(config)
        value = Command(resume="2018-07") if checkpoint.interrupts else None if checkpoint.values else {}
        return graph.invoke(value, config, durability="sync")


def child(dsn, mode):
    env = {**os.environ, "POC_DSN": dsn, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    return subprocess.run([sys.executable, str(Path(__file__).resolve()), mode], env=env,
                          capture_output=True, text=True, timeout=30)


def test_process_crash_receipt_replay_and_interrupt_resume(poc_database):
    assert child(poc_database, "crash").returncode == 71
    assert child(poc_database, "resume").returncode == 0  # 重放回执，持久化 interrupt。
    value = child(poc_database, "resume")
    assert value.returncode == 0, value.stderr
    assert '2018-07' in value.stdout
    with connect(poc_database) as conn:
        assert conn.execute("SELECT count(*) n FROM platform.receipts").fetchone()["n"] == 1


def test_unknown_model_not_resent(poc_database):
    assert child(poc_database, "unknown").returncode == 71
    value = child(poc_database, "resume")
    assert value.returncode != 0 and "unknown_execution" in value.stderr
    with connect(poc_database) as conn:
        assert conn.execute("SELECT count(*) n FROM platform.receipts").fetchone()["n"] == 1


def test_stale_epoch_blocks_checkpoint_and_pending_writes(poc_database):
    with connect(poc_database) as conn:
        conn.execute("SET search_path=agent_checkpoint")
        saver = FencedPostgresSaver(conn, "poc", 1)
        config = {"configurable": {"thread_id": "poc", "checkpoint_ns": ""}}
        saved = saver.put(config, empty_checkpoint(), {"source": "input", "step": 0}, {})
        conn.execute("UPDATE platform.tasks SET execution_version=2 WHERE id='poc'")
        with pytest.raises(LeaseLost):
            saver.put(config, empty_checkpoint(), {"source": "loop", "step": 1}, {})
        with pytest.raises(LeaseLost):
            saver.put_writes(saved, [("answer", "late")], "old-worker")
        assert conn.execute("SELECT count(*) n FROM checkpoint_writes").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) n FROM checkpoints").fetchone()["n"] == 1


if __name__ == "__main__":
    print(json.dumps(run_graph(os.environ["POC_DSN"], sys.argv[1]), default=str))
