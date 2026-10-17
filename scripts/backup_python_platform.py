"""同一 PG 快照备份平台、检查点和分析数据；恢复只允许显式指定的空库。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

HIDDEN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def command(tool, dsn, args):
    values = conninfo_to_dict(dsn)
    names = {"host": "PGHOST", "port": "PGPORT", "user": "PGUSER", "password": "PGPASSWORD", "dbname": "PGDATABASE", "sslmode": "PGSSLMODE", "sslrootcert": "PGSSLROOTCERT"}
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env.update({names[k]: v for k, v in values.items() if k in names})
    if Path(tool).stem == "pg_restore":
        args = ["--dbname="+values["dbname"], *args]
    result = subprocess.run([str(tool), *args], env=env, capture_output=True, timeout=300, **HIDDEN)
    if result.returncode:
        # 工具错误可能含账号/连接信息；调用者只得到稳定错误标识。
        raise RuntimeError(Path(tool).stem + "_failed")


def counts(conn):
    result = {}
    for schema, table in conn.execute("SELECT schemaname,tablename FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema') ORDER BY 1,2").fetchall():
        result[schema+"."+table] = conn.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table))).fetchone()[0]
    return result


def backup(dsn, destination, pg_dump="pg_dump"):
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError("备份目标已存在，拒绝覆盖")
    temporary = destination.with_name(".partial-"+destination.name+"-"+secrets.token_hex(4))
    temporary.mkdir(mode=0o700)
    dump = temporary/"database.dump"
    with psycopg.connect(dsn) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
        manifest = {"format": 1, "source_database": conn.execute("SELECT current_database()").fetchone()[0],
                    "server_version": conn.info.server_version, "counts": counts(conn)}
        manifest["required_roles"] = [r[0] for r in conn.execute("""SELECT DISTINCT pg_get_userbyid(a.grantee)
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL aclexplode(c.relacl) a
            WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND a.grantee<>0 ORDER BY 1""")]
        command(pg_dump, dsn, ["--format=custom", "--no-owner", "--snapshot="+snapshot, "--file="+str(dump)])
    os.chmod(dump, 0o600)
    with dump.open("rb") as stream:
        manifest["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    with (temporary/"manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
    os.chmod(temporary/"manifest.json", 0o600)
    temporary.rename(destination)
    return manifest


def restore(dsn, source, pg_restore="pg_restore"):
    source = Path(source).resolve()
    manifest = json.loads((source/"manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1:
        raise ValueError("未知备份格式")
    with (source/"database.dump").open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != manifest["sha256"]:
            raise ValueError("备份摘要不一致")
    with psycopg.connect(dsn) as conn:
        if counts(conn):
            raise ValueError("恢复目标不是空库，拒绝覆盖")
        roles = {r[0] for r in conn.execute("SELECT rolname FROM pg_roles")}
        if set(manifest["required_roles"]) - roles:
            raise ValueError("先从密钥管理配置创建备份所需的账号，再恢复授权")
    command(pg_restore, dsn, ["--no-owner", "--exit-on-error", "--single-transaction", str(source/"database.dump")])
    with psycopg.connect(dsn) as conn:
        if counts(conn) != manifest["counts"]:
            raise ValueError("恢复后的逐表计数不一致")
    return manifest


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["backup", "restore"])
    p.add_argument("path", type=Path)
    p.add_argument("--pg-bin", type=Path)
    args = p.parse_args()
    name = "pg_dump" if args.command == "backup" else "pg_restore"
    binary = args.pg_bin / (name+".exe" if os.name == "nt" else name) if args.pg_bin else name
    (backup if args.command == "backup" else restore)(os.environ["BACKUP_DATABASE_URL" if args.command == "backup" else "RESTORE_DATABASE_URL"], args.path, binary)
    print("备份完成" if args.command == "backup" else "恢复与逐表核对完成")
