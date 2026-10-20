"""真实 PostgreSQL 验证两类新任务、逐步回执、恢复和模型协议；不请求外部模型。"""
import json
from uuid import uuid4

import httpx
import pytest

from agent.tests.test_push import push_platform, bundle, platform, run_once
from agent.tests.test_query_program import contract as query_contract
from agent.tests.test_exploration import contract as analysis_contract
from agent.platform_app.query_execution import QueryExecution
from agent.runtime.model_transport import ModelTransport


def model_reply(value):
    return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value)}}],
        'usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150}})


def query_model(monkeypatch):
    calls = []
    def reply(request):
        calls.append(request)
        return model_reply({'contract':query_contract().model_dump(mode='json')})
    monkeypatch.setattr(ModelTransport,'client',lambda self:httpx.Client(transport=httpx.MockTransport(reply)))
    return calls


def test_query_initial_full_plan_then_program_only_and_separate_results(push_platform, monkeypatch):
    client,repo,_ = push_platform
    calls = query_model(monkeypatch)
    accepted = client.post('/api/tasks',json={'question':query_contract().question})
    assert accepted.status_code==202,accepted.text
    run_once(repo)
    task = client.get('/api/tasks/'+accepted.json()['id']).json()
    assert task['status']=='completed',task
    assert len(calls)==1
    assert [r['step_id'] for r in task['result']['steps']]==['A','B','C']
    with repo.db.transaction() as conn:
        assert conn.execute('SELECT count(*) n FROM task_evidence WHERE task_id=%s',(task['id'],)).fetchone()['n']==3
        assert conn.execute('SELECT count(*) n FROM task_contracts WHERE task_id=%s',(task['id'],)).fetchone()['n']==1
        trace = conn.execute('SELECT telemetry FROM task_attempts WHERE task_id=%s',(task['id'],)).fetchone()['telemetry']
    assert trace['model_calls']==1 and trace['sql_calls']==2
    reference=task['result']['steps'][0]['result_ref']
    assert client.get('/api/tasks/'+task['id']+'/evidence/'+reference).status_code==200
    assert client.get('/api/tasks/'+task['id']+'/evidence/'+'f'*64).status_code==404


def test_query_resume_after_saved_result_does_not_repeat_model_or_query_a(push_platform,monkeypatch):
    client,repo,_ = push_platform
    calls = query_model(monkeypatch)
    original = QueryExecution.save
    crashed = []
    def save(self,key,plan_hash,data):
        original(self,key,plan_hash,data)
        if key=='A' and not crashed:
            crashed.append(True)
            raise RuntimeError('模拟保存后进程退出')
    monkeypatch.setattr(QueryExecution,'save',save)
    accepted = client.post('/api/tasks',json={'question':query_contract().question}).json()
    run_once(repo)
    with repo.db.transaction() as conn:
        conn.execute("UPDATE tasks SET lease_until=now()-interval '1 second' WHERE id=%s",(accepted['id'],))
    run_once(repo)
    task = client.get('/api/tasks/'+accepted['id']).json()
    assert task['status']=='completed',task
    assert len(calls)==1 and task['result']['steps'][0]['cached']
    with repo.db.transaction() as conn:
        traces = [r['telemetry'] for r in conn.execute('SELECT telemetry FROM task_attempts WHERE task_id=%s ORDER BY execution_version',(task['id'],))]
    assert sum(t.get('sql_calls',0) for t in traces)==2


@pytest.mark.parametrize('dimension',['channel','manufacturer'])
def test_open_analysis_uses_actual_results_and_different_paths(push_platform,monkeypatch,dimension):
    client,repo,_ = push_platform
    calls = []
    def reply(request):
        body = json.loads(request.content)
        system,payload = [m['content'] for m in body['messages']]
        assert len(system.encode())+len(payload.encode())+256 <= 6000
        value = json.loads(payload)
        spec = value['contract']
        if len(calls)<2:
            action = {'action':'query_metrics','arguments':{'period':['baseline','current'][len(calls)],'metrics':['sent','clicked','click_rate']}}
        elif len(calls)==2:
            assert all(e.get('totals') for e in value['evidence'])
            action = {'action':'compare_periods','arguments':{'baseline_ref':'e1','current_ref':'e2'}}
        elif len(calls)==3:
            action = {'action':'query_metrics','arguments':{'period':'current','metrics':['sent','clicked','click_rate'],'dimensions':[dimension]}}
        else:
            refs = [e['ref'] for e in value['evidence']]
            comparison = next(e for e in value['evidence'] if e['kind']=='comparison')
            action = {'action':'finish','arguments':{'evidence_refs':refs,
                'answers':[{'item_id':'a1','status':'answered','text':'已核对整体和分组变化；业务原因仍需验证','evidence_refs':[comparison['ref']]}],
                'claims':[{'result_ref':comparison['ref'],'row':0,'metric':'click_rate','field':'delta'}],
                'hypotheses':['可能与消息内容有关，当前未验证'],'uninvestigated':['尚未检查模板与发送时段']}}
        calls.append(value)
        return model_reply({'spec':spec,'action':action})
    monkeypatch.setattr(ModelTransport,'bounded_client',lambda self,**kwargs:httpx.Client(transport=httpx.MockTransport(reply)))
    # 即使部署策略保留 fixed，新的开放任务仍按观测选择动作。
    client.app.state.policy = client.app.state.policy.model_copy(update={'strategy':'fixed'})
    accepted = client.post('/api/analysis-runs',json={'question':'核查点击率变化','request_id':str(uuid4()),'spec':analysis_contract().model_dump(mode='json')})
    assert accepted.status_code==202,accepted.text
    run_once(repo)
    run = client.get('/api/analysis-runs/'+accepted.json()['id']).json()
    assert run['status']=='completed',run
    report = next(a['data'] for a in run['artifacts'] if a['kind']=='report')
    assert report['completeness']=='complete' and report['claims'][0]['value'] is not None
    assert any(x['dimensions']==[dimension] for x in report['investigated'])
    assert run['state']['model_calls']==5 and run['state']['sql_calls']==3
    assert calls[-1]['remaining_budget']['sql_calls']==3
    with repo.db.transaction() as conn:
        assert conn.execute('SELECT count(*) n FROM task_contracts WHERE task_id=%s',(run['id'],)).fetchone()['n']==1


