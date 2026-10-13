"""把私有备份恢复到独立临时容器；不写原数据库或原数据 volume。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

from private_ops import ROOT, Stack


def run(env_file, backup):
    stack = Stack(env_file)
    manifest = json.loads((backup / "manifest.json").read_text())
    for name, expected in manifest["sha256"].items():
        path = backup / name
        if path.resolve().parent != backup.resolve() or path.is_symlink():
            raise ValueError("invalid_backup_path")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("backup_digest_mismatch")
    if set(manifest["sha256"]) != {"postgres.dump", "postgres-roles.sql", "mysql.sql", "mysql-accounts.sql"}:
        raise ValueError("incomplete_backup")

    def call(args, *, data=None, stream=None, env=None, timeout=120, check=True):
        result = subprocess.run(["docker", *args], input=data, stdin=stream,
                                capture_output=True, timeout=timeout, env=env)
        if check and result.returncode:
            raise RuntimeError("isolated_restore_operation_failed")
        return result

    for engine in ("postgres", "mysql"):
        name = "nl2sql-restore-" + engine + "-" + secrets.token_hex(8)
        created = False
        try:
            environment = os.environ.copy()
            environment["POSTGRES_PASSWORD"] = stack.values["POSTGRES_ADMIN_PASSWORD"]
            environment["MYSQL_ROOT_PASSWORD"] = stack.values["MYSQL_ROOT_PASSWORD"]
            environment["MYSQL_ROOT_HOST"] = "%"
            options = ["run", "--detach", "--rm", "--name", name, "--network", "none", "--label", "nl2sql.restore-check=true"]
            if engine == "postgres":
                options += ["--memory", "192m", "--memory-swap", "192m", "--env", "POSTGRES_PASSWORD",
                            stack.values["POSTGRES_IMAGE"], "-c", "shared_buffers=32MB", "-c", "max_connections=20"]
                probe = ["exec", name, "pg_isready", "-U", "postgres", "-h", "127.0.0.1"]
            else:
                options += ["--memory", "640m", "--memory-swap", "640m", "--env", "MYSQL_ROOT_PASSWORD", "--env", "MYSQL_ROOT_HOST",
                            "-v", str(ROOT / "deploy/mysql-private.cnf") + ":/etc/mysql/conf.d/private.cnf:ro", stack.values["MYSQL_IMAGE"]]
                probe = ["exec", name, "sh", "-c", 'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; mysql -h127.0.0.1 -uroot -N -e "SELECT 1"']
            call(options, env=environment)
            created = True
            deadline = time.monotonic() + 180
            while call(probe, check=False).returncode:
                if time.monotonic() > deadline:
                    raise RuntimeError("isolated_restore_startup_timeout")
                time.sleep(1)
            if engine == "postgres":
                with (backup / "postgres-roles.sql").open("rb") as stream:
                    call(["exec", "-i", name, "psql", "-v", "ON_ERROR_STOP=1", "-U", "postgres"], stream=stream)
                call(["exec", "-i", name, "psql", "-v", "ON_ERROR_STOP=1", "-U", "postgres"], data=b"CREATE DATABASE platform OWNER platform_migrator;\n")
                with (backup / "postgres.dump").open("rb") as stream:
                    call(["exec", "-i", name, "pg_restore", "--exit-on-error", "-U", "postgres", "-d", "platform"], stream=stream, timeout=300)
                counts = call(["exec", name, "psql", "-U", "postgres", "-d", "platform", "-At", "-c",
                               "SELECT (SELECT COUNT(*) FROM users), (SELECT COUNT(*) FROM tasks)"]).stdout.decode().strip()
                if counts != manifest["postgres_counts"]:
                    raise ValueError("postgres_restore_count_mismatch")
            else:
                for filename in ("mysql.sql", "mysql-accounts.sql"):
                    with (backup / filename).open("rb") as stream:
                        call(["exec", "-i", name, "sh", "-c", 'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec mysql -uroot'], stream=stream, timeout=600)
                environment["MYSQL_PWD"] = stack.values["MYSQL_READONLY_PASSWORD"]
                readonly = ["exec", "--env", "MYSQL_PWD", name, "mysql", "-h127.0.0.1", "-ualpha_ro", "-N", "-e"]
                orders = call([*readonly, "SELECT COUNT(*) FROM olist_alpha.metric_orders"], env=environment).stdout.decode().strip()
                if orders != str(manifest["orders"]):
                    raise ValueError("mysql_restore_count_mismatch")
                if call([*readonly, "SELECT COUNT(*) FROM olist_alpha.order_facts"], env=environment, check=False).returncode == 0:
                    raise ValueError("restored_account_has_excess_permissions")
            status = json.loads(call(["inspect", name]).stdout)[0]["State"]
            if status["OOMKilled"]:
                raise ValueError("restore_container_oom")
            print(engine + " isolated restore passed")
        finally:
            if created:
                # 只删除本次随机命名且带验收标签的临时容器。
                label = call(["inspect", "--format", '{{index .Config.Labels "nl2sql.restore-check"}}', name], check=False)
                if label.returncode == 0 and label.stdout.strip() == b"true":
                    call(["rm", "--force", name])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("--env-file", type=Path, default=Path("/srv/olist/.env.private"))
    args = parser.parse_args()
    try:
        run(args.env_file, args.backup)
    except Exception:
        print("Isolated restore failed; source databases were not modified", file=sys.stderr)
        raise SystemExit(1)
