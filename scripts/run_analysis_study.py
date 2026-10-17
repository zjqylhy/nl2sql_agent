"""同一 Python/PG 平台比较 A 单步、B 自适应、C 固定流程；默认零模型题集审计。"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.analysis.dataset import ROOT as DATA, load
from evals.analysis.oracle import expected
from evals.analysis.report import paired_difference, summary
from scripts.evaluate_analysis import evaluate
from scripts.python_analysis_stack import stack, StudyBudget


def source_identity():
    paths = ['agent', 'evals/analysis', 'scripts']
    if (ROOT/'.git').exists():
        if subprocess.check_output(['git', 'status', '--porcelain', '--', *paths], cwd=ROOT).strip():
            raise ValueError('commit_study_code_before_live_execution')
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        scope = 'git_checkout'
    else:
        # 部署者将已提交 archive 只读挂载到隔离容器；摘要绑定实际执行文件。
        commit = os.environ.get('STUDY_SOURCE_COMMIT', '')
        if not re.fullmatch('[0-9a-f]{40}', commit):
            raise ValueError('exported_commit_identity_required')
        scope = 'readonly_exported_commit'
    digest = hashlib.sha256()
    for path in sorted(p for name in paths for p in (ROOT/name).rglob('*') if p.is_file() and '__pycache__' not in p.parts and '.pytest_cache' not in p.parts):
        digest.update(path.relative_to(ROOT).as_posix().encode()+b'\0'+path.read_bytes()+b'\0')
    return {'source_commit': commit, 'source_scope': scope, 'execution_tree_sha256': digest.hexdigest()}


def select_cases(data, split, groups):
    selected = [c for c in data['cases'] if c['split'] == split]
    if groups:
        if set(groups) - {c['group'] for c in selected}:
            raise ValueError('intent_group_not_in_split')
        selected = [c for c in selected if c['group'] in groups]
    return selected


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--with-model", action="store_true")
    p.add_argument("--split", choices=["development", "acceptance"], default="development")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--max-model-calls", type=int, default=0)
    p.add_argument('--intent-group', action='append', help='预实验按完整意图组选题，不挑选某个成功改写')
    p.add_argument('--task-timeout', type=int, default=360)
    p.add_argument('--case-interval', type=int, default=20)
    p.add_argument("--output", type=Path, default=ROOT/".cache/python-analysis-study.json")
    args = p.parse_args()
    data = load()
    cases = select_cases(data, args.split, args.intent_group)
    if not args.with_model:
        print(json.dumps({"mode": "dataset_audit", "platform": "python-langgraph-postgresql", "expressions": len(data["cases"]), "intent_groups": len({c["group"] for c in data["cases"]}), "annotation_status": data["annotation_status"], "model_calls": 0}))
        return
    if not 1 <= args.repeats <= 10 or not 1 <= args.max_model_calls <= 5000:
        p.error("repeats 1..10 and explicit max-model-calls 1..5000 required")
    if not 30 <= args.task_timeout <= 600:
        p.error('task-timeout must be 30..600')
    if not 0 <= args.case_interval <= 60:
        p.error('case-interval must be 0..60')
    required = ("TEST_DATABASE_URL", "LLM_API_KEY", "LLM_MODEL", "ANALYSIS_CURRENCY", "ANALYSIS_PRICE_VERSION", "ANALYSIS_INPUT_PRICE_MICROS", "ANALYSIS_OUTPUT_PRICE_MICROS", "ANALYSIS_MAX_COST_MICROS", "ANALYSIS_TOKENIZER")
    if not all(os.getenv(k) for k in required):
        p.error("explicit test PostgreSQL, model, tokenizer and pricing or labeled test units required")
    budget = StudyBudget(args.max_model_calls)
    study = dict(mode="live_http", platform="python-langgraph-postgresql", **source_identity(),
        annotation_status=data["annotation_status"], split=args.split, repeats=args.repeats, model=os.getenv("LLM_MODEL"), provider_weight_version="unknown",
        experiment_kind=args.split+'_pilot' if args.intent_group else 'full_split_draft' if data['annotation_status']=='draft' else 'full_split',
        started_at=datetime.now(timezone.utc).isoformat(), case_interval_seconds=args.case_interval,
        case_ids=[c['id'] for c in cases], intent_groups=sorted({c['group'] for c in cases}), task_timeout_seconds=args.task_timeout,
        dataset_sha256=hashlib.sha256((DATA/"cases.json").read_bytes()).hexdigest(), runtime_lock_sha256=hashlib.sha256((ROOT/"agent/requirements.lock").read_bytes()).hexdigest(),
        pricing={k: os.getenv(k) for k in required if "PRICE" in k or "COST" in k or "CURRENCY" in k}, groups={}, completed=False,
        policy={k:v for k,v in os.environ.items() if k in {'ANALYSIS_MODEL_MAX_OUTPUT_TOKENS','ANALYSIS_MODEL_TIMEOUT_SECONDS','ANALYSIS_MAX_TOKENS','ANALYSIS_MODEL_INTERVAL_SECONDS','LLM_PLAN_MAX_TOKENS','LLM_INTENT_MAX_TOKENS'}},
        baseline_transport={'plan_max_tokens':int(os.getenv('LLM_PLAN_MAX_TOKENS','512')), 'client_segment_timeout_seconds':20})
    def save():
        study['provider_call_budget'] = {'maximum': budget.maximum, 'issued': budget.issued, 'remaining': budget.remaining, 'scope': 'whole_study_across_all_stopped_stacks'}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix('.next')
        temporary.write_text(json.dumps(study, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(args.output)
    for group in ("baseline", "adaptive", "fixed"):
        results = []
        calls_before_group = budget.issued
        def record(result):
            results.append(result)
            study['groups'][group] = {'results': results, 'summary': summary(results)}
            save()
        for fixture in ("orders.csv", "edge-orders.csv"):
            batch = [c for c in cases if c['fixture'] == fixture]
            if batch and budget.remaining:
                try:
                    with stack(fixture, "fixed" if group == "fixed" else "adaptive", args.max_model_calls, budget=budget) as (base, observer):
                        evaluate(base, group, batch, args.repeats, observer, fixture, on_result=record, task_timeout=args.task_timeout, case_interval=args.case_interval)
                finally:
                    save()
            elif batch:
                for repeat in range(args.repeats):
                    for case in batch:
                        gold = expected(DATA/fixture, case['spec']) if case.get('spec') else {'complete':False}
                        record(dict(id=case['id'], repeat=repeat, intent_group=case['group'], category=case['category'], fixture=fixture,
                                    status='not_run', reason='whole_study_budget_exhausted', expected_complete=gold['complete'],
                                    complete_success=False, behavior_correct=False, cost_known=False))
            study["groups"][group] = {"results": results, "summary": summary(results), 'provider_calls_issued':budget.issued-calls_before_group}
            save()
    study["paired_adaptive_minus_baseline"] = paired_difference(study["groups"]["baseline"]["results"], study["groups"]["adaptive"]["results"])
    study['completed'] = True
    study['finished_at'] = datetime.now(timezone.utc).isoformat()
    save()
    print(json.dumps({g: v["summary"] for g, v in study["groups"].items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
