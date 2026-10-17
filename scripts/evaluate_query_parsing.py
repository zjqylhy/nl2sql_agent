"""用独立语义标注和 CSV 重算验证解析；SQLite 只替代执行，不代表 PostgreSQL 部署验收。"""
import argparse
from datetime import date
from decimal import Decimal
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agent"), str(ROOT)]

from evals.oracle import facts
from evals.understanding.dataset import Case
from evals.understanding.scorer import score
from runtime import app as runtime, telemetry
from runtime.model_transport import ModelTransport
from runtime.multiturn import QueryContext, source_metadata
from runtime.plan import QueryPlan
from runtime.security import validate_sql

DATASET = ROOT / "evals/understanding/parsing-cases.json"


def load_cases():
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    if data["version"] != 1 or data["annotation_status"] != "draft":
        raise ValueError("unsupported annotation version or status")
    identifiers = set()
    for item in data["cases"]:
        case = Case.model_validate(item["case"])
        if case.id in identifiers: raise ValueError("duplicate case")
        identifiers.add(case.id)
        if item["base"] is not None: QueryPlan.model_validate(item["base"])
    return data


def evaluate(*, live=False, rounds=1, max_model_calls=120):
    if not 1 <= rounds <= 3 or not 1 <= max_model_calls <= 120:
        raise ValueError("round or model call budget invalid")
    if live and not all(os.getenv(k) for k in ("LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL")):
        raise ValueError("live model configuration missing")
    data, rows = load_cases(), facts(ROOT / "evals/fixture.csv")
    source = runtime.Datasource(host="isolated-sqlite", database="parsing_fixture", user="fixture", password="unused", analysis_date=date(2018, 8, 31))
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.create_function("TO_CHAR", 2, lambda value, fmt: value[:7])
    connection.execute("CREATE TABLE metric_orders(order_status TEXT,item_amount REAL,delivery_days REAL,review_score REAL,customer_state TEXT,purchased_at TEXT)")
    connection.executemany("INSERT INTO metric_orders VALUES(?,?,?,?,?,?)", [
        (r["status"], *[str(r[k]) if r[k] is not None else None for k in ("amount", "delivery", "review")], r["state"], r["purchased_at"]) for r in rows])
    counters = {"issued": 0, "sql_calls": 0}
    original_post = ModelTransport.post

    def observed_post(transport, *args):
        if counters["issued"] >= max_model_calls:
            raise RuntimeError("model request budget exhausted")
        counters["issued"] += 1
        return original_post(transport, *args)

    def execute(source, sql, params):
        counters["sql_calls"] += 1
        validate_sql(sql, params, overflow_probe=sql.endswith(" LIMIT 1001"))
        cursor = connection.execute(sql, [str(v) if isinstance(v, Decimal) else v for v in params])
        return [dict(row) for row in cursor.fetchall()]

    records = []
    try:
        with patch.dict(os.environ), patch.object(runtime, "sources", lambda: {"alpha:admin": source, "alpha:ops": source}), \
                patch.object(runtime, "execute", execute), patch.object(ModelTransport, "post", observed_post):
            if not live: os.environ.pop("LLM_API_KEY", None)
            for round_number in range(1, rounds + 1):
                for item in data["cases"]:
                    if live and counters["issued"] >= max_model_calls:
                        return {"status": "incomplete", "reason": "model_budget", "scope": "python_parser_sqlite_csv",
                            "mode": "live", "model_calls": counters["issued"], "records": records}
                    case = Case.model_validate(item["case"])
                    base = QueryPlan.model_validate(item["base"]) if item["base"] is not None else None
                    context = QueryContext(binding=source_metadata(source, "alpha", case.identity)["binding"], version=1, plan=base) if base else None
                    allowed = ["gmv", "orders", "aov", "cancellation_rate"] if case.identity == "ops" else list(runtime.catalog().metrics)
                    request = runtime.QueryRequest(tenant_id="alpha", role=case.identity, allowed_metrics=allowed,
                        input={"question": case.question}, context=context)
                    counters["sql_calls"] = 0
                    # 复用实际入口的错误分类和遥测，不能由评测器替服务猜测权限错误。
                    response = runtime.query(request)
                    if isinstance(response, dict):
                        result = response
                        task = {"status": result["status"], "result": result}
                    else:
                        result = json.loads(response.body)
                        task = {"status": "denied" if response.status_code == 403 else "failed",
                            "http_status": response.status_code, "result": result}
                    trace = {**result["telemetry"], "sql_calls": counters["sql_calls"]}
                    verdict = score(case.model_dump(), task, trace, rows)
                    records.append({"round": round_number, "id": case.id, "category": case.category,
                        "expected_status": case.expected_status, "task_status": task["status"], **verdict,
                        "parser": trace["parser"], "model_calls": trace["model_calls"], "sql_calls": trace["sql_calls"],
                        "total_tokens": trace["total_tokens"], "error_code": trace["error_code"]})
    finally:
        connection.close()
    supported = [r for r in records if r["expected_status"] == "completed"]
    report = {"status": "complete", "scope": "python_parser_sqlite_csv", "mode": "live" if live else "rules_only",
        "rounds": rounds, "annotation_status": data["annotation_status"], "model_calls": counters["issued"],
        "records": records, "summary": {"cases": len(records), "passed": sum(r["passed"] for r in records),
            "supported": len(supported), "supported_completed_correctly": sum(r["passed"] for r in supported),
            "wrong_rule_direct": sum(r["parser"] == "rules" and r["task_status"] == "completed" and not r["passed"] for r in records),
            "supported_clarifications": sum(r["task_status"] == "needs_clarification" for r in supported),
            "model_failures": sum(r["model_calls"] > 0 and not r["passed"] for r in records)}}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--max-model-calls", type=int, default=120)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    telemetry.logger.setLevel(logging.WARNING)
    report = evaluate(live=args.live, rounds=args.rounds, max_model_calls=args.max_model_calls)
    report["source_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    paths = ["agent/runtime", "scripts/evaluate_query_parsing.py", str(DATASET.relative_to(ROOT)),
        "evals/understanding/scorer.py", "evals/oracle.py", "evals/fixture.csv"]
    report["source_dirty"] = bool(subprocess.check_output(["git", "diff", "HEAD", "--", *paths], cwd=ROOT) or
        subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "--", *paths], cwd=ROOT))
    report["dataset_sha256"] = hashlib.sha256(DATASET.read_bytes()).hexdigest()
    report["fixture_sha256"] = hashlib.sha256((ROOT / "evals/fixture.csv").read_bytes()).hexdigest()
    report["runtime_sha256"] = hashlib.sha256(b"".join(p.name.encode() + p.read_bytes()
        for p in sorted((ROOT / "agent/runtime").glob("*")) if p.suffix in (".py", ".json"))).hexdigest()
    report["model_configuration_sha256"] = hashlib.sha256("|".join(os.getenv(k, "")
        for k in ("LLM_BASE_URL", "LLM_MODEL", "LLM_PLAN_MAX_TOKENS")).encode()).hexdigest() if args.live else None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({k: v for k, v in report.items() if k not in ("records", "source_commit", "dataset_sha256")}, ensure_ascii=False))
    if report["status"] != "complete": raise SystemExit(1)


if __name__ == "__main__": main()
