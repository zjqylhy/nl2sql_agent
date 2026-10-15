"""同提交、同题集对照仅规则和真实模型；不改变生产解析器。"""
import argparse
import concurrent.futures
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.oracle import facts
from evals.understanding.dataset import DATASET, load
from evals.understanding.report import digest, fingerprints, save, summarize
from evals.understanding.scorer import score
from scripts.evaluate_agent import provision, cleanup, wait_health

HIDDEN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def source_identity(args, data, selected):
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    paths = ["agent/runtime", "evals/understanding", "evals/oracle.py", "evals/fixture.csv", "scripts/evaluate_understanding.py", "scripts/understanding_runtime.py", "cmd/evalfixture"]
    changes = subprocess.check_output(["git", "diff", "HEAD", "--", *paths], cwd=ROOT)
    untracked = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "--", *paths], cwd=ROOT)
    if changes or untracked or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("evaluation_code_must_be_committed")
    configuration = "|".join(os.getenv(key, "") for key in ("LLM_BASE_URL", "LLM_MODEL", "LLM_INTENT_MAX_TOKENS", "LLM_PLAN_MAX_TOKENS"))
    return {"source_commit": commit, "dataset_hash": digest(DATASET), "annotation_status": data.annotation_status, "reviewer": data.reviewer,
            "fixture_hash": digest(ROOT / "evals/fixture.csv"), "fixture_server_hash": digest(args.fixture_server), "fixture_kind": "synthetic_order_facts",
            "scorer_hash": fingerprints([ROOT / p for p in ("evals/understanding/scorer.py", "evals/understanding/dataset.py", "evals/oracle.py")]),
            "runtime_hash": fingerprints([p for p in (ROOT / "agent/runtime").glob("*") if p.suffix in (".py", ".json")]),
            "executor_hash": fingerprints([ROOT / p for p in ("scripts/evaluate_understanding.py", "scripts/understanding_runtime.py", "scripts/evaluate_agent.py", "cmd/evalfixture/main.go")]),
            "model_configuration_hash": hashlib.sha256(configuration.encode()).hexdigest(), "model": os.getenv("LLM_MODEL") if args.live else None,
            "provider_weight_version": "unknown", "analysis_date": data.analysis_date, "split": "diagnostic" if args.case_id else args.split,
            "case_ids": [c["id"] for c in selected]}


