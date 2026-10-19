"""真实 PG 核对手动重试的上下文、重复提交与原任务留存；模型全部模拟。"""
import json
from uuid import uuid4

import httpx
import pytest
from psycopg.types.json import Jsonb

from agent.runtime import app as runtime
from agent.runtime.model_transport import ModelTransport
from agent.tests.test_python_platform import platform
from agent.tests.test_push import bundle, push_platform, plan, run_once, answer_body
from agent.tests.test_exploration import contract


def model(monkeypatch, responses):
    calls = []
    def respond(request):
        calls.append(json.loads(request.content))
        result = responses[len(calls)-1]
        if isinstance(result, int):
            return httpx.Response(result, json={'error':{'code':'insufficient_quota'}})
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result)}}],
                                       'usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150}})
    monkeypatch.setattr(ModelTransport,'client',lambda self:httpx.Client(transport=httpx.MockTransport(respond)))
    return calls


def failed(client, repo):
    task=client.post('/api/tasks',json={'question':'查询 app_1 七月的发送量，观察24小时'}).json()
    run_once(repo)
    return client.get('/api/tasks/'+task['id']).json()


def retry_body(task):
    return {'request_id':str(uuid4()),'expected_execution_version':task['retry']['execution_version']}


def test_manual_retry_is_new_task_and_duplicate_click_does_not_send_twice(push_platform,monkeypatch):
    client,repo,_=push_platform
    calls=model(monkeypatch,[429,{'plan':plan(['sent']).model_dump(mode='json')}])
    old=failed(client,repo);assert old['status']=='failed' and old['retry']['available']
    assert '额度' in old['error'] and old['retry']['error_code']=='model_quota'
    url='/api/tasks/'+old['id']+'/retry';body=retry_body(old)
    first=client.post(url,json=body);assert first.status_code==202,first.text
    child=first.json();assert child['id']!=old['id'] and child['input']==old['input']
    assert client.post(url,json=body).json()['id']==child['id']
    with repo.db.transaction() as conn:
        assert conn.execute('SELECT count(*) n FROM tasks').fetchone()['n']==2
        before=conn.execute('SELECT telemetry FROM task_attempts WHERE task_id=%s',(old['id'],)).fetchone()
    run_once(repo)
    assert client.get('/api/tasks/'+child['id']).json()['status']=='completed'
    kept=client.get('/api/tasks/'+old['id']).json()
    for key in ['status','input','error','attempts','execution_version']:
        assert kept[key]==old[key]
    with repo.db.transaction() as conn:
        assert conn.execute('SELECT telemetry FROM task_attempts WHERE task_id=%s',(old['id'],)).fetchone()==before
        assert conn.execute("SELECT count(*) n FROM task_events WHERE task_id=%s AND kind='manual_retry'",(old['id'],)).fetchone()['n']==1
    assert len(calls)==2


def test_retry_keeps_answered_conditions_and_original_question(push_platform,monkeypatch):
    client,repo,_=push_platform
    calls=model(monkeypatch,[{'clarification':'请补充观察时长'},429,{'plan':plan(['sent']).model_dump(mode='json')}])
    old=failed(client,repo);assert old['status']=='needs_clarification'
    assert client.post('/api/tasks/'+old['id']+'/answers',json=answer_body(old,'24小时，统计2026年7月')).status_code==202
    run_once(repo);old=client.get('/api/tasks/'+old['id']).json();assert old['status']=='failed'
    child=client.post('/api/tasks/'+old['id']+'/retry',json=retry_body(old)).json()
    assert child['clarification']==old['clarification']
    run_once(repo);assert client.get('/api/tasks/'+child['id']).json()['status']=='completed'
    payload=json.loads(calls[-1]['messages'][1]['content'])
    assert payload['original_question']==old['input']['question']
    assert payload['clarifications']==old['clarification']['turns']


@pytest.mark.parametrize('guard',['pending','completed','sql','version','owner','binding','capacity'])
def test_retry_rechecks_status_owner_binding_version_and_capacity(push_platform,monkeypatch,guard):
    client,repo,source=push_platform;model(monkeypatch,[429])
    old=failed(client,repo);body=retry_body(old)
    if guard in {'pending','completed'}:
        with repo.db.transaction() as conn:conn.execute('UPDATE tasks SET status=%s WHERE id=%s',(guard,old['id']))
    elif guard=='sql':
        with repo.db.transaction() as conn:conn.execute('UPDATE task_attempts SET telemetry=%s WHERE task_id=%s',(Jsonb({'error_code':'sql_timeout'}),old['id']))
    elif guard=='version':body['expected_execution_version']+=1
    elif guard=='owner':
        client.post('/api/logout',json={});client.post('/api/login',json={'tenant':'demo','username':'ops','password':'fixture-password'})
    elif guard=='binding':
        changed=source.model_copy(update={'dataset_revision':'b'*64})
        monkeypatch.setattr(runtime,'sources',lambda:{'demo:admin':changed})
    elif guard=='capacity':assert client.post('/api/tasks',json={'question':'另一个尚未执行任务'}).status_code==202
    response=client.post('/api/tasks/'+old['id']+'/retry',json=body)
    assert response.status_code in {403,404,409,429},response.text
    with repo.db.transaction() as conn:
        assert conn.execute("SELECT count(*) n FROM task_events WHERE kind='manual_retry'").fetchone()['n']==0


def test_analysis_retry_preserves_confirmed_scope_answers_and_unknown_usage(push_platform,monkeypatch):
    client,repo,_=push_platform
    monkeypatch.setattr('agent.platform_app.graph.decide',lambda request:{'status':'error','error_code':'unknown_execution','model_sent':True,'message':'模型调用结果未知'})
    spec=contract().model_dump(mode='json')
    created=client.post('/api/analysis-runs',json={'question':spec['question'],'spec':spec,'request_id':str(uuid4())})
    assert created.status_code==202,created.text
    old_id=created.json()['id']
    with repo.db.transaction() as conn:
        conn.execute("UPDATE analysis_runs SET state=jsonb_set(state,'{answers}',%s) WHERE id=%s",(Jsonb(['只查 app_1，观察24小时']),old_id))
    run_once(repo);old=client.get('/api/analysis-runs/'+old_id).json()
    assert old['status']=='failed' and old['retry']['available']
    assert old['usage']['unknown_calls']==1
    body=retry_body(old)
    child=client.post('/api/tasks/'+old_id+'/retry',json=body)
    assert child.status_code==202,child.text
    assert client.post('/api/tasks/'+old_id+'/retry',json=body).json()['id']==child.json()['id']
    new=client.get('/api/analysis-runs/'+child.json()['id']).json()
    assert new['state']['spec']==old['state']['spec']
    assert new['state']['answers']==old['state']['answers']
    assert new['state']['model_calls']==0 and new['artifacts']==[]
    kept=client.get('/api/analysis-runs/'+old_id).json()
    assert kept['state']==old['state'] and kept['usage']==old['usage']
