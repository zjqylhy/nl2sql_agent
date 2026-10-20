"""模型决定动作，服务端保管已确认约定；兼容省略生成字段而不接纳改写。"""
import json
from uuid import uuid4

import httpx
import pytest

from agent.runtime.model_transport import ModelTransport
from agent.tests.test_exploration import contract
from agent.tests.test_open_tasks import model_reply
from agent.tests.test_push import bundle, push_platform, run_once
from agent.tests.test_python_platform import platform


@pytest.mark.parametrize('shape',['action_only','omit_generated_fields'])
def test_confirmed_contract_is_server_owned_and_complete_analysis_still_works(push_platform,monkeypatch,shape):
    client,repo,_=push_platform;calls=[]
    def reply(request):
        payload=json.loads(json.loads(request.content)['messages'][1]['content'])
        calls.append(payload);n=len(calls)
        if n<=2:
            action={'action':'query_metrics','arguments':{'period':['baseline','current'][n-1],'metrics':['click_rate']}}
        elif n==3:
            action={'action':'compare_periods','arguments':{'baseline_ref':'e1','current_ref':'e2'}}
        else:
            action={'action':'finish','arguments':{'evidence_refs':['e1','e2','e3'],
                'answers':[{'item_id':'a1','status':'answered','text':'已比较整体变化，原因仍待验证','evidence_refs':['e3']}],
                'claims':[{'result_ref':'e3','row':0,'metric':'click_rate','field':'delta'}]}}
        result={'action':action}
        if shape=='omit_generated_fields':
            spec=payload['contract'];spec.pop('exploration_permissions');spec.pop('capability_version')
            result['spec']=spec
        return model_reply(result)
    monkeypatch.setattr(ModelTransport,'bounded_client',lambda self,**_:httpx.Client(transport=httpx.MockTransport(reply)))
    task=client.post('/api/analysis-runs',json={'question':'核查点击率变化','request_id':str(uuid4()),'spec':contract().model_dump(mode='json')}).json()
    run_once(repo);run=client.get('/api/analysis-runs/'+task['id']).json()
    assert run['status']=='completed',run
    assert len(calls)==run['state']['model_calls']==4 and run['state']['sql_calls']==2
    report=next(a['data'] for a in run['artifacts'] if a['kind']=='report')
    assert report['contract']['scope']==contract().model_dump(mode='json')['scope']
    assert report['contract']['exploration_permissions']['metrics']


@pytest.mark.parametrize('field',['scope','permissions','version','targets','answers','null_spec'])
def test_model_cannot_replace_confirmed_business_or_generated_fields(push_platform,monkeypatch,field):
    client,repo,_=push_platform;calls=[]
    def reply(request):
        payload=json.loads(json.loads(request.content)['messages'][1]['content']);spec=payload['contract'];calls.append(payload)
        if field=='scope':spec['scope']['filters']=[{'field':'app_id','values':['app_2']}]
        elif field=='permissions':spec['exploration_permissions']['metrics']=['sent']
        elif field=='version':spec['capability_version']='push-other'
        elif field=='targets':spec['target_metrics']=['sent']
        elif field=='answers':spec['answer_items'][0]['text']='替换用户问题'
        else:spec=None
        return model_reply({'spec':spec,'action':{'action':'query_metrics','arguments':{'period':'current','metrics':['click_rate']}}})
    monkeypatch.setattr(ModelTransport,'bounded_client',lambda self,**_:httpx.Client(transport=httpx.MockTransport(reply)))
    task=client.post('/api/analysis-runs',json={'question':'核查点击率变化','request_id':str(uuid4()),'spec':contract().model_dump(mode='json')}).json()
    run_once(repo);run=client.get('/api/analysis-runs/'+task['id']).json()
    assert run['status']=='failed' and run['state']['sql_calls']==0
    assert len(calls)==2 and run['retry']['available']
