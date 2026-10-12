"""单机私有部署的配置、初始化和双库备份；秘密只从受限文件或 stdin 读取。"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BUSINESS_TABLES = "tenants,users,sessions,tasks,task_events,task_outbox,task_attempts"
BACKUP_NAME = re.compile(r"\d{8}T\d{6}Z-[a-f0-9]{8}")


def copy_counts(lines):
    counts, current = {"users": 0, "tasks": 0}, None
    seen = set()
    for line in lines:
        if current:
            if line.rstrip(b"\r\n") == b"\\.":
                current = None
            else:
                counts[current] += 1
        else:
            match = re.match(rb"COPY public\.(users|tasks) \(", line)
            if match:
                current = match[1].decode()
                seen.add(current)
    if current or seen != set(counts):
        raise ValueError("incomplete_postgres_snapshot_data")
    return str(counts["users"]) + "|" + str(counts["tasks"])


def dotenv(values):
    lines = []
    for key, value in values.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or any(c in value for c in "\r\n\0"):
            raise ValueError("invalid_environment_value")
        lines.append(key + "='" + value.replace("'", "\\'") + "'")
    return "\n".join(lines) + "\n"


def read_env(path):
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise ValueError("environment_must_be_private")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not (value.startswith("'") and value.endswith("'")):
            raise ValueError("generated_environment_required")
        values[key] = value[1:-1].replace("\\'", "'")
    return values


def write_private(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(data)


def init_config(path, revision, source):
    if not re.fullmatch(r"[a-f0-9]{40}", revision) or not source.get("gateway_key"):
        raise ValueError("commit_and_gateway_key_required")
    host = path.parent.resolve()
    values = {name: secrets.token_hex(24) for name in (
        "POSTGRES_ADMIN_PASSWORD", "POSTGRES_RUNTIME_PASSWORD", "MYSQL_ROOT_PASSWORD",
        "MYSQL_READONLY_PASSWORD", "SERVICE_KEY", "BOOTSTRAP_PASSWORD")}
    values.update({name.upper() + "_IMAGE": "nl2sql-" + name + ":" + revision
                   for name in ("api", "agent", "web", "tunnel")})
    values.update(POSTGRES_IMAGE=source["postgres_image"], MYSQL_IMAGE=source["mysql_image"],
                  NL2SQL_AGENT_API_KEY=source["gateway_key"], CSV_DIR=str(host / "csv"),
                  TUNNEL_KEY_FILE=str(host / "secrets/tunnel-key"),
                  GATEWAY_KNOWN_HOSTS_FILE=str(host / "secrets/gateway-known-hosts"),
                  GATEWAY_CA_FILE=str(host / "secrets/gateway-ca.pem"))
    datasource = dict(host="mysql", database="olist_alpha", user="alpha_ro",
                      password=values["MYSQL_READONLY_PASSWORD"], analysis_date="2018-08-31")
    values["TENANT_DATASOURCES"] = json.dumps({"alpha:" + role: datasource for role in ("admin", "ops", "data")}, separators=(",", ":"))
    write_private(path, dotenv(values))


class Stack:
    def __init__(self, env_file):
        self.env_file = env_file.resolve()
        self.values = read_env(self.env_file)
        self.compose = ["docker", "compose", "--env-file", str(self.env_file),
                        "-f", str(ROOT / "deploy/compose.private.yaml")]

    def call(self, *args, data=None, output=None, timeout=300):
        result = subprocess.run([*self.compose, *args], input=data,
                                stdout=output or subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if result.returncode:
            # Docker/数据库异常可能含环境配置，只输出失败的动作名。
            raise RuntimeError("private_operation_failed_" + args[0])
        return result.stdout or b""

    def sql(self, query, database="platform"):
        return self.call("exec", "-T", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-U",
                         "platform_migrator", "-d", database, "-At", data=query.encode())

    def mysql(self, query):
        program = """import json,sys,pymysql
request=json.load(sys.stdin)
with pymysql.connect(host='mysql',user='root',password=request['password'],autocommit=True) as connection:
 with connection.cursor() as cursor:
  cursor.execute(request['query'])
  print(json.dumps(cursor.fetchall(),default=str))