def test_unified_entry_identifies_analysis_and_carries_initial_usage(push_platform,monkeypatch):
    client,repo,_=push_platform
    calls=[]
    def reply(request):
        messages=json.loads(request.content)['messages']
        system,payload=[m['content'] for m in messages]
        assert len(system.encode())+len(payload.encode())+256<=6000
        if not calls:
            calls.append('intent')
            return model_reply({'contract':analysis_contract().model_dump(mode='json')})
        value=json.loads(payload)
        if len(calls)<3:
            action={'action':'query_metrics','arguments':{'period':['baseline','current'][len(calls)-1],'metrics':['sent','clicked','click_rate']}}
        else:
            refs=[e['ref'] for e in value['evidence']]
            action={'action':'finish','arguments':{'evidence_refs':refs,
                'answers':[{'item_id':'a1','status':'answered','text':'已取得两期证据，原因尚待进一步验证','evidence_refs':refs}],
                'claims':[{'result_ref':refs[-1],'metric':'click_rate'}], 'hypotheses':['业务原因仍待验证']}}
        calls.append('analysis')
        return model_reply({'spec':value['contract'],'action':action})
    monkeypatch.setattr(ModelTransport,'client',lambda self:httpx.Client(transport=httpx.MockTransport(reply)))
    monkeypatch.setattr(ModelTransport,'bounded_client',lambda self,**kwargs:httpx.Client(transport=httpx.MockTransport(reply)))
    accepted=client.post('/api/tasks',json={'question':'分析 app_1 最近点击率变化原因，期间和观察口径已明确'})
    assert accepted.status_code==202
    run_once(repo)
    pending=client.get('/api/tasks/'+accepted.json()['id']).json()
    assert pending['kind']=='analysis' and pending['status']=='pending'
    assert calls==['intent']
    # 已识别的任务需按分析名额领取，不能从初始查询进程直接绕过两并发限制。
    occupying=[]
    with repo.db.transaction() as conn:
        for user_id,tenant in [('ops','demo'),('other','other')]:
            user=repo.user(conn,user_id,tenant)
            task_id=repo.insert_task(conn,user,{'question':'占用分析名额'},kind='analysis')
            occupying.append(task_id)
            conn.execute("UPDATE tasks SET status='querying',lease_until=now()+interval '60 seconds' WHERE id=%s",(task_id,))
    assert repo.claim(True) is None
    assert calls==['intent']
    with repo.db.transaction() as conn:
        conn.execute("UPDATE tasks SET status='completed',lease_until=NULL WHERE id=ANY(%s)",(occupying,))
        conn.execute('UPDATE task_outbox SET delivered_at=now() WHERE task_id=ANY(%s)',(occupying,))
    run_once(repo)
    task=client.get('/api/tasks/'+accepted.json()['id']).json()
    run=client.get('/api/analysis-runs/'+task['id']).json()
    assert task['kind']=='analysis' and task['status']=='completed',run
    assert calls==['intent','analysis','analysis','analysis']
    assert run['state']['model_calls']==4 and run['usage']['known_tokens']==600


