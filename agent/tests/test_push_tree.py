"""树分支、条件继承与有界上下文通过真实平台事务验证。"""
import json
from uuid import uuid4

import httpx
import psycopg
import pytest

from agent.tests.test_push import platform, bundle, push_platform, plan, run_once
from agent.runtime import app as runtime
from agent.runtime.model_transport import ModelTransport


def root(client,repo):
    task=client.post('/api/tasks',json={'question':'2026年7月各厂商推送表现，观察24小时','push_plan':plan(dimensions=['manufacturer']).model_dump(mode='json')}).json()
    run_once(repo)
    response=client.post('/api/push-threads',json={'task_id':task['id']})
    assert response.status_code==201,response.text
    return response.json()


def mock_model(monkeypatch,decisions):
    calls=[]
    def respond(request):
        messages=json.loads(request.content)['messages']
        payload=json.loads(messages[1]['content'])
        summary='previous_summary' in payload
        calls.append({'summary':summary,'payload':payload,'messages':messages})
        value={'summary':'已讨论推送查询与展示方式，详细条件见当前计划，原文可按消息引用取回。'} if summary else decisions.pop(0)
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value,ensure_ascii=False)}}],
                                       'usage':{'prompt_tokens':200,'completion_tokens':100,'total_tokens':300}})
    monkeypatch.setattr(ModelTransport,'client',lambda self:httpx.Client(transport=httpx.MockTransport(respond)))
    return calls


def send(client,tree,text):
    body={'question':text,'request_id':str(uuid4()),'expected_version':tree['version']}
    response=client.post('/api/push-threads/'+tree['id']+'/turns',json=body)
    assert response.status_code==202,response.text
    return response.json(),body


def get(client,tree):
    response=client.get('/api/push-threads/'+tree['id'])
    assert response.status_code==200,response.text
    return response.json()


def seek(client,tree,node):
    response=client.post('/api/push-threads/'+tree['id']+'/cursor',json={'expected_version':tree['version'],'message_id':node})
    assert response.status_code==200,response.text
    return response.json()


def test_tree_forks_and_rollback_preserve_old_results(push_platform,monkeypatch):
    client,repo,_=push_platform
    tree=root(client,repo); first=tree['cursor_id']; original=tree['messages']
    calls=mock_model(monkeypatch,[{'action':'query','patch':{'filters':[{'field':'manufacturer','values':['huawei']}]}},
                                 {'action':'query','patch':{'time_range':{'start':'2026-09-01T00:00:00+08:00','end_exclusive':'2026-10-01T00:00:00+08:00'}}}])
    tree,body=send(client,tree,'只看华为')
    assert client.post('/api/push-threads/'+tree['id']+'/turns',json=body).json()['active_task_id']==tree['active_task_id']
    run_once(repo); tree=get(client,tree); second=tree['cursor_id']
    assert tree['result']['plan']['filters'][0]['values']==['huawei']
    assert tree['result']['plan']['time_range']['start'].startswith('2026-07')
    second_result=tree['result']
    tree=seek(client,tree,first)
    assert len(tree['branches'])==1 and tree['result']['plan']['filters']==[]
    tree,_=send(client,tree,'只把时间改为9月')
    assert len(tree['branches'])==2
    run_once(repo); tree=get(client,tree)
    assert tree['result']['plan']['filters']==[]
    assert tree['result']['totals']['sent']['value']=='3600'
    assert second not in tree['path_ids']
    assert tree['messages'][:len(original)]==original
    payload=[c['payload'] for c in calls if not c['summary']][-1]
    assert payload['current_plan']['filters']==[]
    assert second not in json.dumps(payload)
    tree=seek(client,tree,second)
    assert tree['result']==second_result
    assert len([m for m in tree['messages'] if m['kind']=='answer'])==3
    with repo.db.transaction() as conn:
        with pytest.raises(psycopg.Error):
            conn.execute("UPDATE push_messages SET content='{}' WHERE id=%s",(first,))


