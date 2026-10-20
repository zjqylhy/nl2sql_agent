"""恢复中断、并发及证据边界；真实隔离PG，全部模型模拟。"""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from agent.tests.test_model_retry import retry_body
from agent.tests.test_push import bundle, push_platform, run_once
from agent.tests.test_python_platform import platform
from agent.tests.test_exploration import contract


def failed_analysis(client,repo,monkeypatch):
    from agent.platform_app import graph
    def decide(request):
        if request.artifacts:
            return {'status':'error','error_code':'model_format','message':'模拟响应异常','model_sent':True,
                    'usage':{'prompt_tokens':100,'completion_tokens':50}}
        return decision(request,{'action':'query_metrics','arguments':{'period':'baseline','metrics':['click_rate']}})
    monkeypatch.setattr(graph,'decide',decide)
    spec=contract().model_dump(mode='json')
    task=client.post('/api/analysis-runs',json={'question':spec['question'],'spec':spec,'request_id':str(uuid4())}).json()
    run_once(repo)
    run=client.get('/api/analysis-runs/'+task['id']).json()
    assert run['status']=='failed' and run['retry']['available'],run
    return run


def decision(request,action):
    return {'status':'completed','model_sent':True,'usage':{'prompt_tokens':100,'completion_tokens':50},
            'decision':{'spec':request.spec.model_dump(mode='json'),'action':action}}


def finish_model(monkeypatch,queries=False):
    calls=[]
    def decide(request):
        calls.append(request)
        if queries and len([a for a in request.artifacts if a['kind']=='query'])==1:
            action={'action':'query_metrics','arguments':{'period':'current','metrics':['click_rate']}}
        else:
            ref=next(a['result_ref'] for a in request.artifacts if a['kind']=='query')
            action={'action':'finish','arguments':{'evidence_refs':[ref],
                'answers':[{'item_id':'a1','status':'answered','text':'已取得数值证据，业务原因仍待验证','evidence_refs':[ref]}],
                'claims':[{'result_ref':ref,'row':0,'metric':'click_rate'}]}}
        return decision(request,action)
    monkeypatch.setattr('agent.platform_app.graph.decide',decide)
    return calls


@pytest.mark.parametrize('guard',['corrupt_evidence','missing_checkpoint','pruning','code_changed','revoked','canceled','other_budget'])
def test_resume_rejects_invalid_saved_state_without_external_calls(push_platform,monkeypatch,guard):
    client,repo,_=push_platform
    old=failed_analysis(client,repo,monkeypatch);body=retry_body(old)
    with repo.db.transaction() as conn:
        if guard=='corrupt_evidence':
            conn.execute("UPDATE analysis_artifacts SET body=jsonb_set(body,'{data,coverage}','\"partial\"') WHERE run_id=%s AND body->>'kind'='query'",(old['id'],))
        elif guard=='missing_checkpoint':conn.execute('UPDATE analysis_runs SET recovery_checkpoint=NULL WHERE id=%s',(old['id'],))
        elif guard=='pruning':conn.execute("UPDATE analysis_runs SET evidence_lifecycle='pruning' WHERE id=%s",(old['id'],))
        elif guard=='code_changed':conn.execute("UPDATE analysis_runs SET metadata=jsonb_set(metadata,'{code_digest}','\"unknown-code\"') WHERE id=%s",(old['id'],))
        elif guard=='revoked':conn.execute("UPDATE users SET allowed_metrics='{}' WHERE id='admin'")
        elif guard=='canceled':conn.execute("UPDATE tasks SET status='canceled' WHERE id=%s",(old['id'],))
        else:conn.execute("UPDATE analysis_runs SET state=jsonb_set(state,'{tokens}','30000') WHERE id=%s",(old['id'],))
    calls=finish_model(monkeypatch,queries=True)
    assert client.post('/api/tasks/'+old['id']+'/retry',json=body).status_code in {404,409}
    assert not calls
    with repo.db.transaction() as conn:
        assert conn.execute('SELECT count(*) n FROM task_resume_rounds').fetchone()['n']==0