"""
        payload = json.dumps(dict(password=self.values["MYSQL_ROOT_PASSWORD"], query=query)).encode()
        return json.loads(self.call("run", "--rm", "-T", "--no-deps", "--entrypoint", "python", "import", "-c", program, data=payload))

    def dump_counts(self, dump):
        # 从已备份的快照计数，避免备份期间新增任务造成恢复验收误报。
        with dump.open("rb") as source:
            process = subprocess.Popen([*self.compose, "exec", "-T", "postgres", "pg_restore",
                                        "--data-only", "--table=users", "--table=tasks", "--file=-"],
                                       stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                counts = copy_counts(process.stdout)
                if process.wait(timeout=120):
                    raise RuntimeError("postgres_snapshot_inspection_failed")
                return counts
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def initialize(self):
        marker = self.env_file.parent / "state/initialized.json"
        if marker.exists():
            raise ValueError("already_initialized_use_update_procedure")
        self.call("config", "--quiet")
        self.call("up", "--wait", "--wait-timeout", "300", "postgres", "mysql", "gateway", timeout=360)
        if self.mysql("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='olist_alpha'"):
            raise ValueError("analysis_database_already_exists_no_overwrite")
        password = self.values["POSTGRES_RUNTIME_PASSWORD"]
        if not re.fullmatch(r"[a-f0-9]{48}", password):
            raise ValueError("generated_password_required")
        self.sql("SELECT format('CREATE ROLE platform_runtime LOGIN PASSWORD %L', '" + password + "') "
                 "WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='platform_runtime')\n\\gexec\n"
                 "REVOKE CREATE ON SCHEMA public FROM PUBLIC; GRANT USAGE ON SCHEMA public TO platform_runtime;\n")
        self.call("run", "--rm", "-T", "--no-deps", "migrate")
        self.sql("GRANT CONNECT ON DATABASE platform TO platform_runtime; GRANT SELECT,INSERT,UPDATE,DELETE ON "
                 + BUSINESS_TABLES + " TO platform_runtime;\n")
        self.call("run", "--rm", "-T", "--no-deps", "import", timeout=900)
        if self.mysql("SELECT COUNT(*) FROM olist_alpha.order_facts")[0][0] != 99441:
            raise ValueError("olist_order_count_mismatch")
        self.call("run", "--rm", "-T", "--no-deps", "api", "-command", "bootstrap", "-tenant", "alpha",
                  "-user", "admin", "-role", "admin", data=(self.values["BOOTSTRAP_PASSWORD"] + "\n").encode())
        write_private(marker, json.dumps(dict(orders=99441, analysis_date="2018-08-31")) + "\n")
        self.call("up", "--wait", "--wait-timeout", "180", "agent", "api", "web", timeout=240)

    def backup(self):
        root = self.env_file.parent / "backups"
        root.mkdir(mode=0o700, exist_ok=True)
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
        temporary = root / (".partial-" + name)
        temporary.mkdir(mode=0o700)
        for filename, command in (
            ("postgres.dump", ("exec", "-T", "postgres", "pg_dump", "-U", "platform_migrator", "-Fc", "platform")),
            ("postgres-roles.sql", ("exec", "-T", "postgres", "pg_dumpall", "-U", "platform_migrator", "--roles-only")),
            ("mysql.sql", ("exec", "-T", "mysql", "sh", "-c", 'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec mysqldump -uroot --single-transaction --no-tablespaces --routines --events --triggers --databases olist_alpha')),
        ):
            path = temporary / filename
            with path.open("xb") as stream:
                os.chmod(path, 0o600)
                self.call(*command, output=stream, timeout=600)
            if path.stat().st_size == 0:
                raise ValueError("empty_backup")
        # MySQL 8.4 返回单列；兼容带身份列的输出时，定义也位于最后一列。
        account = self.mysql("SHOW CREATE USER 'alpha_ro'@'%'")[0][-1]
        grants = self.mysql("SHOW GRANTS FOR 'alpha_ro'@'%'")
        write_private(temporary / "mysql-accounts.sql", account + ";\n" + "\n".join(row[0] + ";" for row in grants) + "\n")
        counts = self.dump_counts(temporary / "postgres.dump")
        orders = self.mysql("SELECT COUNT(*) FROM olist_alpha.order_facts")[0][0]
        hashes = {}
        for file in temporary.iterdir():
            digest = hashlib.sha256()
            with file.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            hashes[file.name] = digest.hexdigest()
        write_private(temporary / "manifest.json", json.dumps(dict(orders=orders, postgres_counts=counts, sha256=hashes)) + "\n")
        complete = root / name
        temporary.rename(complete)
        # 成功生成完整新备份后才轮换；只处理本工具创建的目录。
        previous = sorted((p for p in root.iterdir() if BACKUP_NAME.fullmatch(p.name) and p.is_dir() and not p.is_symlink()), reverse=True)
        for old in previous[7:]:
            if old.resolve().parent != root.resolve():
                raise ValueError("backup_path_outside_root")
            shutil.rmtree(old)
        return complete


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init-config", "initialize", "backup", "smoke"])
    parser.add_argument("--env-file", type=Path, default=Path("/srv/olist/.env.private"))
    parser.add_argument("--commit")
    args = parser.parse_args()
    if args.action == "init-config":
        init_config(args.env_file, args.commit, json.load(sys.stdin))
        print("Private configuration created")
        return
    # 运维锁与每日备份共用；防止初始化/备份互相交错。
    import fcntl
    lock = args.env_file.parent / "state/private-operations.lock"
    with lock.open("a") as stream:
        os.chmod(lock, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stack = Stack(args.env_file)
        if args.action == "initialize":
            stack.initialize()
            print("Private services initialized and healthy")
        elif args.action == "backup":
            print(stack.backup())
        else:
            sys.path.insert(0, str(ROOT))
            from scripts.cloud_smoke import run
            os.environ.update(SMOKE_TENANT="alpha", SMOKE_USER="admin", SMOKE_PASSWORD=stack.values["BOOTSTRAP_PASSWORD"])
            run("http://localhost:8088", allow_private=True)
            print("Private rules, model and SSE smoke passed")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("Private operation failed; credentials and database data were preserved", file=sys.stderr)
        raise SystemExit(1)
