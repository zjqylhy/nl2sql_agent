"""离线固定工具核对或显式启用真实 HTTP 对照；不把 fixture 成功当作模型准确率。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlparse
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.analysis.dataset import load, ROOT as DATA
from evals.analysis.oracle import expected, score
from evals.analysis.report import summary


def parameter_correct(step, case, gold):
    if step.get("status") != "completed" and step.get('error_code') not in {'sql_timeout','sql_connection','sql_unknown'}:
        return False
    if step.get("action") != "query_metrics":
        return True
    p = (step.get("arguments") or {}).get("plan", {})
    spec = case["spec"]
    if p.get("time_range") not in [spec["current"], spec.get("baseline")]:
        return False
    if not set(p.get("metrics", [])) <= {"gmv", "orders", "aov"} or p.get("dimensions", []) not in [[], ["state"]]:
        return False
    if spec["goal"] == "compare_declines" and p.get("top_n") is not None:
        return False
    key = lambda f: json.dumps(f, sort_keys=True)
    base, actual = set(map(key, spec.get("filters", []))), set(map(key, p.get("filters", [])))
    if not base <= actual:
        return False
    for extra in actual-base:
        f = json.loads(extra)
        if f.get("field") != "state" or f.get("op") != "in" or sorted(f["values"]) != sorted(gold.get("states", [])):
            return False
    return True


def spec_correct(actual, expected):
    if not actual or not expected:
        return False
    for name,default in [("goal",None),("current",None),("baseline",None),("ranking",None),("n",3)]:
        if actual.get(name,default)!=expected.get(name,default):
            return False
    key=lambda f:json.dumps(f,sort_keys=True)
    return sorted(map(key,actual.get("filters",[])))==sorted(map(key,expected.get("filters",[]))) and set(actual.get("checks",[]))==set(expected.get("checks",[]))


def baseline_complete(task, spec, gold):
    # 仅前一州且无附加核查时，原查询的一行本身已是所需合计，不替基线补算。
    if not spec or spec['goal']!='top_n_total' or spec['n']!=1 or spec.get('checks') or task.get('status')!='completed':
        return False
    result=task.get('result') or {}
    rows=result.get('rows',[])
    plan=result.get('plan',{})
    if len(rows)!=1 or plan.get('time_range')!=spec['current'] or plan.get('top_n')!=1 or plan.get('order_by')!=[{'field':'gmv','direction':'desc'}]:
        return False
    state=rows[0].get('dimensions',{}).get('state')
    return parameter_correct({'status':'completed','action':'query_metrics','arguments':{'plan':plan}}, {'spec':spec},gold) and score({'complete':True,'total':{'states':[state],'value':rows[0].get('values',{}).get('gmv',{}).get('value')},'extras':{}},gold)


def baseline_observation(observer, task_id):
    """观测全部尝试，不能只取最终成功响应中的 usage。"""
    if not observer:
        raise ValueError("baseline_requires_observer_url")
    response = observer.get('/evaluation/tasks/'+task_id)
    response.raise_for_status()
    trace = response.json()
    input_price, output_price = [int(os.environ[k]) for k in ('ANALYSIS_INPUT_PRICE_MICROS','ANALYSIS_OUTPUT_PRICE_MICROS')]
    cost = (trace.get('known_input_tokens',0)*input_price+trace.get('known_output_tokens',0)*output_price+999999)//1000000
    return {'known_tokens':trace.get('known_total_tokens',0),'unknown_calls':trace.get('unknown_usage_calls',0),
            'known_cost_micros':cost,'cost_known':trace.get('unknown_usage_calls',0)==0,
            'model_calls':trace['model_calls'],'sql_calls':trace['sql_calls'],'attempts':trace['attempts']}


def evaluate(base, group, cases, repeats, observer_url=None, fixture='orders.csv'):
    import httpx
    if urlparse(base).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("dedicated_loopback_test_stack_required")
    results = []
    if observer_url and urlparse(observer_url).hostname not in {'127.0.0.1','localhost','::1'}:
        raise ValueError('dedicated_loopback_observer_required')
    observer = httpx.Client(base_url=observer_url, headers={'X-Service-Key':os.environ['SERVICE_KEY']},timeout=5,trust_env=False) if observer_url else None
    if group=='baseline' and not observer:
        raise ValueError('baseline_requires_observer_url')
    try:
        return evaluate_authenticated(base, group, cases, repeats, observer, fixture)
    finally:
        if observer:
            observer.close()


def evaluate_authenticated(base, group, cases, repeats, observer, fixture):
    import httpx
    results=[]
    with httpx.Client(base_url=base, headers={"Origin": base.rstrip("/")}, timeout=40, follow_redirects=False, trust_env=False) as client:
        auth = {"tenant": os.environ["ANALYSIS_EVAL_TENANT"], "username": os.environ["ANALYSIS_EVAL_USER"], "password": os.environ["ANALYSIS_EVAL_PASSWORD"]}
        response = client.post("/api/login", json=auth)
        response.raise_for_status()
        for repeat in range(repeats):
            for case in cases:
                spec = case.get("spec")
                gold = expected(DATA / case["fixture"], spec) if spec else {"complete": False}
                if case["fixture"] != fixture:
                    results.append({"id": case["id"], "repeat": repeat, "status": "not_run", "reason": "requires_separate_edge_datasource", "expected_complete": gold["complete"]})
                    continue
                started = time.monotonic()
                result = {"id": case["id"], "repeat": repeat, "expected_complete": gold["complete"], "status": "failed", "complete_success": False, "behavior_correct": False, "cost_known": False}
                run = None
                try:
                    endpoint = "/api/tasks" if group == "baseline" else "/api/analysis-runs"
                    body = {"question": case["question"]}
                    if group != "baseline":
                        body["request_id"] = str(uuid.uuid4())
                    response = client.post(endpoint, json=body)
                    if response.status_code != 202:
                        result.update(http_status=response.status_code, duration_ms=(time.monotonic()-started)*1000)
                        results.append(result)
                        continue
                    run = response.json()
                    answers = iter(case.get("answers", []))
                    deadline = time.monotonic()+240
                    while time.monotonic() < deadline:
                        response = client.get(endpoint+"/"+run["id"])
                        response.raise_for_status()
                        run = response.json()
                        if run["status"] == "needs_clarification":
                            answer = next(answers, None)
                            if group != "baseline" and answer:
                                response = client.post(endpoint+"/"+run["id"]+"/answers", json={"request_id": str(uuid.uuid4()), "expected_version": run["version"], "answer": answer})
                                response.raise_for_status()
                                run = response.json()
                            else:
                                break
                        elif run["status"] not in {"pending", "querying"}:
                            break
                        time.sleep(.25)
                    else:
                        client.post("/api/tasks/"+run["id"]+"/stop", json={})
                        result["status"] = "timeout"
                    result["duration_ms"] = (time.monotonic()-started)*1000
                    if group != "baseline":
                        strategy = run.get("policy", {}).get("strategy") or "adaptive"
                        if strategy != group:
                            client.post("/api/tasks/"+run["id"]+"/stop", json={})
                            raise ValueError("stack_strategy_does_not_match_group")
                        report = next((a["data"] for a in run.get("artifacts", []) if a["kind"] == "report"), None)
                        result["complete_success"] = run["status"] == "completed" and spec_correct(run.get("state",{}).get("spec"),spec) and score(report, gold)
                        proposals = [s for s in run.get("steps", []) if s["phase"] == "tool"]
                        invalid = sum(s["phase"] == "decide" and s.get("error_code") in {"invalid_arguments", "model_format"} for s in run.get("steps", []))
                        result.update(tool_proposals=len(proposals)+invalid, correct_parameters=sum(parameter_correct(s, case, gold) for s in proposals) if spec else sum(s["action"] == "request_clarification" for s in proposals),
                                      extra_calls=sum(bool(s.get("cached")) or not parameter_correct(s,case,gold) for s in proposals) if spec else None,
                                      calls_above_minimum=max(0,len(proposals)-case.get("minimum_tools",len(proposals))) if gold["complete"] else None,
                                      known_tokens=run.get("usage", {}).get("known_tokens", 0), known_cost_micros=run.get("usage", {}).get("known_cost_micros", 0),
                                      unknown_calls=run.get("usage", {}).get("unknown_calls", 0), cost_known=run.get("usage", {}).get("unknown_calls", 0) == 0,
                                      reserved_and_known_cost_micros=run.get('state',{}).get('cost',0),
                                      sql_calls=run.get("state", {}).get("sql_calls"), model_calls=run.get("state", {}).get("model_calls"))
                        result['narrative_fallback']=report.get('narrative_fallback') if report else None
                        if case['category']=='clarification':
                            clarify_index=next((i for i,s in enumerate(proposals) if s['action']=='request_clarification'),None)
                            query_index=next((i for i,s in enumerate(proposals) if s['action']=='query_metrics'),None)
                            result['complete_success'] &= clarify_index is not None and (query_index is None or clarify_index<query_index)
                    else:
                        result.update(baseline_observation(observer,run['id']))
                        result['complete_success']=baseline_complete(run,spec,gold)
                        # 单步返回多行不等于已经完成合计或两期核查，不能由判分器替它补算。
                        result['tool_proposals']=result['sql_calls']
                        plan=(run.get('result') or {}).get('plan')
                        result['correct_parameters']=int(bool(plan) and bool(spec) and parameter_correct({'status':'completed','action':'query_metrics','arguments':{'plan':plan}},case,gold))
                    result["status"] = result.get("status") if result.get("status") == "timeout" else run["status"]
                    result["behavior_correct"] = result["complete_success"] if gold["complete"] else run["status"] in {"needs_clarification", "failed"}
                    if case.get("requires_no_sql") and result.get('sql_calls',run.get("state",{}).get("sql_calls",0))>0:
                        result["behavior_correct"]=False
                    if run["status"] == "needs_clarification":
                        client.post("/api/tasks/"+run["id"]+"/stop", json={})
                    results.append(result)
                except httpx.HTTPError as exc:
                    result.update(status='failed', complete_success=False, behavior_correct=False,
                                  error_code=type(exc).__name__, cost_known=False,
                                  duration_ms=(time.monotonic()-started)*1000)
                    if run and run.get('id'):
                        try:
                            client.post('/api/tasks/'+run['id']+'/stop', json={})
                        except httpx.HTTPError:
                            pass
                    results.append(result)
    by_id={c['id']:c for c in cases}
    for result in results:
        case=by_id[result['id']]
        result.update(category=case['category'],intent_group=case['group'],fixture=case['fixture'])
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=["baseline", "adaptive", "fixed"], default="adaptive")
    parser.add_argument("--base-url", default="http://localhost:8088")
    parser.add_argument("--split", choices=["development", "acceptance"], default="development")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--with-model", action="store_true")
    parser.add_argument('--observer-url', help='baseline 必须提供本机 analysis_eval_runtime 的地址')
    parser.add_argument("--output", type=Path, default=ROOT/".cache/analysis-evaluation.json")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error("repeats must be 1..10")
    dataset = load()
    cases = [c for c in dataset["cases"] if c["split"] == args.split]
    if args.with_model:
        if subprocess.check_output(['git','status','--porcelain','--','agent/runtime','internal','cmd','evals/analysis','scripts/evaluate_analysis.py','scripts/analysis_eval_runtime.py'],cwd=ROOT).strip():
            raise ValueError('commit_evaluation_code_before_live_run')
        results = evaluate(args.base_url, args.group, cases, args.repeats, args.observer_url)
    else:
        # 默认只检查数据与独立黄金预期，不产生模型请求或虚假的成功率。
        for c in cases:
            if c.get("spec"):
                expected(DATA/c["fixture"], c["spec"])
        results = [{"id": c["id"], "status": "not_run", "reason": "model_not_enabled"} for c in cases]
    report = {"version": 1, "group": args.group, "mode": "live_http" if args.with_model else "dataset_audit",
              "annotation_status": dataset["annotation_status"], "dataset_sha256": hashlib.sha256((DATA/"cases.json").read_bytes()).hexdigest(),
              "code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(), "summary": summary(results), "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"mode": report["mode"], "annotation_status": report["annotation_status"], "summary": report["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