def test_concurrent_resume_accepts_one_round_and_fences_old_worker(push_platform,monkeypatch):
    from agent.platform_app.task_retry import create_retry
    from agent.platform_app.checkpoints import LeaseLost
    client,repo,_=push_platform;old=failed_analysis(client,repo,monkeypatch)
    with repo.db.transaction() as conn:
        stale=conn.execute('SELECT * FROM tasks WHERE id=%s',(old['id'],)).fetchone()
    def accept(_):
        from fastapi import HTTPException
        with repo.db.transaction() as conn:
            user=repo.user(conn,'admin','demo')
            try:
                create_retry(repo,conn,user,old['id'],str(uuid4()),old['retry']['execution_version'],object())
                return 202
            except HTTPException as exc:
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(accept,range(2)))==[202,409]
    with repo.db.transaction() as conn:
        assert conn.execute('SELECT count(*) n FROM task_resume_rounds').fetchone()['n']==1
        with pytest.raises(LeaseLost):repo.execution(conn,stale)
    assert client.post('/api/tasks/'+old['id']+'/stop',json={}).status_code==200
    kept=client.get('/api/analysis-runs/'+old['id']).json()
    assert kept['status']=='canceled' and kept['resume_rounds'][0]['status']=='canceled'


@pytest.mark.parametrize('crash_at',['applied_checkpoint','query_receipt'])
def test_resume_crash_does_not_rewind_or_repeat_saved_query(push_platform,monkeypatch,crash_at):
    from agent.platform_app.ledger import Ledger
    from langgraph.graph.state import CompiledStateGraph
    client,repo,_=push_platform;old=failed_analysis(client,repo,monkeypatch)
    assert client.post('/api/tasks/'+old['id']+'/retry',json=retry_body(old)).status_code==202
    calls=finish_model(monkeypatch,queries=True);crashed=[]
    update=CompiledStateGraph.update_state;complete=Ledger.complete
    def crash_update(self,*args,**kwargs):
        value=update(self,*args,**kwargs)
        if not crashed and kwargs.get('as_node')=='interpret':
            crashed.append(True);raise RuntimeError('模拟恢复存档已写入后进程中断')
        return value
    def crash_complete(self,op_id,output):
        value=complete(self,op_id,output)
        if not crashed and output.get('artifact',{}).get('kind')=='query':
            crashed.append(True);raise RuntimeError('模拟新查询成功回执后进程中断')
        return value
    if crash_at=='applied_checkpoint':monkeypatch.setattr(CompiledStateGraph,'update_state',crash_update)
    else:monkeypatch.setattr(Ledger,'complete',crash_complete)
    run_once(repo);assert crashed
    with repo.db.transaction() as conn:
        conn.execute("UPDATE tasks SET lease_until=now()-interval '1 second' WHERE id=%s",(old['id'],))
    run_once(repo)
    run=client.get('/api/analysis-runs/'+old['id']).json()
    assert run['status']=='completed',run
    assert run['state']['sql_calls']==2
    assert run['state']['model_calls']==old['state']['model_calls']+2
    assert len(calls)==2
    assert len(run['resume_rounds'])==1


def test_unknown_resumed_model_is_not_automatically_resent(push_platform,monkeypatch):
    client,repo,_=push_platform;old=failed_analysis(client,repo,monkeypatch)
    assert client.post('/api/tasks/'+old['id']+'/retry',json=retry_body(old)).status_code==202
    sent=[]
    def unknown(request):
        sent.append(request);raise RuntimeError('模拟发送模型后丢失回执')
    monkeypatch.setattr('agent.platform_app.graph.decide',unknown)
    run_once(repo)
    with repo.db.transaction() as conn:
        conn.execute("UPDATE tasks SET lease_until=now()-interval '1 second' WHERE id=%s",(old['id'],))
    run_once(repo)
    failed=client.get('/api/analysis-runs/'+old['id']).json()
    assert failed['status']=='failed' and failed['usage']['unknown_calls']==1
    assert len(sent)==1 and failed['state']['model_calls']==old['state']['model_calls']+1
    assert client.post('/api/tasks/'+old['id']+'/retry',json=retry_body(failed)).status_code==202
    calls=finish_model(monkeypatch,queries=True)
    run_once(repo)
    run=client.get('/api/analysis-runs/'+old['id']).json()
    assert run['status']=='completed' and run['usage']['unknown_calls']==1,(run['state']['message'],run['state']['feedback'])
    assert len(calls)==2 and run['state']['sql_calls']==2


