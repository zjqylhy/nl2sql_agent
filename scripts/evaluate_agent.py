"""隔离测试数据库上的真实问数链路；live 显式调用模型，deterministic 不调用模型。"""
import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pymysql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.cases import cases
from evals.oracle import facts, compare


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wait_health(url, process):
    for _ in range(100):
        if process.poll() is not None: raise RuntimeError("evaluation_process_failed")
        try:
            if httpx.get(url + "/healthz", timeout=1, trust_env=False).status_code == 200: return
        except httpx.HTTPError: pass
        time.sleep(0.1)
    raise RuntimeError("evaluation_health_timeout")


def provision(prefix, rows):
    root = pymysql.connect(host=os.environ["MYSQL_TEST_HOST"], port=int(os.getenv("MYSQL_TEST_PORT", "3306")), user=os.getenv("MYSQL_TEST_USER", "root"), password=os.environ["MYSQL_TEST_PASSWORD"], autocommit=True)
    password = secrets.token_hex(24)
    names = []
    try:
        for marker in ("a", "b"):
            schema, account = "test_eval_" + marker + prefix, "ev_" + marker + prefix
            names.append((schema, account))
            with root.cursor() as cursor:
                cursor.execute("CREATE DATABASE " + schema)
                cursor.execute(f"CREATE TABLE {schema}.facts(purchased_at DATETIME,customer_state VARCHAR(2),order_status VARCHAR(16),item_amount DECIMAL(20,2),delivery_days DECIMAL(20,8),review_score DECIMAL(10,4))")
                cursor.executemany(f"INSERT INTO {schema}.facts VALUES(%s,%s,%s,%s,%s,%s)", [(r["purchased_at"], r["state"], r["status"], r["amount"], r["delivery"], r["review"]) for r in rows])
                cursor.execute(f"CREATE VIEW {schema}.metric_orders AS SELECT * FROM {schema}.facts" + (" WHERE customer_state='SP'" if marker == "b" else ""))
                cursor.execute("CREATE USER %s@'%%' IDENTIFIED BY %s", (account, password))
                cursor.execute(f"GRANT SELECT ON {schema}.metric_orders TO %s@'%%'", (account,))
        sources = {}
        for marker, (schema, account) in zip(("ea", "eb"), names, strict=True):
            for role in ("admin", "ops"):
                sources[marker + "_" + prefix + ":" + role] = {"host": os.environ["MYSQL_TEST_HOST"], "port": int(os.getenv("MYSQL_TEST_PORT", "3306")), "database": schema, "user": account, "password": password, "analysis_date": "2018-08-31"}
        return root, names, sources
    except Exception:
        cleanup(root, names)
        raise


def cleanup(root, names):
    try:
        with root.cursor() as cursor:
            for schema, account in names:
                cursor.execute("DROP DATABASE IF EXISTS " + schema)
                cursor.execute("DROP USER IF EXISTS %s@'%%'", (account,))
    finally:
        root.close()


def execute_case(client, probe, service_key, case, sessions, rows, observe=None):
    alpha, beta, ops = sessions
    question = {"question": case["question"]}
    if case["category"] == "permission":
        check = case["check"]
        if check == "manual_metric":
            response = ops.post("/api/tasks", json={"question": "本月订单量", "metric": "orders"})
            return response.status_code == 403, {"sql_calls": 0, "model_calls": 0}
        if check == "ops_summary":
            response = ops.get("/api/operations/summary")
            own, other = alpha.get("/api/operations/summary"), beta.get("/api/operations/summary")
            invalid = alpha.get("/api/operations/summary?tenant_id=other")
            return response.status_code == 403 and own.status_code == 200 and other.status_code == 200 and other.json()["attempts"] == 0 and invalid.status_code == 400, {"sql_calls": 0, "model_calls": 0}
        task_id = alpha.post("/api/tasks", json=question).json()["id"]
        suffix = {"cross_detail": "", "cross_events": "/events", "cross_stop": "/stop"}[check]
        response = beta.post("/api/tasks/" + task_id + suffix) if check == "cross_stop" else beta.get("/api/tasks/" + task_id + suffix)
        # 等待源任务终止，避免下一案例与上一个任务并发混入观测。
        for _ in range(500):
            status = alpha.get("/api/tasks/" + task_id).json()["status"]
            if status not in ("pending", "querying"): break
            time.sleep(0.05)
        return response.status_code == 404, {"sql_calls": None, "model_calls": None}
    response = alpha.post("/api/tasks", json=question)
    if response.status_code != 202: return False, {"error_code": "task_admission"}
    task_id = response.json()["id"]
    task = None
    for _ in range(1600):
        response = alpha.get("/api/tasks/" + task_id)
        if response.status_code != 200: return False, {"error_code": "task_read"}
        task = response.json()
        if task["status"] not in ("pending", "querying"): break
        time.sleep(0.05)
    else:
        alpha.post("/api/tasks/" + task_id + "/stop")
        return False, {"error_code": "evaluation_timeout"}
    if observe is None:
        observed = httpx.get(probe + "/evaluation/" + task_id, headers={"X-Service-Key": service_key}, timeout=3, trust_env=False)
        if observed.status_code != 200: return False, {"error_code": "observation_missing"}
        trace = observed.json()
    else:
        trace = observe(task_id)
    trace["task_status"] = task["status"]
    if case["category"] in ("clarification", "unsupported"):
        return task["status"] == "needs_clarification" and trace["sql_calls"] == 0 and trace["model_calls"] == 0, trace
    if task["status"] != "completed": return False, {**trace, "error_code": trace.get("error_code") or "not_completed"}
    passed = compare(task["result"], case["expected"], rows) and (case["category"] != "rules" or trace["model_calls"] == 0)
    if not passed: trace["error_code"] = "answer_mismatch"
    return passed, trace


