"""同一 Python/PG 平台比较 A 单步、B 自适应、C 固定流程；默认零模型题集审计。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.analysis.dataset import ROOT as DATA, load
from evals.analysis.report import paired_difference, summary
from scripts.evaluate_analysis import evaluate
from scripts.python_analysis_stack import stack


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--with-model", action="store_true")
    p.add_argument("--split", choices=["development", "acceptance"], default="development")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--max-model-calls", type=int, default=0)
    p.add_argument("--output", type=Path, default=ROOT/".cache/python-analysis-study.json")
    args = p.parse_args()
    data = load()
    if not args.with_model:
        print(json.dumps({"mode": "dataset_audit", "platform": "python-langgraph-postgresql", "expressions": len(data["cases"]), "intent_groups": len({c["group"] for c in data["cases"]}), "annotation_status": data["annotation_status"], "model_calls": 0}))
        return
    if not 1 <= args.repeats <= 10 or not 1 <= args.max_model_calls <= 5000:
        p.error("repeats 1..10 and explicit max-model-calls 1..5000 required")
    required = ("TEST_DATABASE_URL", "LLM_API_KEY", "LLM_MODEL", "ANALYSIS_CURRENCY", "ANALYSIS_PRICE_VERSION", "ANALYSIS_INPUT_PRICE_MICROS", "ANALYSIS_OUTPUT_PRICE_MICROS", "ANALYSIS_MAX_COST_MICROS", "ANALYSIS_TOKENIZER")
    if not all(os.getenv(k) for k in required):
        p.error("explicit test PostgreSQL, model, tokenizer and real pricing required")
    paths = ["agent", "evals/analysis", "scripts/run_analysis_study.py", "scripts/evaluate_analysis.py", "scripts/python_analysis_stack.py", "scripts/python_analysis_eval_api.py", "scripts/python_analysis_eval_worker.py"]
    if subprocess.check_output(["git", "status", "--porcelain", "--", *paths], cwd=ROOT).strip():
        p.error("commit study code before live execution")
    study = dict(mode="live_http", platform="python-langgraph-postgresql", source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        annotation_status=data["annotation_status"], split=args.split, repeats=args.repeats, model=os.getenv("LLM_MODEL"), provider_weight_version="unknown",
        dataset_sha256=hashlib.sha256((DATA/"cases.json").read_bytes()).hexdigest(), runtime_lock_sha256=hashlib.sha256((ROOT/"agent/requirements.lock").read_bytes()).hexdigest(),
        pricing={k: os.getenv(k) for k in required if "PRICE" in k or "COST" in k or "CURRENCY" in k}, groups={})
    for group in ("baseline", "adaptive", "fixed"):
        results = []
        for fixture in ("orders.csv", "edge-orders.csv"):
            cases = [c for c in data["cases"] if c["split"] == args.split and c["fixture"] == fixture]
            if cases:
                with stack(fixture, "fixed" if group == "fixed" else "adaptive", args.max_model_calls) as (base, observer):
                    results.extend(evaluate(base, group, cases, args.repeats, observer, fixture))
            study["groups"][group] = {"results": results, "summary": summary(results)}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(study, ensure_ascii=False, indent=2), encoding="utf-8")
    study["paired_adaptive_minus_baseline"] = paired_difference(study["groups"]["baseline"]["results"], study["groups"]["adaptive"]["results"])
    args.output.write_text(json.dumps(study, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({g: v["summary"] for g, v in study["groups"].items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