def test_budget_stop_preserves_partial_report_without_more_model_calls(push_platform,monkeypatch):
    from agent.platform_app.ledger import Ledger,BudgetExceeded
    client,repo,_=push_platform
    calls=[]
    def reply(request):
        value=json.loads(json.loads(request.content)['messages'][1]['content'])
        calls.append(value)
        return model_reply({'spec':value['contract'],'action':{'action':'query_metrics','arguments':{'period':'current','metrics':['click_rate']}}})
    monkeypatch.setattr(ModelTransport,'bounded_client',lambda self,**kwargs:httpx.Client(transport=httpx.MockTransport(reply)))
    original=Ledger.begin
    def stop_after_evidence(self,op,phase,payload,snapshot):
        if phase=='decide' and snapshot.get('refs'):
            raise BudgetExceeded('模拟调查预算耗尽')
        return original(self,op,phase,payload,snapshot)
    monkeypatch.setattr(Ledger,'begin',stop_after_evidence)
    accepted=client.post('/api/analysis-runs',json={'question':'调查变化','request_id':str(uuid4()),'spec':analysis_contract().model_dump(mode='json')}).json()
    run_once(repo)
    run=client.get('/api/analysis-runs/'+accepted['id']).json()
    assert run['status']=='failed' and len(calls)==1,run
    result=next(a['data'] for a in run['artifacts'] if a['kind']=='report')
    assert result['completeness']=='partial' and '预算耗尽' in result['stop_reason']
    assert result['answers'][0]['status']=='unanswered'


def test_scope_change_requires_specific_confirmation_and_new_version(push_platform,monkeypatch):
    client,repo,_=push_platform
    calls=[]
    proposed=analysis_contract().scope.model_dump(mode='json')
    proposed['filters']=[{'field':'app_id','op':'in','values':['app_2']}]
    def reply(request):
        value=json.loads(json.loads(request.content)['messages'][1]['content'])
        index=len(calls)
        if index==0:
            action={'action':'query_metrics','arguments':{'period':'current','metrics':['click_rate']}}
        elif index==1:
            action={'action':'request_scope_change','arguments':{'scope':proposed,'reason':'需要核查另一应用，请确认具体范围'}}
        elif index in (2,3):
            assert value['contract']['scope']['filters']==proposed['filters']
            if index==2:
                assert value['evidence']==[]
            action={'action':'query_metrics','arguments':{'period':['baseline','current'][index-2],'metrics':['click_rate']}}
        else:
            refs=[e['ref'] for e in value['evidence']]
            action={'action':'finish','arguments':{'evidence_refs':refs,
                'answers':[{'item_id':'a1','status':'answered','text':'已核对新范围的结果','evidence_refs':refs}],
                'claims':[{'result_ref':refs[-1],'metric':'click_rate'}]}}
        calls.append(value)
        return model_reply({'spec':value['contract'],'action':action})
    monkeypatch.setattr(ModelTransport,'bounded_client',lambda self,**kwargs:httpx.Client(transport=httpx.MockTransport(reply)))
    accepted=client.post('/api/analysis-runs',json={'question':'调查变化','request_id':str(uuid4()),'spec':analysis_contract().model_dump(mode='json')}).json()
    run_once(repo)
    run=client.get('/api/analysis-runs/'+accepted['id']).json()
    assert run['status']=='needs_clarification' and run['state']['pending_scope'],run
    assert len(calls)==2
    answer=client.post('/api/analysis-runs/'+run['id']+'/answers',json={'answer':'同意','expected_version':run['version'],'request_id':str(uuid4())})
    assert answer.status_code==409
    confirmation={'approved':True,'expected_version':run['version'],'request_id':str(uuid4())}
    assert client.post('/api/analysis-runs/'+run['id']+'/scope-confirmations',json=confirmation).status_code==200
    assert client.post('/api/analysis-runs/'+run['id']+'/scope-confirmations',json=confirmation).status_code==200
    run_once(repo)
    result=client.get('/api/analysis-runs/'+run['id']).json()
    assert result['status']=='completed',result
    assert result['state']['model_calls']==5 and result['state']['sql_calls']==3
    with repo.db.transaction() as conn:
        versions=conn.execute('SELECT version,body FROM task_contracts WHERE task_id=%s ORDER BY version',(run['id'],)).fetchall()
    assert [v['version'] for v in versions]==[1,2]
    assert versions[0]['body']['scope']['filters'][0]['values']==['app_1']


def test_historical_catalog_result_readable_without_restoring_old_execution(push_platform,monkeypatch):
    from agent.platform_app.historical_results import legacy_push_binding
    from agent.tests.test_push import plan
    from agent.runtime import app as runtime
    from psycopg.types.json import Jsonb
    client,repo,source=push_platform
    accepted=client.post('/api/tasks',json={'question':'旧目录结果','push_plan':plan(['sent']).model_dump(mode='json')}).json()
    run_once(repo)
    with repo.db.transaction() as conn:
        user=repo.user(conn,'admin','demo')
        task=conn.execute('SELECT result FROM tasks WHERE id=%s',(accepted['id'],)).fetchone()
        result={**task['result'],'catalog_version':'push-1'}
        conn.execute('UPDATE tasks SET query_binding=%s,result=%s WHERE id=%s',(legacy_push_binding(user),Jsonb(result),accepted['id']))
    assert client.get('/api/tasks/'+accepted['id']).status_code==200
    changed=source.model_copy(update={'dataset_revision':'b'*64})
    monkeypatch.setattr(runtime,'sources',lambda:{'demo:admin':changed})
    assert client.get('/api/tasks/'+accepted['id']).status_code==404