def test_tree_presentation_reuses_evidence_and_raw_model_response(push_platform,monkeypatch):
    client,repo,_=push_platform
    tree=root(client,repo); original=tree['result']; first=tree['cursor_id']
    calls=mock_model(monkeypatch,[{'action':'present','presentation':{'layout':'table','detail':'detailed'}}])
    tree,_=send(client,tree,'改成表格，详细解释指标口径')
    claimed=run_once(repo); tree=get(client,tree)
    assert tree['result']['rows']==original['rows']
    assert tree['result']['presentation']=={'layout':'table','detail':'detailed'}
    assert tree['result']['metrics'][0]['definition'] in tree['messages'][-1]['content']['text']
    with repo.db.transaction() as conn:
        row=conn.execute('SELECT result,telemetry FROM tasks t JOIN task_attempts a ON a.task_id=t.id WHERE t.id=%s',(claimed['id'],)).fetchone()
        assert row['telemetry'].get('sql_calls')==0
        rev=conn.execute('SELECT * FROM push_revisions WHERE message_id=%s',(tree['cursor_id'],)).fetchone()
        old=conn.execute('SELECT * FROM push_revisions WHERE message_id=%s',(first,)).fetchone()
        assert rev['result_task_id']==old['result_task_id']
        audits=conn.execute("SELECT * FROM push_model_calls WHERE task_id=%s AND kind='decision'",(claimed['id'],)).fetchall()
        assert len(audits)==1 and audits[0]['response_content']
        assert audits[0]['parsed_decision']['action']=='present'
        assert 'Authorization' not in json.dumps(audits[0]['request_snapshot'])
    assert len([c for c in calls if not c['summary']])==1


def test_tree_clarification_cancel_and_optimistic_version(push_platform,monkeypatch):
    client,repo,_=push_platform
    tree=root(client,repo); first=tree['cursor_id']; oldversion=tree['version']
    mock_model(monkeypatch,[{'action':'clarify','message':'请确认改查的月份'}, {'action':'present','presentation':{'layout':'cards','detail':'brief'}}])
    tree,body=send(client,tree,'调整一下')
    url='/api/push-threads/'+tree['id']
    assert client.post(url+'/turns',json={**body,'request_id':str(uuid4())}).status_code==409
    assert client.post(url+'/cursor',json={'expected_version':tree['version'],'message_id':first}).status_code==409
    run_once(repo); tree=get(client,tree)
    assert tree['task']['status']=='needs_clarification'
    assert client.post(url+'/cursor',json={'expected_version':tree['version'],'message_id':first}).status_code==409
    task=tree['task']; answer={'answer':'改成卡片展示','request_id':str(uuid4()),'expected_version':task['clarification']['version']}
    assert client.post('/api/tasks/'+task['id']+'/answers',json=answer).status_code==202
    run_once(repo); tree=get(client,tree)
    assert tree['active_task_id'] is None
    assert tree['result']['presentation']['layout']=='cards'
    assert client.post(url+'/cursor',json={'expected_version':oldversion,'message_id':first}).status_code==409
    tree,_=send(client,tree,'暂时不查了')
    assert client.post('/api/tasks/'+tree['task']['id']+'/stop').status_code==200
    tree=get(client,tree)
    assert tree['active_task_id'] is None
    assert tree['messages'][-1]['kind']=='canceled'


@pytest.mark.parametrize('change',['tenant','owner','binding','metric_dependency'])
def test_tree_enforces_identity_binding_and_permissions(push_platform,monkeypatch,change):
    client,repo,source=push_platform
    tree=root(client,repo)
    if change=='tenant':
        assert client.post('/api/login',json={'tenant':'other','username':'admin','password':'fixture-password'}).status_code==200
    elif change=='owner':
        with repo.db.transaction() as conn:
            conn.execute("INSERT INTO users(id,tenant_id,username,password_hash,role,allowed_metrics) SELECT 'peer',tenant_id,'peer',password_hash,role,allowed_metrics FROM users WHERE id='admin'")
        assert client.post('/api/login',json={'tenant':'demo','username':'peer','password':'fixture-password'}).status_code==200
    elif change=='binding':
        monkeypatch.setattr(runtime,'sources',lambda:{'demo:admin':source.model_copy(update={'context_revision':2})})
    else:
        with repo.db.transaction() as conn:
            conn.execute("UPDATE users SET allowed_metrics=array_remove(allowed_metrics,'delivered') WHERE id='admin'")
    assert client.get('/api/push-threads/'+tree['id']).status_code==404