class Evaluation:
    def __init__(self, args, mode, rows):
        self.args, self.mode, self.rows = args, mode, rows
        self.processes, self.sessions = [], []
        self.root_connection, self.names = None, []
        self.prefix, self.password, self.key = secrets.token_hex(8), secrets.token_hex(24), secrets.token_hex(24)

    def __enter__(self):
        try:
            self.root_connection, self.names, sources = provision(self.prefix, self.rows)
            with socket.socket() as available:
                available.bind(("127.0.0.1", 0))
                port = available.getsockname()[1]
            self.probe = f"http://127.0.0.1:{port}"
            self.env = {**os.environ, "PYTHONPATH": str(ROOT / "agent") + os.pathsep + str(ROOT), "PYTHONUTF8": "1",
                        "SERVICE_KEY": self.key, "TENANT_DATASOURCES": json.dumps(sources), "EVAL_PREFIX": self.prefix,
                        "EVAL_PASSWORD": self.password, "AGENT_URL": self.probe, "EVAL_MAX_MODEL_CALLS": str(self.args.max_model_calls if self.mode == "full" else 0)}
            self.env.pop("EVAL_CLEANUP_ONLY", None)
            if self.mode == "rules_only":
                self.env.pop("LLM_API_KEY", None)
            self.log = tempfile.TemporaryFile()
            agent = subprocess.Popen([sys.executable, "-m", "uvicorn", "scripts.understanding_runtime:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"], cwd=ROOT, env=self.env, stdout=self.log, stderr=self.log, **HIDDEN)
            self.processes.append(agent)
            wait_health(self.probe, agent)
            fixture = subprocess.Popen([str(self.args.fixture_server.resolve())], env=self.env, stdout=subprocess.PIPE, stderr=self.log, text=True, **HIDDEN)
            self.processes.append(fixture)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                try:
                    origin = json.loads(pool.submit(fixture.stdout.readline).result(timeout=20))["api_url"]
                except Exception:
                    fixture.kill()
                    raise RuntimeError("fixture_start_failed") from None
            wait_health(origin, fixture)
            for tenant, role in (("ea", "admin"), ("eb", "admin"), ("ea", "ops")):
                session = httpx.Client(base_url=origin, headers={"Origin": origin}, timeout=5, trust_env=False)
                self.sessions.append(session)
                if session.post("/api/login", json={"tenant": tenant + "_" + self.prefix, "username": role, "password": self.password}).status_code != 200:
                    raise RuntimeError("fixture_login_failed")
            self.observer = httpx.Client(base_url=self.probe, headers={"X-Service-Key": self.key}, timeout=5, trust_env=False)
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def stats(self):
        response = self.observer.get("/evaluation/stats")
        response.raise_for_status()
        if response.json()["budget_exhausted"]:
            raise RuntimeError("model_budget_exhausted")
        return response.json()

    def wait(self, session, task_id):
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            self.stats()
            response = session.get("/api/tasks/" + task_id)
            if response.status_code != 200:
                return {"http_status": response.status_code, "status": "failed", "error": response.json().get("error", "")}
            task = response.json()
            if task["status"] not in ("pending", "querying"):
                return task
            time.sleep(.05)
        session.post("/api/tasks/" + task_id + "/stop", json={})
        return {"status": "failed", "error": "evaluation_timeout"}

    def execute(self, case):
        alpha, beta, ops = self.sessions
        blank = {"model_calls": 0, "sql_calls": 0, "attempts": 0, "parser": "unknown", "unknown_usage_calls": 0,
                 "known_input_tokens": 0, "known_output_tokens": 0, "known_total_tokens": 0, "stages_ms": {}, "error_code": ""}
        if case["check"] == "ops_summary":
            response = ops.get("/api/operations/summary")
            return {"http_status": response.status_code}, {**blank, "error_code": "permission" if response.status_code == 403 else ""}
        if case["check"].startswith("cross_"):
            response = alpha.post("/api/tasks", json={"question": "本月 GMV"})
            response.raise_for_status()
            task_id = response.json()["id"]
            own = self.wait(alpha, task_id)
            if own["status"] != "completed":
                raise RuntimeError("permission_setup_failed")
            suffix = {"cross_detail": "", "cross_events": "/events", "cross_stop": "/stop"}[case["check"]]
            response = beta.post("/api/tasks/" + task_id + suffix, json={}) if case["check"] == "cross_stop" else beta.get("/api/tasks/" + task_id + suffix)
            # 目标动作被拒绝；准备任务的合法查询另属 setup，要求规则零模型调用。
            trace = self.observer.get("/evaluation/tasks/" + task_id).json()
            if trace["model_calls"] != 0:
                raise RuntimeError("permission_setup_called_model")
            return {"http_status": response.status_code}, {**blank, "error_code": "permission" if response.status_code == 404 else ""}
        session = ops if case["identity"] == "ops" else alpha
        response = session.post("/api/tasks", json={"question": case["question"], **case["input"]})
        if response.status_code != 202:
            return {"http_status": response.status_code, "error": response.json().get("error", "")}, {**blank, "error_code": "permission" if response.status_code == 403 else "task_admission"}
        task_id = response.json()["id"]
        task = self.wait(session, task_id)
        observed = self.observer.get("/evaluation/tasks/" + task_id)
        if observed.status_code != 200:
            # 失联意味着用量无法完整观察，整份报告不能用于模型增益归因。
            raise RuntimeError("observation_missing")
        trace = {**blank, **observed.json()}
        self.stats()
        return task, trace

    def __exit__(self, *_):
        for session in self.sessions:
            session.close()
        if hasattr(self, "observer"):
            self.observer.close()
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
        if hasattr(self, "env"):
            cleared = subprocess.run([str(self.args.fixture_server.resolve())], env={**self.env, "EVAL_CLEANUP_ONLY": "true"}, capture_output=True, timeout=20, **HIDDEN)
            if cleared.returncode:
                raise RuntimeError("test_schema_cleanup_failed")
        if self.root_connection is not None:
            cleanup(self.root_connection, self.names)
        if hasattr(self, "log"):
            self.log.close()


