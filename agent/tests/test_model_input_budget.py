"""输入、整任务预留、旧任务兼容与常见分析上下文使用同一冻结预算。"""
from copy import deepcopy
from datetime import date
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from agent.platform_app.policy import Policy
from agent.runtime import telemetry
from agent.runtime.analysis import artifact
from agent.runtime.app import QueryRequest
from agent.runtime.exploration import prompt,tool
from agent.runtime.model_budget import DEFAULT_INPUT_BYTES,input_bytes
from agent.runtime.model_transport import ModelTransport
from agent.runtime.push import DEFINITIONS,result_for
from agent.runtime.push_query import parse_query
from agent.tests.test_exploration import contract,evidence,request
from agent.tests.test_initial_context import DIALOGUE
from agent.tests.test_model_retry import model
from agent.tests.test_open_tasks import model_reply
from agent.tests.test_push import push_platform,bundle,run_once
from agent.tests.test_python_platform import platform,POLICY
from agent.tests.test_query_program import source


def test_policy_defaults_for_new_tasks_and_legacy_frozen_policy(monkeypatch):
    for name,value in {'ANALYSIS_ENABLED':'true','ANALYSIS_CURRENCY':'test','ANALYSIS_PRICE_VERSION':'v1','LLM_MODEL':'fixture',
        'ANALYSIS_TOKENIZER':'byte_bpe_upper_bound','ANALYSIS_INPUT_PRICE_MICROS':'1000000',
        'ANALYSIS_OUTPUT_PRICE_MICROS':'1000000','ANALYSIS_MAX_COST_MICROS':'1000000','ANALYSIS_MODEL_MAX_OUTPUT_TOKENS':'4096'}.items():
        monkeypatch.setenv(name,value)
    monkeypatch.delenv('ANALYSIS_MAX_INPUT_BYTES',raising=False);monkeypatch.delenv('ANALYSIS_MAX_TOKENS',raising=False)
    new=Policy.from_env()
    assert new.max_input_bytes==32768 and new.max_tokens==400000
    assert new.reserved_tokens==36864 and 10*new.reserved_tokens<=new.max_tokens
    old=POLICY.model_dump();old.pop('max_input_bytes')
    assert Policy.model_validate(old).max_input_bytes==6000
    monkeypatch.setenv('ANALYSIS_MAX_COST_MICROS','100')
    with pytest.raises(ValueError,match='不足以预留'):Policy.from_env()


@pytest.mark.parametrize('excess',[0,1])
def test_new_initial_input_boundary_and_zero_send_on_oversize(push_platform,monkeypatch,excess):
    _,_,src=push_platform
    monkeypatch.setattr('agent.runtime.push_query.initial_messages',lambda *_:('a'*100,'b'*(32412+excess)))
    calls=model(monkeypatch,[{'clarification':'请确认日期'}])
    req=QueryRequest(tenant_id='demo',role='admin',allowed_metrics=['sent'],input={'question':'查询发送'},initial_max_input_bytes=32768)
    with telemetry.capture(None) as trace:
        if excess:
            with pytest.raises(HTTPException,match='未调用模型'):parse_query(req,src)
        else:assert parse_query(req,src)['status']=='needs_clarification'
    assert trace['model_calls']==len(calls)==(0 if excess else 1)


def normal_context(direction_count,read_limit=3):
    spec=contract();items=[evidence('baseline'),evidence('current')]
    metrics=['sent','delivered','clicked','failed','unknown_delivery','delivery_rate','click_rate']
    for dimension in ['channel','manufacturer','template_id','failure_reason','day'][:direction_count]:
        for period in ['baseline','current']:
            plan=spec.plan_for(period,metrics,[dimension])
            raw=[{'d_'+dimension:'组_'+str(i),'c_sent':1000,'c_delivered':600,'c_clicked':40,'c_failed':100,'c_unknown_delivery':300} for i in range(20)]
            data=result_for(plan,raw,source(),{},'',[]);data['period_name']=period
            items.append(artifact('query',data,data['metrics_used'],'a'*64))
    if read_limit:
        item=items[-1]
        items.append(tool(request({'action':'inspect_result','arguments':{'result_ref':item['result_ref'],'limit':read_limit}},items),source())['artifact'])
    return SimpleNamespace(spec=spec,question=spec.question,answers=[],allowed_metrics=list(DEFINITIONS),artifacts=items,
        feedback={},budget={'model_calls':2,'actions':2,'sql_calls':1},max_input_bytes=DEFAULT_INPUT_BYTES)


