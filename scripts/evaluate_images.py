"""隔离运行实际发布镜像，直接读取测试元数据库遥测，不修改服务入口。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse
from datetime import datetime, timezone

import httpx
import pymysql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.cases import cases
from evals.oracle import facts
from scripts.evaluate_agent import execute_case, digest


class Stack:
    def __init__(self, images, folder, mode, budget):
        self.images, self.folder = images, folder
        self.project = "imageeval" + secrets.token_hex(8)
        self.password, self.key = secrets.token_hex(24), secrets.token_hex(24)
        self.prefix = secrets.token_hex(8)
        self.config = folder / "compose.json"
        self.compose = ["docker", "compose", "-p", self.project, "-f", str(self.config)]
        self.mode, self.budget = mode, budget
        self.sessions = []

    def call(self, *args, data=None, timeout=180):
        result = subprocess.run([*self.compose, *args], input=data, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            # 只记录动作名；Compose 的异常正文可能含完整环境配置。
            raise RuntimeError("image_evaluation_command_failed_" + args[0])
        return result.stdout.strip()

    def sql(self, query):
        return self.call("exec", "-T", "postgres", "psql", "-U", "evaluation", "-d", "evaluation", "-At", data=query)

    def start(self, rows):
        self.origin = "http://web:8080"
        dsn = f"postgres://evaluation:{self.password}@postgres:5432/evaluation?sslmode=disable"
        health = lambda command: {"test": command, "interval": "1s", "timeout": "7s", "retries": 60}
        services = {
            "postgres": {"image": "postgres:17-alpine", "environment": {"POSTGRES_USER": "evaluation", "POSTGRES_PASSWORD": self.password, "POSTGRES_DB": "evaluation"}, "healthcheck": health(["CMD", "pg_isready", "-U", "evaluation"]), "tmpfs": ["/var/lib/postgresql/data"]},
            "mysql": {"image": "mysql:8.4", "environment": {"MYSQL_ROOT_PASSWORD": self.password, "MYSQL_ROOT_HOST": "%"}, "ports": ["127.0.0.1::3306"], "healthcheck": health(["CMD", "mysqladmin", "ping", "-h", "localhost", "--silent"]), "tmpfs": ["/var/lib/mysql"]},
            "api": {"image": self.images["api"], "environment": {"DATABASE_URL": dsn, "PUBLIC_ORIGIN": self.origin, "COOKIE_SECURE": "false", "SERVICE_KEY": self.key, "AGENT_URL": "http://agent:8000"}, "healthcheck": health(["CMD", "/app/api", "-command", "health"])},
            "agent": {"image": self.images["agent"], "environment": {"SERVICE_KEY": self.key}, "healthcheck": health(["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2)"])},
            "web": {"image": self.images["web"], "ports": ["127.0.0.1::8080"], "healthcheck": health(["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/healthz"])},
        }
        if self.mode == "live":
            services["model"] = {"image": self.images["agent"], "entrypoint": ["python", "/evaluation/gateway.py"],
                "volumes": [{"type": "bind", "source": str(ROOT / "scripts/eval_model_gateway.py"), "target": "/evaluation/gateway.py", "read_only": True}],
                "environment": {"EVAL_MODEL_KEY": self.key, "EVAL_MAX_MODEL_CALLS": str(self.budget), "LLM_BASE_URL": os.environ["LLM_BASE_URL"], "LLM_API_KEY": os.environ["LLM_API_KEY"]},
                "healthcheck": health(["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/healthz',timeout=2)"])}
            services["agent"]["environment"].update(LLM_API_KEY=self.key, LLM_BASE_URL="http://model:9000", LLM_MODEL=os.environ["LLM_MODEL"])
        self.services = services
        services["api"]["depends_on"] = {"agent": {"condition": "service_healthy"}}
        services["web"]["depends_on"] = {"api": {"condition": "service_healthy"}}
        self.write()
        self.call("up", "--wait", "--wait-timeout", "120", "postgres", "mysql")
        port = int(self.call("port", "mysql", "3306").rsplit(":", 1)[1])
        # mysqladmin 在初始化临时实例时也可能成功；以真实 TCP 登录为准。
        deadline, db = time.monotonic() + 60, None
        while time.monotonic() < deadline:
            try:
                db = pymysql.connect(host="127.0.0.1", port=port, user="root", password=self.password, autocommit=True, connect_timeout=2)
                break
            except pymysql.MySQLError:
                time.sleep(0.5)
        if db is None:
            raise RuntimeError("image_mysql_login_timeout")
        with db:
            sources = {}
            with db.cursor() as cursor:
                for tenant in ("ea", "eb"):
                    schema, user = "fixture_" + tenant, "ro_" + tenant
                    cursor.execute("CREATE DATABASE " + schema)
                    cursor.execute(f"CREATE TABLE {schema}.facts(purchased_at DATETIME,customer_state VARCHAR(2),order_status VARCHAR(16),item_amount DECIMAL(20,2),delivery_days DECIMAL(20,8),review_score DECIMAL(10,4))")
                    cursor.executemany(f"INSERT INTO {schema}.facts VALUES(%s,%s,%s,%s,%s,%s)", [(r["purchased_at"], r["state"], r["status"], r["amount"], r["delivery"], r["review"]) for r in rows])
                    cursor.execute(f"CREATE VIEW {schema}.metric_orders AS SELECT * FROM {schema}.facts" + (" WHERE customer_state='SP'" if tenant == "eb" else ""))
                    cursor.execute("CREATE USER %s@'%%' IDENTIFIED BY %s", (user, self.password))
                    cursor.execute(f"GRANT SELECT ON {schema}.metric_orders TO %s@'%%'", (user,))
                    for role in ("admin", "ops"):
                        sources[tenant + "_" + self.prefix + ":" + role] = {"host": "mysql", "database": schema, "user": user, "password": self.password, "analysis_date": "2018-08-31"}
        services["agent"]["environment"]["TENANT_DATASOURCES"] = json.dumps(sources)
        self.write()
        self.call("run", "--rm", "--no-deps", "api", "-command", "migrate")
        for tenant, role in (("ea", "admin"), ("eb", "admin"), ("ea", "ops")):
            self.call("run", "--rm", "--no-deps", "api", "-command", "bootstrap", "-tenant", tenant + "_" + self.prefix, "-user", role, "-role", role, data=self.password + "\n")
        self.sql("UPDATE users SET allowed_metrics=ARRAY['gmv'] WHERE role='ops';")
        names = ["agent", "api", "web"] + (["model"] if self.mode == "live" else [])
        self.call("up", "--wait", "--wait-timeout", "120", *names)
        self.url = "http://" + self.call("port", "web", "8080")
        for tenant, role in (("ea", "admin"), ("eb", "admin"), ("ea", "ops")):
            client = httpx.Client(base_url=self.url, headers={"Origin": self.origin}, timeout=5, trust_env=False)
            self.sessions.append(client)
            response = client.post("/api/login", json={"tenant": tenant + "_" + self.prefix, "username": role, "password": self.password})
            if response.status_code != 200:
                raise RuntimeError("image_evaluation_login_failed")

    def write(self):
        self.config.write_text(json.dumps({"services": self.services}), encoding="utf-8")
        os.chmod(self.config, 0o600)

    def observe(self, task_id):
        if not re.fullmatch(r"[a-f0-9]{48}", task_id):
            raise ValueError("invalid_task_id")
        attempts = json.loads(self.sql(f"SELECT COALESCE(json_agg(json_build_object('telemetry',telemetry,'call_ms',call_ms,'error',error_code) ORDER BY execution_version),'[]'::json) FROM task_attempts WHERE task_id='{task_id}';"))
        if not attempts or any(a["telemetry"] is None for a in attempts):
            raise RuntimeError("image_attempt_telemetry_missing")
        traces = [a["telemetry"] for a in attempts]
        last = traces[-1]
        result = {"sql_calls": sum("sql" in t["stages_ms"] for t in traces), "model_calls": sum(t["model_calls"] for t in traces),
            "duration_ms": sum(a["call_ms"] or 0 for a in attempts), "known_tokens": sum(t["total_tokens"] or 0 for t in traces),
            "unknown_usage_calls": sum(t["model_calls"] for t in traces if t["total_tokens"] is None), "parser": last["parser"], "error_code": attempts[-1]["error"], "stages_ms": {}}
        for trace in traces:
            for stage, elapsed in trace["stages_ms"].items():
                result["stages_ms"][stage] = result["stages_ms"].get(stage, 0) + elapsed
        return result

    def verify_sse(self):
        task_id = self.sql("SELECT id FROM tasks WHERE status='completed' ORDER BY created_at LIMIT 1;")
        if not task_id:
            return False
        client = self.sessions[0]
        response = client.get("/api/tasks/" + task_id + "/events")
        ids = [int(v) for v in re.findall(r"^id: (\d+)$", response.text, re.MULTILINE)]
        if response.status_code != 200 or "text/event-stream" not in response.headers.get("content-type", "") or not ids:
            return False
        after = ids[0]
        resumed = client.get("/api/tasks/" + task_id + "/events", headers={"Last-Event-ID": str(after)})
        received = [int(v) for v in re.findall(r"^id: (\d+)$", resumed.text, re.MULTILINE)]
        return resumed.status_code == 200 and received == [v for v in ids if v > after]

    def runtime_hash(self):
        # 哈希从正在运行的镜像文件读取，不能拿工作区内容替代。
        program = "import hashlib,os,pathlib; h=hashlib.sha256(); [(h.update(p.name.encode()),h.update(p.read_bytes())) for p in sorted(pathlib.Path('/app/runtime').glob('*')) if p.suffix in ('.py','.json')]; h.update((os.getenv('IDENTITY_BASE','')+'|'+os.getenv('IDENTITY_MODEL','')).encode()); print(h.hexdigest())"
        return self.call("exec", "-T", "-e", "IDENTITY_BASE=" + (os.getenv("LLM_BASE_URL", "") if self.mode == "live" else ""), "-e", "IDENTITY_MODEL=" + (os.getenv("LLM_MODEL", "") if self.mode == "live" else ""), "agent", "python", "-c", program)

    def verify_images(self):
        for name, reference in self.images.items():
            cid = self.call("ps", "-q", name)
            actual = subprocess.run(["docker", "inspect", "--format", "{{.Image}}", cid], capture_output=True, text=True, check=True).stdout.strip()
            expected = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", reference], capture_output=True, text=True, check=True).stdout.strip()
            if actual != expected:
                raise RuntimeError("running_image_identity_mismatch")
            if not self.local:
                revision = subprocess.run(["docker", "inspect", "--format", '{{index .Config.Labels "org.opencontainers.image.revision"}}', cid], capture_output=True, text=True, check=True).stdout.strip()
                if revision != self.commit:
                    raise RuntimeError("running_image_revision_mismatch")

    def close(self):
        for session in self.sessions:
            session.close()
        self.call("down", "--volumes", "--remove-orphans")


def run(args):
    identity = json.loads(args.identity.read_text(encoding="utf-8"))
    images = identity["images"]
    if set(images) != {"api", "agent", "web"} or not re.fullmatch(r"[a-f0-9]{40}", identity["source_commit"]):
        raise ValueError("image_identity_invalid")
    immutable = all(re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", image) for image in images.values())
    releasable = immutable and not args.local_images
    if not immutable and not args.local_images:
        raise ValueError("repository_digests_required")
    if args.mode == "live":
        endpoint = urlparse(os.getenv("LLM_BASE_URL", ""))
        if not all(os.getenv(k) for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")) or endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.query or endpoint.fragment:
            raise ValueError("explicit_https_model_configuration_required")
    selected = [c for c in cases() if c["split"] == "acceptance" and (args.mode == "live" or c["category"] != "paraphrase")]
    rows, results = facts(ROOT / "evals/fixture.csv"), []
    # 直接调用评测入口时也支持全新工作区，不依赖 CLI 预建缓存。
    (ROOT / ".cache").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="image-evaluation-", dir=ROOT / ".cache") as folder:
        stack = Stack(images, Path(folder), args.mode, args.max_model_calls)
        stack.local, stack.commit = args.local_images, identity["source_commit"]
        try:
            stack.start(rows)
            stack.verify_images()
            if httpx.get(stack.url + "/", timeout=5, trust_env=False).status_code != 200:
                raise RuntimeError("web_image_unavailable")
            for case in selected:
                passed, trace = execute_case(stack.sessions[0], "", "", case, stack.sessions, rows, observe=stack.observe)
                results.append({"id": case["id"], "category": case["category"], "critical": case["critical"], "passed": bool(passed), **trace})
                print(json.dumps({"id": case["id"], "passed": bool(passed)}), flush=True)
            report = {"report_version": 1, "status": "complete", "source_commit": identity["source_commit"] if releasable else "worktree-" + identity["source_commit"], "images": images,
                "mode": args.mode, "split": "acceptance", "model": os.getenv("LLM_MODEL") if args.mode == "live" else None,
                "model_endpoint": os.getenv("LLM_BASE_URL") if args.mode == "live" else None,
                "configuration_hash": stack.runtime_hash(), "execution_scope": "release_images" if releasable else "local_images",
                "dataset_hash": digest(ROOT / "evals/cases.py"), "fixture_hash": digest(ROOT / "evals/fixture.csv"), "scorer_version": digest(ROOT / "evals/oracle.py"),
                "executor_version": digest(Path(__file__)), "fixture_kind": "synthetic_order_facts", "cases": results,
                "sse_verified": stack.verify_sse(), "created_at": datetime.now(timezone.utc).isoformat()}
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            stack.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("deterministic", "live"), default="deterministic")
    parser.add_argument("--max-model-calls", type=int, default=45)
    parser.add_argument("--local-images", action="store_true", help="只允许本地诊断；报告不能作为正式发布证明")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        run(args)
        return 0
    except Exception as exc:
        reason = str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith(("image_", "running_", "web_")) else type(exc).__name__
        args.output.write_text(json.dumps({"report_version": 1, "status": "incomplete", "failure": reason}), encoding="utf-8")
        print("Image evaluation incomplete: " + reason, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
