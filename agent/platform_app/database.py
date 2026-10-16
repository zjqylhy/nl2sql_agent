"""平台事务与迁移入口；运行连接池只访问 platform schema。"""
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def migrate(dsn, *, adopt_legacy=False):
    from alembic import command
    from alembic.config import Config
    from langgraph.checkpoint.postgres import PostgresSaver
    root = Path(__file__).parent / "migrations"
    if adopt_legacy:
        with psycopg.connect(dsn) as conn:
            if conn.execute("SELECT to_regclass('platform.tasks')").fetchone()[0]:
                raise ValueError("目标 platform schema 已存在，拒绝重复接管")
            if conn.execute("SELECT count(*) FROM public.tasks WHERE status IN ('pending','querying','needs_clarification')").fetchone()[0]:
                raise ValueError("先停止旧服务并完成或取消未结束任务，再接管")
            for path in sorted((root / "baseline").glob("*.sql")):
                row = conn.execute("SELECT checksum FROM public.schema_migrations WHERE version=%s", ("migrations/" + path.name,)).fetchone()
                if not row or row[0] != hashlib.sha256(path.read_bytes()).hexdigest():
                    raise ValueError("旧库迁移版本或校验值不匹配")
            conn.execute("CREATE SCHEMA platform")
            names = ["tenants", "users", "sessions", "tasks", "task_events", "task_outbox", "task_attempts", "conversations", "analysis_runs", "analysis_steps", "analysis_step_attempts", "analysis_artifacts", "analysis_budget_ledger", "analysis_answers", "schema_migrations"]
            for name in names:
                conn.execute(sql.SQL("ALTER TABLE public.{} SET SCHEMA platform").format(sql.Identifier(name)))
    config = Config()
    config.set_main_option("script_location", str(root))
    config.attributes["url"] = "postgresql+psycopg://" + dsn.split("://", 1)[1]
    command.upgrade(config, "head")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS agent_checkpoint")
        conn.execute("SET search_path=agent_checkpoint,pg_catalog")
        PostgresSaver(conn).setup()


class Database:
    def __init__(self, dsn=None):
        self.dsn = dsn or os.environ["DATABASE_URL"]
        self.pool = ConnectionPool(make_conninfo(self.dsn, options="-c search_path=platform,pg_catalog -c statement_timeout=5000"),
            min_size=1, max_size=8, timeout=5, kwargs={"row_factory": dict_row}, open=True)
        self.pool.wait(timeout=10)

    @contextmanager
    def transaction(self):
        with self.pool.connection() as conn, conn.transaction():
            yield conn

    def close(self):
        self.pool.close()
