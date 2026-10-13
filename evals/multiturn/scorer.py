"""完整计划、独立 CSV 数值与持久状态分别判定；不排除失败轮。"""
from copy import deepcopy
from evals.oracle import compare
from evals.understanding.scorer import plan_diff

def score(turn,task,trace,conversation,mode,rows):
    result=task.get("result") or {}
    actual=task.get("status")
    denied=task.get("http_status") in (403,404) or actual=="failed" and trace.get("error_code")=="permission"
    status_ok=denied if turn["status"]=="denied" else actual==turn["status"]
    verdict={"passed":False,"status_correct":bool(status_ok),"plan_correct":None,"result_correct":None,"context_correct":None,"draft_correct":None,"differences":[],"inheritance_errors":[],"unsafe_execution":turn["status"]!="completed" and trace.get("sql_calls",0)>0}
    expected=turn["expected"]
    if turn["status"]=="completed" and isinstance(result.get("plan"),dict):
        try:
            differences=plan_diff(result["plan"],expected)
            verdict.update(plan_correct=not differences,differences=differences)
            verdict["inheritance_errors"]=[d["field"] for d in differences if d["field"] in turn["preserved_fields"]]
            numeric=deepcopy(result);numeric["plan"]=expected
            verdict["result_correct"]=compare(numeric,expected,rows)
        except (KeyError,TypeError,ValueError,ArithmeticError): verdict["result_correct"]=False
    if mode=="controlled":
        state=conversation.get("plan")
        try: verdict["context_correct"]=(state is None if expected is None else isinstance(state,dict) and not plan_diff(state,expected))
        except (KeyError,TypeError,ValueError): verdict["context_correct"]=False
        pending=conversation.get("pending") or {}
        draft_ok=set(pending.get("missing",[]))==set(turn["pending_missing"])
        for field,value in turn["pending_values"].items():
            found=(pending.get("initial") or {}).get(field)
            for op in pending.get("operations",[]):
                if op.get("op")=="set_"+field: found=op.get("value")
            draft_ok=draft_ok and found==value
        verdict["draft_correct"]=draft_ok
    answer_ok=verdict["plan_correct"] is True and verdict["result_correct"] is True if turn["status"]=="completed" else not verdict["unsafe_execution"]
    verdict["passed"]=bool(status_ok and answer_ok and (mode!="controlled" or verdict["context_correct"] and verdict["draft_correct"]))
    return verdict

def summarize(records):
    questions=[r for r in records if not r["action"]]
    successes=[r for r in questions if r["expected_status"]=="completed"]
    chains={r["chain"] for r in records}
    return {"turns":len(records),"question_turns":len(questions),"correct_turns":sum(r["passed"] for r in questions),"chains":len(chains),"correct_chains":sum(all(r["passed"] for r in records if r["chain"]==c) for c in chains),"expected_successes":len(successes),"observable_plans":sum(r["plan_correct"] is not None for r in successes),"correct_plans":sum(r["plan_correct"] is True for r in successes),"correct_results":sum(r["result_correct"] is True for r in successes),"inheritance_error_turns":sum(bool(r["inheritance_errors"]) for r in questions),"unsafe_executions":sum(r["unsafe_execution"] for r in questions),"model_calls":sum(r["model_calls"] for r in records),"known_tokens":sum(r["known_total_tokens"] for r in records),"unknown_usage_calls":sum(r["unknown_usage_calls"] for r in records)}
