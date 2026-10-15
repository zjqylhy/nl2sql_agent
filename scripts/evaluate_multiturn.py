"""同提交、同数据、同身份对照独立问句与受控会话，逐轮保存失败。"""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from evals.multiturn.dataset import DATASET,load
from evals.multiturn.scorer import score,summarize
from evals.oracle import facts
from evals.understanding.report import digest,fingerprints,save
from scripts.evaluate_understanding import Evaluation

FIELDS=("parser","model_calls","sql_calls","attempts","known_input_tokens","known_output_tokens","known_total_tokens","unknown_usage_calls","error_code")
BLANK={k:("unknown" if k=="parser" else "" if k=="error_code" else 0) for k in FIELDS}

def identity(args,data):
    paths=["agent/runtime","internal","cmd/evalfixture","evals/multiturn","evals/oracle.py","evals/fixture.csv","scripts/evaluate_multiturn.py","scripts/understanding_runtime.py","scripts/evaluate_understanding.py"]
    dirty=subprocess.check_output(["git","diff","HEAD","--",*paths],cwd=ROOT)
    untracked=subprocess.check_output(["git","ls-files","--others","--exclude-standard","--",*paths],cwd=ROOT)
    if dirty or untracked: raise ValueError("evaluation_code_must_be_committed")
    config="|".join(os.getenv(k,"") for k in ("LLM_BASE_URL","LLM_MODEL","LLM_PLAN_MAX_TOKENS"))
    return {"source_commit":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),"dataset_hash":digest(DATASET),"annotation_status":data.annotation_status,"reviewer":data.reviewer,"fixture_hash":digest(ROOT/"evals/fixture.csv"),"fixture_binary_hash":digest(args.fixture_server),"analysis_date":data.analysis_date,"split":args.split,"model_configuration_hash":hashlib.sha256(config.encode()).hexdigest(),"provider_weight_version":"unknown","runtime_hash":fingerprints(list((ROOT/"agent/runtime").glob("*.py"))),"scorer_hash":fingerprints([ROOT/p for p in ("evals/multiturn/scorer.py","evals/multiturn/dataset.py","evals/oracle.py")])}

def execute(evaluation,session,turn,mode,conversation_id):
    conversation={}
    if mode=="controlled":
        response=session.get("/api/conversations/"+conversation_id);response.raise_for_status()
        conversation=response.json()["conversation"]
    if turn["action"]:
        if mode=="controlled":
            response=session.post("/api/conversations/"+conversation_id+"/discard-pending",json={"expected_version":conversation["version"]})
            if response.status_code!=200: return {"http_status":response.status_code},BLANK,conversation
            conversation=response.json()
        return {"status":"discarded"},BLANK,conversation
    body={"question":turn["question"]}
    path="/api/tasks"
    if mode=="controlled":
        path="/api/conversations/"+conversation_id+"/turns"
        body.update(request_id=str(uuid.uuid4()),expected_version=conversation["version"])
    response=session.post(path,json=body)
    if response.status_code!=202:
        task={"http_status":response.status_code}
        trace={**BLANK,"error_code":"permission" if response.status_code in (403,404) else "admission"}
    else:
        task_id=response.json()["id"]
        task=evaluation.wait(session,task_id)
        observed=evaluation.observer.get("/evaluation/tasks/"+task_id)
        if observed.status_code!=200: raise RuntimeError("observation_missing")
        trace={**BLANK,**observed.json()}
    if mode=="controlled":
        response=session.get("/api/conversations/"+conversation_id);response.raise_for_status()
        conversation=response.json()["conversation"]
        if conversation["active_task_id"]: raise RuntimeError("terminal_conversation_slot_not_released")
    return task,trace,conversation

def run(args):
    if not all(os.getenv(k) for k in ("TEST_DATABASE_URL","MYSQL_TEST_HOST","MYSQL_TEST_PASSWORD")): raise ValueError("explicit_test_databases_required")
    if args.live and not all(os.getenv(k) for k in ("LLM_API_KEY","LLM_BASE_URL","LLM_MODEL")): raise ValueError("explicit_live_configuration_required")
    if not 1<=args.rounds<=3 or not 1<=args.max_model_calls<=120: raise ValueError("invalid_budget")
    if args.details_dir and not args.details_dir.resolve().is_relative_to((ROOT/".cache").resolve()): raise ValueError("details_must_be_ignored")
    data=load();selected=[c.model_dump() for c in data.chains if args.split=="all" or c.split==args.split]
    if args.chain_id:
        selected=[c for c in selected if c["id"] in args.chain_id]
        if len(selected)!=len(set(args.chain_id)): raise ValueError("unknown_chain")
    study={"report_version":1,"status":"incomplete","identity":identity(args,data),"chain_ids":[c["id"] for c in selected],"live":args.live,"created_at":datetime.now(timezone.utc).isoformat(),"rounds":[]}
    save(args.output,study);rows=facts(ROOT/"evals/fixture.csv")
    for index in range(1,args.rounds+1):
        pair={"round":index,"status":"incomplete","records":[],"issued_calls":0};study["rounds"].append(pair)
        # 两种模式共用一个观测代理，出站前硬限制本轮总请求而非各自限额。
        with Evaluation(args,"full" if args.live else "rules_only",rows) as evaluation:
            for mode in ("independent","controlled"):
                for chain in selected:
                    session=evaluation.sessions[2 if chain["identity"]=="ops" else 0]
                    conversation_id=None
                    if mode=="controlled":
                        response=session.post("/api/conversations",json={});response.raise_for_status();conversation_id=response.json()["id"]
                    for n,turn in enumerate(chain["turns"],1):
                        started=time.perf_counter()
                        task,trace,conversation=execute(evaluation,session,turn,mode,conversation_id)
                        verdict=score(turn,task,trace,conversation,mode,rows)
                        record={"chain":chain["id"],"category":chain["category"],"turn":n,"mode":mode,"action":turn["action"],"expected_status":turn["status"],"task_status":task.get("status","denied"),**verdict,**{k:trace[k] for k in FIELDS},"duration_ms":round((time.perf_counter()-started)*1000,3)}
                        pair["records"].append(record);pair["issued_calls"]=evaluation.stats()["issued_calls"];save(args.output,study)
                        if args.details_dir: save(args.details_dir/f"{index}-{mode}-{chain['id']}-{n}.json",{"task":task,"conversation":conversation,"verdict":verdict})
                    print(json.dumps({"round":index,"mode":mode,"chain":chain["id"],"correct":sum(r["passed"] for r in pair["records"] if r["mode"]==mode and r["chain"]==chain["id"]),"turns":len(chain["turns"])}),flush=True)
        pair["status"]="complete"
        pair["summary"]={m:summarize([r for r in pair["records"] if r["mode"]==m]) for m in ("independent","controlled")}
        save(args.output,study)
    study["status"]="complete";save(args.output,study)
    return study

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-server",type=Path,required=True)
    parser.add_argument("--split",choices=("development","acceptance","all"),default="development")
    parser.add_argument("--live",action="store_true")
    parser.add_argument("--rounds",type=int,default=1)
    parser.add_argument("--max-model-calls",type=int,default=120)
    parser.add_argument("--chain-id",action="append")
    parser.add_argument("--details-dir",type=Path)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    try:
        study=run(args);print(json.dumps({"status":study["status"],"summary":[r["summary"] for r in study["rounds"]]},ensure_ascii=False))
    except (ValueError,RuntimeError):
        print(json.dumps({"status":"incomplete","error":"evaluation_failed_check_local_diagnostics"}));raise

if __name__=="__main__": main()
