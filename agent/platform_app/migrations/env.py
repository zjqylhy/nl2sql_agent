"""平台迁移独立于 API/Worker，启动服务不隐式修改数据库结构。"""
from alembic import context
from sqlalchemy import create_engine

engine = create_engine(context.config.attributes["url"])
with engine.connect() as conn:
    conn.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS platform")
    conn.commit()
    context.configure(connection=conn, version_table_schema="platform")
    with context.begin_transaction():
        conn.exec_driver_sql("SET LOCAL search_path=platform,pg_catalog")
        conn.exec_driver_sql("SELECT pg_advisory_xact_lock(710090)")
        context.run_migrations()