def test_api_role_accepts_resume_without_checkpoint_schema_access(push_platform,monkeypatch):
    import psycopg
    from psycopg.conninfo import make_conninfo
    from agent.platform_app.__main__ import provision_roles
    from agent.platform_app.database import Database
    from agent.platform_app.api import create_app
    from agent.tests.test_python_platform import POLICY,ORIGIN
    from fastapi.testclient import TestClient
    client,repo,source=push_platform;old=failed_analysis(client,repo,monkeypatch)
    api_role,worker_role=source.user+'_api',source.user+'_worker'
    monkeypatch.setenv('POSTGRES_API_PASSWORD','fixture-api-password-only')
    monkeypatch.setenv('POSTGRES_WORKER_PASSWORD','fixture-worker-password-only')
    provision_roles(repo.db.dsn,api_role,worker_role)
    dsn=make_conninfo(repo.db.dsn,user=api_role,password='fixture-api-password-only')
    with psycopg.connect(dsn) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):conn.execute('SELECT * FROM agent_checkpoint.checkpoints')
    db=Database(dsn)
    try:
        with TestClient(create_app(db,POLICY,origin=ORIGIN)) as api_client:
            api_client.headers['Origin']=ORIGIN
            assert api_client.post('/api/login',json={'tenant':'demo','username':'admin','password':'fixture-password'}).status_code==200
            assert api_client.post('/api/tasks/'+old['id']+'/retry',json=retry_body(old)).status_code==202
    finally:
        db.close()
    finish_model(monkeypatch,queries=True);run_once(repo)
    result=client.get('/api/analysis-runs/'+old['id']).json()
    assert result['status']=='completed',(result['state']['message'],result['state']['feedback'])


def test_real_ten_call_budget_stop_can_extend_and_finish(push_platform,monkeypatch):
    client,repo,_=push_platform;calls=[]
    client.app.state.policy=client.app.state.policy.model_copy(update={'max_input_bytes':32768,'max_tokens':400000})
    metrics=contract().exploration_permissions.metrics[:10]
    def decide(request):
        calls.append(request)
        return decision(request,{'action':'lookup_metrics','arguments':{'metrics':[metrics[len(calls)-1]]}})
    monkeypatch.setattr('agent.platform_app.graph.decide',decide)
    spec=contract().model_dump(mode='json')
    spec['scope']['periods']={'baseline':spec['scope']['periods']['baseline']}
    spec['question']='核实指定期间点击率及已调查方向'
    task=client.post('/api/analysis-runs',json={'question':spec['question'],'spec':spec,'request_id':str(uuid4())}).json()
    run_once(repo)
    old=client.get('/api/analysis-runs/'+task['id']).json()
    assert len(calls)==old['state']['model_calls']==10,(old['state']['message'],old['state']['feedback'])
    assert old['status']=='failed' and old['retry']['available'] and old['retry']['requires_call_extension'],old
    assert old['retry']['error_code']=='model_calls_budget'
    body={**retry_body(old),'additional_model_calls':2}
    assert client.post('/api/tasks/'+task['id']+'/retry',json=body).status_code==202
    resumed=finish_model(monkeypatch)
    # 此前只有口径证据；追加后先取一个完整结果，再提交报告。
    def after(request):
        if not any(a['kind']=='query' for a in request.artifacts):
            return decision(request,{'action':'query_metrics','arguments':{'period':'baseline','metrics':['click_rate']}})
        return resumed_decide(request)
    from agent.platform_app import graph
    resumed_decide=graph.decide
    monkeypatch.setattr(graph,'decide',after)
    run_once(repo)
    run=client.get('/api/analysis-runs/'+task['id']).json()
    assert run['status']=='completed' and run['state']['model_calls']==12,(run['state']['message'],run['state']['feedback'],run['state'].get('recovery_failure'),run['state']['model_calls'])
    assert run['state']['sql_calls']==1 and len(resumed)==1


def test_legacy_preparation_is_in_place_and_requires_exact_compatibility(push_platform,monkeypatch,tmp_path):
    import json
    from agent.platform_app.repository import code_digest
    from agent.platform_app import resume_compatibility
    from agent.tools.prepare_task_resume import prepare
    from fastapi import HTTPException
    client,repo,_=push_platform;old=failed_analysis(client,repo,monkeypatch)
    with repo.db.transaction() as conn:
        conn.execute("UPDATE analysis_runs SET recovery_checkpoint=NULL,metadata=jsonb_set(metadata,'{code_digest}',%s) WHERE id=%s",
                     (Jsonb('a'*64),old['id']))
        before=conn.execute('SELECT state,binding FROM analysis_runs WHERE id=%s',(old['id'],)).fetchone()
    with pytest.raises(HTTPException):prepare(repo,old['id'])
    manifest=tmp_path/'resume_compatibility.json'
    proof={'source_digest':'a'*64,'target_digest':code_digest(),'graph_version':'stategraph-1',
           'recovery_version':1,'replay_tests':['isolated-legacy-checkpoint']}
    manifest.write_text(json.dumps([proof]),encoding='utf-8')
    monkeypatch.setattr(resume_compatibility,'Path',lambda _:manifest)
    # with_name保持独立清单路径，使用真实原生存档；不调用模型、不导出结果。
    result=prepare(repo,old['id'])
    assert result['valid'] and not result['applied']
    with repo.db.transaction() as conn:
        assert conn.execute('SELECT recovery_checkpoint FROM analysis_runs WHERE id=%s',(old['id'],)).fetchone()['recovery_checkpoint'] is None
    assert prepare(repo,old['id'],apply=True)['applied']
    with repo.db.transaction() as conn:
        after=conn.execute('SELECT state,binding FROM analysis_runs WHERE id=%s',(old['id'],)).fetchone()
        assert after==before
    ready=client.get('/api/analysis-runs/'+old['id']).json()
    assert ready['retry']['available']
    assert client.post('/api/tasks/'+old['id']+'/retry',json=retry_body(ready)).status_code==202
    finish_model(monkeypatch,queries=True);run_once(repo)
    assert client.get('/api/analysis-runs/'+old['id']).json()['status']=='completed'


