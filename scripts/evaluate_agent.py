"""隔离测试数据库上的真实问数链路；live 显式调用模型，deterministic 不调用模型。"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

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
        observed = httpx.get(probe + "/evaluation/tasks/" + task_id, headers={"X-Service-Key": service_key}, timeout=3, trust_env=False)
        if observed.status_code != 200: return False, {"error_code": "observation_missing"}
        trace = observed.json()
        trace["known_tokens"] = trace["known_total_tokens"]
        trace["error_code"] = next(iter(trace["error_codes"]), "")
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
    if not os.getenv("TEST_DATABASE_URL"):
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
    from scripts.python_query_stack import stack
    sessions = []
    with stack(rows, args.agent_root, args.max_model_calls, live=args.mode == "live") as (url, prefix, password, key):
        probe = url
        try:
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
            metadata = {"report_version": 1, "status": "complete", "mode": args.mode, "split": args.split, "dataset_hash": digest(ROOT / "evals/cases.py"), "fixture_hash": digest(fixture), "scorer_version": digest(ROOT / "evals/oracle.py"), "source_commit": args.source_commit, "model": os.getenv("LLM_MODEL") if args.mode == "live" else None, "cases": results}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            metadata["created_at"] = datetime.now(timezone.utc).isoformat()
            metadata["fixture_kind"] = "synthetic_order_facts"
            metadata["platform_scope"] = "candidate_python_platform_for_both_agents"
            executor_hash = hashlib.sha256()
            for file in ("scripts/evaluate_agent.py", "scripts/python_query_stack.py", "scripts/query_eval_runtime.py"):
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
            for session in sessions:
                session.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--agent-root", type=Path, default=ROOT / "agent")
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
