"""评测器必须抓住查询成功但继承条件错误的情况。"""
import json
from copy import deepcopy
from pathlib import Path
import pytest
from evals.multiturn.dataset import load, Dataset
from evals.multiturn.scorer import score

def test_chain_partition_and_explicit_context_contract():
    data=load()
    assert len(data.chains)==30 and sum(c.category=="unfamiliar" for c in data.chains)==10
    bad=data.model_dump();bad["chains"][0]["turns"][1]["base"]=None
    with pytest.raises(ValueError): Dataset.model_validate(bad)

def test_executable_wrong_plan_and_unsafe_clarification_are_failures():
    turn=load().chains[0].turns[1].model_dump()
    wrong=deepcopy(turn["expected"]);wrong["metrics"]=["orders"]
    verdict=score(turn,{"status":"completed","result":{"plan":wrong}},{"sql_calls":1},{"plan":wrong},"controlled",[])
    assert not verdict["passed"] and "metrics" in verdict["inheritance_errors"]
    turn=load().chains[0].turns[2].model_dump()
    verdict=score(turn,{"status":"needs_clarification"},{"sql_calls":1},{"plan":turn["expected"]},"controlled",[])
    assert not verdict["passed"] and verdict["unsafe_execution"]


def test_report_rejects_dropped_turns_and_fabricated_usage():
    from evals.multiturn.report import validate
    from evals.multiturn.scorer import summarize
    from evals.understanding.report import digest
    from evals.multiturn.dataset import DATASET
    data=load();records=[]
    for mode in ('independent','controlled'):
        for c in data.chains:
            for n,t in enumerate(c.turns,1):
                records.append({'mode':mode,'chain':c.id,'turn':n,'action':t.action,'expected_status':t.status,'passed':False,'plan_correct':None,'result_correct':None,'inheritance_errors':[],'unsafe_execution':False,'model_calls':0,'unknown_usage_calls':0,'known_total_tokens':0})
    study={'report_version':1,'status':'complete','identity':{'source_commit':'a'*40,'dataset_hash':digest(DATASET),'annotation_status':'draft','reviewer':None,'split':'all'},'chain_ids':[c.id for c in data.chains],'rounds':[{'round':1,'status':'complete','records':records,'issued_calls':0,'summary':{m:summarize([r for r in records if r['mode']==m]) for m in ('independent','controlled')}}]}
    validate(study)
    bad=deepcopy(study);bad['rounds'][0]['records'].pop()
    with pytest.raises(ValueError):validate(bad)
    bad=deepcopy(study);bad['rounds'][0]['issued_calls']=1
    with pytest.raises(ValueError):validate(bad)