def test_tree_rejects_cross_branch_base_and_unknown_receipt(push_platform,monkeypatch):
    client,repo,_=push_platform
    tree=root(client,repo)
    mock_model(monkeypatch,[{'action':'query','patch':{'observation_hours':48},'base_message_id':'a'*48}])
    tree,_=send(client,tree,'改成48小时')
    task=run_once(repo); tree=get(client,tree)
    assert tree['messages'][-1]['kind']=='failed'
    tree,_=send(client,tree,'重新查一下')
    task_id=tree['active_task_id']
    with repo.db.transaction() as conn:
        conn.execute("INSERT INTO query_receipts(task_id,status) VALUES(%s,'running')",(task_id,))
    run_once(repo); tree=get(client,tree)
    assert tree['active_task_id'] is None and tree['messages'][-1]['kind']=='failed'


def test_tree_long_history_summary_is_branch_scoped_and_originals_remain(push_platform,monkeypatch):
    client,repo,_=push_platform
    tree=root(client,repo); first=tree['cursor_id']
    calls=mock_model(monkeypatch,[{'action':'present','presentation':{'layout':'both','detail':'detailed'}}]*5)
    for i in range(4):
        tree,_=send(client,tree,'详细解释这版结果，保留现有条件。'+('展示偏好说明。'*25))
        run_once(repo); tree=get(client,tree)
        assert tree['messages'][-1]['kind']=='answer',tree['messages'][-1]
    assert any(c['summary'] for c in calls)
    with repo.db.transaction() as conn:
        summaries=conn.execute('SELECT * FROM push_summaries WHERE thread_id=%s',(tree['id'],)).fetchall()
        assert summaries
        assert all(set(s['source_message_ids'])<=set(tree['path_ids']) for s in summaries)
        assert all(m['content'].get('text') for m in tree['messages'] if m['role']=='user')
        snapshots=conn.execute('SELECT request_snapshot FROM push_model_calls').fetchall()
        assert all(r['request_snapshot']['input_upper_bound']<=6000 for r in snapshots)
    old_ids=set(tree['path_ids'])
    tree=seek(client,tree,first)
    tree,_=send(client,tree,'只简要显示')
    run_once(repo); tree=get(client,tree)
    payload=[c['payload'] for c in calls if not c['summary']][-1]
    assert not (set(m['id'] for m in payload.get('messages',[])) & (old_ids-set(tree['path_ids'])))


def test_tree_mandatory_context_overflow_never_sends_model(push_platform,monkeypatch):
    client,repo,_=push_platform
    tree=root(client,repo)
    calls=mock_model(monkeypatch,[])
    tree,_=send(client,tree,'请解释'*600)
    run_once(repo); tree=get(client,tree)
    assert tree['messages'][-1]['kind']=='failed'
    assert '预算' in tree['messages'][-1]['content']['text']
    assert calls==[]


def test_tree_late_model_after_cancel_cannot_append_answer(push_platform,monkeypatch):
    client,repo,_=push_platform
    tree=root(client,repo)
    tree,_=send(client,tree,'只改成表格')
    task_id=tree['active_task_id']
    canceled=[]
    def respond(request):
        assert client.post('/api/tasks/'+task_id+'/stop').status_code==200
        canceled.extend(get(client,tree)['messages'])
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'action':'present','presentation':{'layout':'table','detail':'brief'}})}}]})
    monkeypatch.setattr(ModelTransport,'client',lambda self:httpx.Client(transport=httpx.MockTransport(respond)))
    run_once(repo)
    current=get(client,tree)
    assert current['messages']==canceled
    assert current['active_task_id'] is None