def run(args):
    if not all(os.getenv(k) for k in ("TEST_DATABASE_URL", "MYSQL_TEST_HOST", "MYSQL_TEST_PASSWORD")):
        raise RuntimeError("explicit_test_databases_required")
    if args.mode == "live" and not all(os.getenv(k) for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")):
        raise RuntimeError("live_model_configuration_required")
    fixture = ROOT / "evals/fixture.csv"
    rows = facts(fixture)
    all_cases = cases()
    assert len(all_cases) == 120 and all(sum(c["split"] == split for c in all_cases) == 60 for split in ("development", "acceptance"))
    selected = [c for c in all_cases if (args.split == "all" or c["split"] == args.split) and (args.mode == "live" or c["category"] != "paraphrase")]
    if args.case_id:
        selected = [c for c in selected if c["id"] == args.case_id]
        if not selected: raise RuntimeError("evaluation_case_missing")
    if args.mode == "live" and sum(c["category"] == "paraphrase" for c in selected) * 3 > args.max_model_calls:
        raise RuntimeError("evaluation_call_budget_too_small")
    prefix, password, key = secrets.token_hex(8), secrets.token_hex(24), secrets.token_hex(24)
    root, names, sources = provision(prefix, rows)
    processes, sessions = [], []
    try:
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0)); port = available.getsockname()[1]
        probe = f"http://127.0.0.1:{port}"
        env = {**os.environ, "SERVICE_KEY": key, "TENANT_DATASOURCES": json.dumps(sources), "PYTHONPATH": str(args.agent_root.resolve()) + os.pathsep + str(ROOT), "EVAL_PREFIX": prefix, "EVAL_PASSWORD": password, "AGENT_URL": probe}
        env["EVAL_MAX_MODEL_CALLS"] = str(args.max_model_calls)
        if args.mode == "deterministic": env.pop("LLM_API_KEY", None)
        # 输出含服务日志，写入临时文件避免堵塞管道；不加入默认评测报告。
        with tempfile.TemporaryFile() as log:
            agent = subprocess.Popen([sys.executable, "-m", "uvicorn", "scripts.eval_runtime:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"], env=env, stderr=log, stdout=log)
            processes.append(agent); wait_health(probe, agent)
            server = subprocess.Popen([str(args.fixture_server.resolve())], env=env, stdout=subprocess.PIPE, stderr=log, text=True)
            processes.append(server)
            # 启动失败即退出，不把缺失报告误判为通过。
            import concurrent.futures
            reader = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                ready = reader.submit(server.stdout.readline).result(timeout=20)
                url = json.loads(ready)["api_url"]
            except Exception:
                server.terminate()
                raise RuntimeError("fixture_start_failed")
            finally:
                reader.shutdown(wait=True)
            wait_health(url, server)
            for tenant, role in (("ea", "admin"), ("eb", "admin"), ("ea", "ops")):
                session = httpx.Client(base_url=url, headers={"Origin": url}, timeout=5, trust_env=False)
                sessions.append(session)
                if session.post("/api/login", json={"tenant": tenant + "_" + prefix, "username": role, "password": password}).status_code != 200:
                    raise RuntimeError("fixture_login_failed")
            results = []
            for case in selected:
                try:
                    passed, trace = execute_case(sessions[0], probe, key, case, sessions, rows)
                except (httpx.HTTPError, ValueError, KeyError, TypeError, ArithmeticError):
                    passed, trace = False, {"error_code": "evaluation_error"}
                results.append({"id": case["id"], "category": case["category"], "critical": case["critical"], "passed": bool(passed), "model_calls": trace.get("model_calls"), "sql_calls": trace.get("sql_calls"), "duration_ms": trace.get("duration_ms"), "tokens": (trace.get("usage") or {}).get("tokens", (trace.get("usage") or {}).get("total_tokens")), "error_code": trace.get("error_code", "")})
                results[-1].update(task_status=trace.get("task_status"), parser=trace.get("parser", "unknown"), known_tokens=trace.get("known_tokens"), unknown_usage_calls=trace.get("unknown_usage_calls"), stages_ms=trace.get("stages_ms"))
                if trace.get("known_tokens") is not None:
                    results[-1]["tokens"] = trace["known_tokens"] if trace["unknown_usage_calls"] == 0 else None
                print(json.dumps({"id": case["id"], "passed": bool(passed)}, ensure_ascii=False), flush=True)
            metadata = {"report_version": 1, "status": "complete", "mode": args.mode, "split": args.split, "dataset_hash": digest(ROOT / "evals/cases.py"), "fixture_hash": digest(fixture), "scorer_version": digest(ROOT / "evals/oracle.py"), "source_commit": args.source_commit, "model": os.getenv("LLM_MODEL") if args.mode == "live" else None, "cases": results,
                        "configuration_hash": hashlib.sha256((os.getenv("LLM_BASE_URL", "") + "|" + os.getenv("LLM_MODEL", "") + "|" + digest(args.agent_root / "runtime/app.py") + "|" + digest(args.agent_root / "runtime/catalog.json")).encode()).hexdigest()}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            metadata["created_at"] = datetime.now(timezone.utc).isoformat()
            metadata["fixture_kind"] = "synthetic_order_facts"
            metadata["platform_scope"] = "candidate_go_platform_for_both_agents"
            executor_hash = hashlib.sha256()
            for file in ("scripts/evaluate_agent.py", "scripts/eval_runtime.py", "cmd/evalfixture/main.go"):
                executor_hash.update((ROOT / file).read_bytes())
            metadata["executor_version"] = executor_hash.hexdigest()
            supported = [c for c in results if c["category"] in ("rules", "paraphrase")]
            answered = [c for c in supported if c["task_status"] == "completed"]
            metadata["summary"] = {"supported_correct": sum(c["passed"] for c in supported), "supported_total": len(supported), "answered_correct": sum(c["passed"] for c in answered), "answered_total": len(answered), "categories": {kind: {"passed": sum(c["passed"] for c in results if c["category"] == kind), "total": sum(c["category"] == kind for c in results)} for kind in ("rules", "paraphrase", "clarification", "unsupported", "permission")}}
            if args.case_id: metadata["split"] = "diagnostic"
            runtime_hash = hashlib.sha256()
            for path in sorted((args.agent_root / "runtime").glob("*")):
                if path.suffix in (".py", ".json"):
                    runtime_hash.update(path.name.encode())
                    runtime_hash.update(path.read_bytes())
            runtime_hash.update((os.getenv("LLM_BASE_URL", "") + "|" + os.getenv("LLM_MODEL", "")).encode())
            metadata["configuration_hash"] = runtime_hash.hexdigest()
            args.output.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            return metadata
    finally:
        for session in sessions: session.close()
        for process in reversed(processes):
            process.terminate()
            try: process.wait(timeout=15)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        cleanup(root, names)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--agent-root", type=Path, default=ROOT / "agent")
    p.add_argument("--fixture-server", type=Path, required=True)
    p.add_argument("--mode", choices=("deterministic", "live"), default="deterministic")
    p.add_argument("--split", choices=("development", "acceptance", "all"), default="development")
    p.add_argument("--source-commit", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--max-model-calls", type=int, default=75)
    p.add_argument("--case-id", help="单案例诊断报告不能用于发布验收")
    args = p.parse_args()
    try:
        run(args)
    except Exception:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"report_version": 1, "status": "incomplete"}), encoding="utf-8")
        print("Evaluation incomplete; release remains blocked", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