def test_native_checkpoint_loss_after_acceptance_stops_before_model(push_platform,monkeypatch):
    client,repo,_=push_platform;old=failed_analysis(client,repo,monkeypatch)
    assert client.post('/api/tasks/'+old['id']+'/retry',json=retry_body(old)).status_code==202
    with repo.db.transaction() as conn:
        for table in ('checkpoint_writes','checkpoint_blobs','checkpoints'):
            conn.execute(f'DELETE FROM agent_checkpoint.{table} WHERE thread_id=%s',(old['id'],))
    calls=finish_model(monkeypatch,queries=True)
    run_once(repo)
    run=client.get('/api/analysis-runs/'+old['id']).json()
    assert run['status']=='failed' and not calls
    assert not run['retry']['available']
    assert run['state']['model_calls']==old['state']['model_calls']


def test_retried_initial_parse_routes_analysis_with_cumulative_usage(push_platform,monkeypatch):
    from agent.tests.test_model_retry import model,failed
    from agent.platform_app import graph
    client,repo,_=push_platform
    initial=model(monkeypatch,[429,{'contract':contract().model_dump(mode='json')}])
    old=failed(client,repo)
    assert client.post('/api/tasks/'+old['id']+'/retry',json=retry_body(old)).status_code==202
    run_once(repo)
    pending=client.get('/api/analysis-runs/'+old['id']).json()
    assert pending['status']=='pending' and pending['state']['model_calls']==2
    assert pending['usage']['unknown_calls']==1
    finish_model(monkeypatch,queries=True);next_decide=graph.decide
    def after(request):
        if not any(a['kind']=='query' for a in request.artifacts):
            return decision(request,{'action':'query_metrics','arguments':{'period':'baseline','metrics':['click_rate']}})
        return next_decide(request)
    monkeypatch.setattr(graph,'decide',after)
    run_once(repo)
    run=client.get('/api/analysis-runs/'+old['id']).json()
    assert run['status']=='completed' and run['state']['model_calls']==5
    assert len(initial)==2 and run['state']['sql_calls']==2
    assert run['usage']['unknown_calls']==1 and run['usage']['known_tokens']==600
    assert [s['resume_round'] for s in run['steps'][:2]]==[0,1]


def test_tenth_initial_response_keeps_accepted_contract_without_reparsing(push_platform,monkeypatch):
    from agent.tests.test_model_retry import model,failed
    from agent.platform_app import graph
    client,repo,_=push_platform
    client.app.state.policy=client.app.state.policy.model_copy(update={'max_input_bytes':32768,'max_tokens':400000})
    initial=model(monkeypatch,[{'unexpected':'bad format'}]*9+[{'contract':contract().model_dump(mode='json')}])
    task=failed(client,repo)
    for _ in range(9):
        assert task['status']=='failed' and task['retry']['available'],task.get('error')
        assert client.post('/api/tasks/'+task['id']+'/retry',json=retry_body(task)).status_code==202
        run_once(repo)
        task=client.get('/api/tasks/'+task['id']).json()
    assert task['kind']=='analysis' and task['status']=='pending' and len(initial)==10
    run_once(repo)
    stopped=client.get('/api/analysis-runs/'+task['id']).json()
    assert stopped['retry']['requires_call_extension'] and stopped['retry']['available']
    assert client.post('/api/tasks/'+task['id']+'/retry',json={**retry_body(stopped),'additional_model_calls':3}).status_code==202
    finish_model(monkeypatch,queries=True);next_decide=graph.decide
    def after(request):
        if not any(a['kind']=='query' for a in request.artifacts):
            return decision(request,{'action':'query_metrics','arguments':{'period':'baseline','metrics':['click_rate']}})
        return next_decide(request)
    monkeypatch.setattr(graph,'decide',after)
    run_once(repo)
    run=client.get('/api/analysis-runs/'+task['id']).json()
    assert run['status']=='completed' and run['state']['model_calls']==13
    assert len(initial)==10