def test_tree_completed_receipt_recovers_without_model_replay(push_platform,monkeypatch):
    client,repo,_=push_platform
    tree=root(client,repo)
    calls=mock_model(monkeypatch,[{'action':'present','presentation':{'layout':'table','detail':'brief'}}])
    tree,_=send(client,tree,'改成表格')
    task_id=tree['active_task_id']
    finish=repo.finish
    monkeypatch.setattr(repo,'finish',lambda *args,**kwargs:(_ for _ in ()).throw(RuntimeError('crash before tree publication')))
    run_once(repo)
    monkeypatch.setattr(repo,'finish',finish)
    with repo.db.transaction() as conn:
        conn.execute("UPDATE tasks SET lease_until=now()-interval '1 second' WHERE id=%s",(task_id,))
    run_once(repo)
    current=get(client,tree)
    assert current['active_task_id'] is None
    assert len([m for m in current['messages'] if m['kind']=='answer'])==2
    assert len([c for c in calls if not c['summary']])==1


def test_tree_reads_exact_ancestor_and_can_base_change_on_old_revision(push_platform,monkeypatch):
    client,repo,_=push_platform
    tree=root(client,repo); first=tree['cursor_id']
    calls=mock_model(monkeypatch,[{'action':'query','patch':{'filters':[{'field':'manufacturer','values':['huawei']}]}},
                                 {'action':'read_history','message_id':first},
                                 {'action':'query','base_message_id':first,'patch':{'time_range':{'start':'2026-09-01T00:00:00+08:00','end_exclusive':'2026-10-01T00:00:00+08:00'}}}])
    tree,_=send(client,tree,'只看华为')
    run_once(repo); tree=get(client,tree)
    tree,_=send(client,tree,'回到第一版，只把时间改成9月')
    run_once(repo); tree=get(client,tree)
    assert tree['result']['plan']['filters']==[]
    assert tree['result']['totals']['sent']['value']=='3600'
    payload=[c['payload'] for c in calls if not c['summary']][-1]
    assert payload['retrieved_message']['id']==first
    assert '统计期间' in payload['retrieved_message']['content']['text']


def test_tree_conflicting_parallel_turns_admit_only_one(push_platform,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    client,repo,_=push_platform
    tree=root(client,repo)
    def submit(text):
        return client.post('/api/push-threads/'+tree['id']+'/turns',json={'question':text,'request_id':str(uuid4()),'expected_version':tree['version']}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses=list(pool.map(submit,['只看华为','只看小米']))
    assert sorted(statuses)==[202,409]
    current=get(client,tree)
    assert len([m for m in current['messages'] if m['role']=='user'])==2


def test_tree_sequence_works_with_actual_api_database_role(push_platform,monkeypatch):
    from fastapi.testclient import TestClient
    from psycopg.conninfo import make_conninfo
    from agent.platform_app.__main__ import provision_roles
    from agent.platform_app.database import Database
    from agent.platform_app.api import create_app
    from agent.tests.test_python_platform import POLICY
    client,repo,source=push_platform
    task=client.post('/api/tasks',json={'question':'运行身份验收','push_plan':plan().model_dump(mode='json')}).json()
    run_once(repo)
    password='tree-runtime-test-password'
    monkeypatch.setenv('POSTGRES_API_PASSWORD',password)
    monkeypatch.setenv('POSTGRES_WORKER_PASSWORD',password)
    api_role,worker_role=source.user+'_api',source.user+'_worker'
    provision_roles(repo.db.dsn,api_role,worker_role)
    db=Database(make_conninfo(repo.db.dsn,user=api_role,password=password))
    try:
        with TestClient(create_app(db,POLICY,origin='http://testserver')) as runtime_client:
            runtime_client.headers['Origin']='http://testserver'
            assert runtime_client.post('/api/login',json={'tenant':'demo','username':'admin','password':'fixture-password'}).status_code==200
            response=runtime_client.post('/api/push-threads',json={'task_id':task['id']})
            assert response.status_code==201,response.text
            assert len(response.json()['messages'])==4
    finally:
        db.close()
