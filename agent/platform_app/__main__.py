"""同一代码库分别启动 API、Worker 和显式维护命令。"""
import argparse
import getpass
import os
import secrets
import signal
import threading

import bcrypt
import psycopg
from psycopg import sql

from agent.runtime.catalog import catalog
from .database import Database, migrate
from .policy import Policy
from .repository import Repository


def provision_roles(dsn, api_role="platform_api", worker_role="platform_worker"):
    """迁移身份独占 DDL；API 无图状态权限，查询账号由数据导入单独创建。"""
    with psycopg.connect(dsn) as conn:
        for role, name in ((api_role, "POSTGRES_API_PASSWORD"), (worker_role, "POSTGRES_WORKER_PASSWORD")):
            password = os.environ[name]
            if len(password) < 16:
                raise ValueError("运行账号密码至少 16 字符")
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
            if not exists:
                conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT").format(sql.Identifier(role), sql.Literal(password)))
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA platform TO {};").format(sql.Identifier(role)))
            conn.execute(sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA platform TO {}").format(sql.Identifier(role)))
        conn.execute("REVOKE ALL ON SCHEMA agent_checkpoint FROM PUBLIC")
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA agent_checkpoint TO {}").format(sql.Identifier(worker_role)))
        conn.execute(sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA agent_checkpoint TO {}").format(sql.Identifier(worker_role)))
        # 共享实例也不允许普通账号向 public 放置可被 search_path 劫持的对象。
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["api", "worker", "migrate", "seed", "provision-roles", "prune"])
    parser.add_argument("--tenant", default="demo")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument('--domain', choices=['push', 'orders'], default='push', help='新账号的指标数据域')
    args = parser.parse_args()
    if args.command == "api":
        import uvicorn
        uvicorn.run("agent.platform_app.api:app", host=args.host, port=args.port, access_log=False)
        return
    if args.command == "migrate":
        migrate(os.environ["DATABASE_URL"])
        return
    if args.command == "provision-roles":
        provision_roles(os.environ["DATABASE_URL"])
        return
    db = Database()
    try:
        if args.command == "seed":
            from agent.runtime.push import DEFINITIONS
            metric_ids = list(DEFINITIONS) if args.domain == 'push' else list(catalog().metrics)
            password = os.getenv("BOOTSTRAP_PASSWORD") or getpass.getpass("初始管理员密码: ")
            if not 12 <= len(password.encode()) <= 72:
                raise ValueError("管理员密码必须为 12–72 字节")
            with db.transaction() as conn:
                conn.execute("INSERT INTO tenants VALUES(%s,%s) ON CONFLICT DO NOTHING", (args.tenant, args.tenant))
                conn.execute("INSERT INTO users(id,tenant_id,username,password_hash,role,allowed_metrics) VALUES(%s,%s,%s,%s,'admin',%s)", (secrets.token_hex(24), args.tenant, args.username, bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)), metric_ids))
        elif args.command == "prune":
            from .retention import prune
            prune(Repository(db))
        else:
            from .worker import run
            stop = threading.Event()
            for name in (signal.SIGINT, signal.SIGTERM):
                signal.signal(name, lambda *_: stop.set())
            run(Repository(db), Policy.from_env(), stop)
    finally:
        db.close()


if __name__ == "__main__":
    main()