def run(args):
    if not all(os.getenv(k) for k in ("TEST_DATABASE_URL", "MYSQL_TEST_HOST", "MYSQL_TEST_PASSWORD")):
        raise ValueError("explicit_test_databases_required")
    if args.live and not all(os.getenv(k) for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")):
        raise ValueError("live_model_configuration_required")
    if not 1 <= args.rounds <= 3 or not 1 <= args.max_model_calls <= 120:
        raise ValueError("invalid_rounds_or_budget")
    if args.details_dir and not args.details_dir.resolve().is_relative_to((ROOT / ".cache").resolve()):
        raise ValueError("details_must_be_in_ignored_cache")
    data = load()
    selected = [c.model_dump() for c in data.cases if args.split == "all" or c.split == args.split]
    if args.case_id:
        selected = [c for c in selected if c["id"] in args.case_id]
        if {c["id"] for c in selected} != set(args.case_id):
            raise ValueError("diagnostic_case_missing")
    identity = source_identity(args, data, selected)
    study = {"report_version": 1, "status": "incomplete", "mode": "paired_live" if args.live else "rules_only", "identity": identity,
             "created_at": datetime.now(timezone.utc).isoformat(), "rounds": []}
    save(args.output, study)
    rows = facts(ROOT / "evals/fixture.csv")
    for index in range(1, args.rounds + 1):
        pair = {}
        study["rounds"].append(pair)
        for mode in (("rules_only", "full") if args.live else ("rules_only",)):
            report = {"status": "incomplete", "mode": mode, "round": index, "identity": identity, "cases": [], "issued_calls": 0}
            pair[mode] = report
            with Evaluation(args, mode, rows) as evaluation:
                for case in selected:
                    started = time.perf_counter()
                    task, trace = evaluation.execute(case)
                    verdict = score(case, task, trace, rows)
                    record = {"id": case["id"], "category": case["category"], **verdict,
                              **{key: trace[key] for key in ("parser", "model_calls", "sql_calls", "attempts", "known_input_tokens", "known_output_tokens", "known_total_tokens", "unknown_usage_calls", "stages_ms", "error_code")},
                              "task_status": task.get("status", "denied"), "duration_ms": round((time.perf_counter() - started) * 1000, 3)}
                    report["cases"].append(record)
                    report["issued_calls"] = evaluation.stats()["issued_calls"]
                    save(args.output, study)
                    if args.details_dir:
                        save(args.details_dir / f"{index}-{mode}-{case['id']}.json", {"question": case["question"], "task": task, "verdict": verdict})
                    print(json.dumps({"round": index, "mode": mode, "id": case["id"], "passed": verdict["passed"], "parser": trace["parser"]}), flush=True)
            report["status"] = "complete"
            save(args.output, study)
    study["status"] = "complete"
    study["summary"] = summarize(study)
    save(args.output, study)
    return study


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-server", type=Path, required=True)
    parser.add_argument("--split", choices=("development", "acceptance", "all"), default="development")
    parser.add_argument("--live", action="store_true", help="显式启用真实模型同题对照")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--max-model-calls", type=int, default=120)
    parser.add_argument("--case-id", action="append", help="单题诊断，可重复指定；不能代替完整验收")
    parser.add_argument("--details-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args)
    except Exception:
        # 不输出可能含凭据、DSN 或提供商响应的异常正文。
        study = json.loads(args.output.read_text(encoding="utf-8")) if args.output.exists() else {"report_version": 1}
        study.update(status="incomplete")
        study.pop("summary", None)
        save(args.output, study)
        print("Understanding evaluation incomplete; no acceptance claim", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