@pytest.mark.parametrize('directions,read_limit',[(0,0),(0,1),(1,3),(2,10),(3,20),(4,20)])
def test_normal_comparison_grouping_and_reading_contexts_fit_new_budget(directions,read_limit):
    req=normal_context(directions,read_limit);original=deepcopy(req.artifacts)
    system,user=prompt(req,date(2026,10,14));body=json.loads(user)
    assert input_bytes(system,user)<=32768
    assert len(body['evidence'])==len(req.artifacts)
    assert req.artifacts==original
    if read_limit:assert body['evidence'][-1]['preview']


def test_long_original_question_and_two_clarifications_are_not_truncated(push_platform,monkeypatch):
    _,_,src=push_platform;question='查询发送量并保持所有筛选条件。'*100
    calls=model(monkeypatch,[{'clarification':'请补充发送期间'}])
    req=QueryRequest(tenant_id='demo',role='admin',allowed_metrics=list(DEFINITIONS),input={'question':question},
        initial_max_input_bytes=32768,push_dialogue=DIALOGUE)
    assert parse_query(req,src)['status']=='needs_clarification'
    system,user=[m['content'] for m in calls[0]['messages']]
    assert input_bytes(system,user)>6000 and input_bytes(system,user)<=32768
    assert json.loads(user)=={'original_question':question,'clarifications':DIALOGUE}


@pytest.mark.parametrize('entry',['analysis','unified'])
def test_new_input_budget_frozen_through_full_analysis_and_settlement(push_platform,monkeypatch,entry):
    client,repo,_=push_platform;calls=[]
    client.app.state.policy=POLICY.model_copy(update={'max_input_bytes':32768,'max_tokens':400000,'max_output_tokens':4096})
    def reply(http_request):
        value=json.loads(json.loads(http_request.content)['messages'][1]['content']);calls.append(value);n=len(calls)
        if n<=2:action={'action':'query_metrics','arguments':{'period':['baseline','current'][n-1],'metrics':['click_rate']}}
        elif n==3:action={'action':'compare_periods','arguments':{'baseline_ref':'e1','current_ref':'e2'}}
        else:action={'action':'finish','arguments':{'evidence_refs':['e1','e2','e3'],
            'answers':[{'item_id':'a1','status':'answered','text':'已核对整体比率变化，原因仍待业务验证','evidence_refs':['e3']}],
            'claims':[{'result_ref':'e3','row':0,'metric':'click_rate','field':'delta'}]}}
        response=model_reply({'action':action});body=json.loads(response.content)
        body['usage']={'prompt_tokens':9000,'completion_tokens':100,'total_tokens':9100}
        return httpx.Response(200,json=body)
    monkeypatch.setattr(ModelTransport,'bounded_client',lambda self,**_:httpx.Client(transport=httpx.MockTransport(reply)))
    def initial_reply(http_request):
        response=model_reply({'contract':contract().model_dump(mode='json')});body=json.loads(response.content)
        body['usage']={'prompt_tokens':9000,'completion_tokens':100,'total_tokens':9100}
        return httpx.Response(200,json=body)
    monkeypatch.setattr(ModelTransport,'client',lambda self:httpx.Client(transport=httpx.MockTransport(initial_reply)))
    if entry=='analysis':
        task=client.post('/api/analysis-runs',json={'question':'核对常见两期变化','request_id':str(uuid4()),'spec':contract().model_dump(mode='json')}).json()
    else:
        task=client.post('/api/tasks',json={'question':'核对常见两期变化并分析原因'}).json()
    client.app.state.policy=POLICY
    if entry=='unified':run_once(repo)
    run_once(repo);value=client.get('/api/analysis-runs/'+task['id']).json()
    assert value['status']=='completed',value
    count=4 if entry=='analysis' else 5
    assert value['policy']['max_input_bytes']==32768 and value['state']['tokens']==9100*count
    assert value['usage']['unknown_calls']==0
    with repo.db.transaction() as conn:
        rows=conn.execute('SELECT reserved_tokens,actual_tokens,status FROM analysis_budget_ledger WHERE run_id=%s',(task['id'],)).fetchall()
    assert len(rows)==count and all(r=={'reserved_tokens':36864,'actual_tokens':9100,'status':'settled'} for r in rows)
