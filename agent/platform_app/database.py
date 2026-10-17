"""平台事务与迁移入口；运行连接池只访问 platform schema。"""
import os
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def migrate(dsn):
    from alembic import command
    from alembic.config import Config
    from langgraph.checkpoint.postgres import PostgresSaver
    root = Path(__file__).parent / "migrations"
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
