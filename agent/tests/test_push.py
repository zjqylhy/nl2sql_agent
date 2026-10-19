"""真实 PostgreSQL、平台任务与独立样本预期对齐；不访问真实模型或云端数据。"""
import contextlib
from datetime import datetime, timedelta
from decimal import Decimal
import io
import json
from uuid import uuid4

import psycopg
import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from agent.tests.test_python_platform import platform
from agent.platform_app.worker import execute
from agent.runtime import app as runtime
from agent.runtime.analysis_api import Snapshot, ToolRequest, metadata, tool
from agent.runtime.push import DEFINITIONS, PushPlan, compile_push, execute_push
from agent.runtime.push_analysis import PushSpec
from agent.runtime.push_sql import validate_push_sql
from agent.runtime.security import UnsafeSQL
from agent.runtime.model_transport import ModelTransport
from agent.tools.generate_push import generate
from agent.tools.import_push_postgres import provision, read_bundle, validate_bundle


@pytest.fixture(scope='module')
def bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp('push_data')
    with contextlib.redirect_stdout(io.StringIO()):
        generate(root)
    return root/'data'


@pytest.fixture
def push_platform(platform, bundle, monkeypatch):
    client, repo, original = platform
    with psycopg.connect(repo.db.dsn, autocommit=True) as conn:
        digest = provision(conn, bundle, 'analytics_push', original.user, original.password,
                           cutoff=datetime.fromisoformat('2026-10-15T00:00:00+08:00'), create_role=False)
    source = original.model_copy(update={'schema_name': 'analytics_push', 'domain': 'push', 'analysis_date': datetime(2026,10,14).date(),
        'data_start': datetime.fromisoformat('2026-04-01T00:00:00+08:00'), 'data_cutoff': datetime.fromisoformat('2026-10-15T00:00:00+08:00'),
        'dataset_revision': digest, 'is_synthetic': True})
    monkeypatch.setattr(runtime, 'sources', lambda: {'demo:admin': source, 'demo:ops': source, 'other:admin': source})
    with repo.db.transaction() as conn:
        conn.execute('UPDATE users SET allowed_metrics=%s', (list(DEFINITIONS),))
    return client, repo, source


def plan(metrics=None, **kwargs):
    return PushPlan(metrics=metrics or ['sent','delivered','clicked','delivery_rate','click_rate'],
        time_range={'start': '2026-07-01T00:00:00+08:00', 'end_exclusive': '2026-08-01T00:00:00+08:00'}, **kwargs)


def run_once(repo):
    task = repo.claim(True)
    assert task
    execute(repo, task)
    return task


@pytest.mark.parametrize('index', range(4))
def test_arbitrary_windows_match_independent_exported_oracle(push_platform, bundle, index):
    _, _, source = push_platform
    expected = [json.loads(x) for x in (bundle/'expected_results.jsonl').read_text(encoding='utf-8').splitlines()][index]
    p = PushPlan(metrics=['sent','accepted','delivered','clicked','delivery_rate','click_rate'],
        time_range=expected['send_period'], observation_hours=expected['observation_hours'])
    result = execute_push(source, p, list(DEFINITIONS), {})
    for name, key in [('sent','sent_count'),('accepted','accepted_count'),('delivered','reported_delivered_count'),('clicked','clicked_count')]:
        assert int(result['totals'][name]['value']) == expected[key]
    assert Decimal(result['totals']['delivery_rate']['value']) == Decimal(expected['reported_delivered_count'])/expected['sent_count']*100
    dp = PushPlan(metrics=['devices','valid_devices','invalid_devices','unknown_devices','active_devices'], time_range=expected['send_period'])
    result = execute_push(source, dp, list(DEFINITIONS), {})
    assert int(result['totals']['active_devices']['value']) == expected['active_app_devices']
    for k in ['valid','invalid','unknown']:
        assert int(result['totals'][k+'_devices']['value']) == expected['device_status_at_period_end'].get(k,0)


def test_duplicate_receipts_and_cross_app_devices_do_not_multiply(push_platform):
    _, _, source = push_platform
    metrics = ['devices','valid_devices','invalid_devices','unknown_devices']
    p = plan(metrics, as_of='2026-10-15T00:00:00+08:00')
    result = execute_push(source, p, list(DEFINITIONS), {})
    assert int(result['totals']['devices']['value']) == 600
    assert [int(result['totals'][k]['value']) for k in metrics[1:]] == [301,280,19]
    p = plan(['sent','clicked','delivered','click_to_delivery_rate'], dimensions=['channel'])
    result = execute_push(source, p, list(DEFINITIONS), {})
    assert int(result['totals']['sent']['value']) == 31*120
    components = {k: sum(Decimal(r['components'].get(k,'0')) for r in result['rows']) for k in ['delivered_clicked','delivered']}
    assert Decimal(result['totals']['click_to_delivery_rate']['value']) == components['delivered_clicked']/components['delivered']*100


def test_no_samples_is_not_zero_rate_and_parameter_binding_blocks_injection(push_platform):
    _, _, source = push_platform
    p = plan(filters=[{'field':'app_id','values':["app_1' OR 1=1 --"]}])
    sql, params = compile_push(p)
    assert 'OR 1=1' not in sql and params[-1].endswith('--')
    result = execute_push(source, p, list(DEFINITIONS), {})
    assert result['totals']['sent']['value'] == '0'
    assert result['totals']['delivery_rate']['value'] is None


def test_template_content_and_grouping_match_generated_records(push_platform, bundle):
    _, _, source = push_platform
    records = [json.loads(line) for line in (bundle/'push_deliveries.jsonl').read_text(encoding='utf-8').splitlines()]
    assert all(r['template_id'] and r['title'] and r['body'] for r in records)
    selected = [r for r in records if r['sent_at'].startswith('2026-07-') and r['app_id'] == 'app_1']
    p = plan(['sent'], dimensions=['template_id'], filters=[{'field':'app_id','values':['app_1']}])
    result = execute_push(source, p, list(DEFINITIONS), {})
    assert {r['dimensions']['template_id']: int(r['values']['sent']['value']) for r in result['rows']} == {
        template: sum(r['template_id'] == template for r in selected) for template in {r['template_id'] for r in selected}}
    invalid, _ = read_bundle(bundle)
    invalid['push_deliveries'][0]['body'] = ''
    with pytest.raises(ValueError, match='模板 ID、标题与正文'):
        validate_bundle(invalid, source.data_cutoff)


@pytest.mark.parametrize('change', [
    {'metrics':['devices','sent']}, {'metrics':['active_devices'],'dimensions':['channel']},
    {'metrics':['active_devices'],'dimensions':['day']}, {'metrics':['not_a_metric']},
    {'dimensions':['device_id']}, {'dimensions':['day','month']},
    {'time_range':{'start':'2026-07-01','end_exclusive':'2026-08-01'}},
    {'time_range':{'start':'2026-08-01T00:00:00+08:00','end_exclusive':'2026-07-01T00:00:00+08:00'}},
])
def test_invalid_grain_fields_or_time_rejected(change):
    value = plan().model_dump(mode='json')
    with pytest.raises(ValidationError):
        PushPlan.model_validate({**value, **change})


def test_cutoff_observation_and_metric_dependencies_enforced(push_platform):
    _, _, source = push_platform
    with pytest.raises(HTTPException) as exc:
        execute_push(source, plan(), ['delivery_rate'], {})
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        execute_push(source, plan(observation_hours=8760), list(DEFINITIONS), {})
    with pytest.raises(HTTPException):
        execute_push(source, plan(['active_devices'], as_of='2026-07-01T00:00:00+08:00'), list(DEFINITIONS), {})
    with pytest.raises(UnsafeSQL):
        validate_push_sql('SELECT pg_sleep(5) FROM metric_devices WHERE 1=1 LIMIT 1001', [])
    with pytest.raises(UnsafeSQL):
        validate_push_sql('SELECT COUNT(*) FROM users WHERE 1=1 LIMIT 1001', [])


def test_actual_api_worker_query_and_sse(push_platform):
    client, repo, _ = push_platform
    assert client.get('/api/query-capabilities').json()['domain'] == 'push'
    assert 'sent' in {m['id'] for m in client.get('/api/metrics').json()}
    response = client.post('/api/tasks', json={'question':'自定义月份推送统计', 'push_plan':plan().model_dump(mode='json')})
    assert response.status_code == 202, response.text
    run_once(repo)
    task = client.get('/api/tasks/'+response.json()['id']).json()
    assert task['status'] == 'completed', task
    assert task['result']['totals']['sent']['value'] == '3720'
    assert 'event: done' in client.get('/api/tasks/'+task['id']+'/events').text


@pytest.mark.parametrize('goal', ['push_summary','device_summary','compare_push'])
def test_persisted_graph_fixed_protocol_and_permission_revalidation(push_platform, goal):
    client, repo, _ = push_platform
    client.app.state.policy = client.app.state.policy.model_copy(update={'strategy':'fixed'})
    p = plan(['devices','valid_devices','active_devices'] if goal=='device_summary' else ['sent','delivered','delivery_rate','clicked','failed','delivery_seconds'], dimensions=['manufacturer'] if goal=='device_summary' else ['channel'])
    spec = PushSpec(goal=goal, current=p, baseline={'start':'2026-06-01T00:00:00+08:00','end_exclusive':'2026-07-01T00:00:00+08:00'} if goal=='compare_push' else None,
                    rank_metric='delivery_rate' if goal=='compare_push' else None,n=3 if goal=='compare_push' else None)
    request = {'question':'已确认结构化分析条件', 'spec':spec.model_dump(mode='json'), 'request_id':str(uuid4())}
    response = client.post('/api/analysis-runs', json=request)
    assert response.status_code == 202, response.text
    assert client.post('/api/analysis-runs', json=request).json()['id'] == response.json()['id']
    run_once(repo)
    run = client.get('/api/analysis-runs/'+response.json()['id']).json()
    assert run['status'] == 'completed', run
    report = next(a['data'] for a in run['artifacts'] if a['kind']=='report')
    assert report['domain']=='push' and report['is_synthetic']
    assert run['state']['model_calls'] == 0
    assert run['state']['sql_calls'] == (2 if goal=='compare_push' else 1)
    with repo.db.transaction() as conn:
        conn.execute("UPDATE users SET allowed_metrics='{}' WHERE id='admin'")
    assert client.get('/api/analysis-runs/'+run['id']).status_code == 404


def test_tool_cannot_finish_early_or_change_confirmed_plan(push_platform):
    _, _, source = push_platform
    identity = dict(tenant_id='demo',role='admin',allowed_metrics=list(DEFINITIONS))
    bound = metadata(Snapshot(**identity,binding='a'*64,question='test'))[1]['binding']
    spec = PushSpec(goal='push_summary',current=plan())
    request = dict(**identity,binding=bound,question='test',spec=spec.model_dump(mode='json'))
    assert tool(ToolRequest(**request,action={'action':'finish','arguments':{'evidence_refs':['fake']}}))['error_code']=='missing_evidence'
    changed = plan(dimensions=['channel']).model_dump(mode='json')
    assert tool(ToolRequest(**request,action={'action':'query_metrics','arguments':{'plan':changed}}))['status']=='error'


def test_adaptive_http_protocol_short_refs_and_context_budget(push_platform, monkeypatch):
    client, repo, _ = push_platform
    p = plan(['sent','delivered','delivery_rate','clicked','failed','delivery_seconds'], dimensions=['channel'])
    spec = PushSpec(goal='compare_push',current=p,baseline={'start':'2026-06-01T00:00:00+08:00','end_exclusive':'2026-07-01T00:00:00+08:00'},rank_metric='delivery_rate',n=3)
    calls=[]
    def respond(request):
        body=json.loads(request.content)
        system,payload=[m['content'] for m in body['messages']]
        assert len(system.encode())+len(payload.encode())+256<=6000
        value=json.loads(payload)
        confirmed=PushSpec.model_validate(value['confirmed_spec'])
        call=len(calls)
        if call<2:
            from agent.runtime.push_analysis import plans
            action={'action':'query_metrics','arguments':{'plan':plans(confirmed)[call].model_dump(mode='json')}}
        else:
            action=[{'action':'compare_periods','arguments':{'baseline_ref':'e1','current_ref':'e2'}},
                    {'action':'rank_declines','arguments':{'result_ref':'e3'}},
                    {'action':'finish','arguments':{'evidence_refs':['e4']}}][call-2]
        calls.append(value)
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'spec':spec.model_dump(mode='json'),'action':action})}}],
                                       'usage':{'prompt_tokens':120,'completion_tokens':80,'total_tokens':200}})
    monkeypatch.setattr(ModelTransport,'bounded_client',lambda self,**kwargs:httpx.Client(transport=httpx.MockTransport(respond)))
    response=client.post('/api/analysis-runs',json={'question':'明确条件的模型协议测试','spec':spec.model_dump(mode='json'),'request_id':str(uuid4())})
    assert response.status_code==202,response.text
    run_once(repo)
    run=client.get('/api/analysis-runs/'+response.json()['id']).json()
    assert run['status']=='completed',run
    assert len(calls)==run['state']['model_calls']==5
    assert run['usage']['known_tokens']==1000


def test_push_metadata_binds_cutoff_and_single_query_permission_revocation(push_platform,monkeypatch):
    client,repo,source=push_platform
    response=client.post('/api/tasks',json={'question':'权限校验','push_plan':plan().model_dump(mode='json')})
    assert response.status_code==202,response.text
    run_once(repo)
    task_id=response.json()['id']
    assert client.get('/api/tasks/'+task_id).status_code==200
    changed=source.model_copy(update={'data_cutoff':source.data_cutoff-timedelta(days=1)})
    monkeypatch.setattr(runtime,'sources',lambda:{'demo:admin':changed})
    assert client.get('/api/tasks/'+task_id).status_code==404


def test_import_conflicting_duplicate_or_overlapping_history_rejected(bundle):
    rows, _ = read_bundle(bundle)
    rows['push_events'].append({**rows['push_events'][0], 'event_type':'expired'})
    with pytest.raises(ValueError,match='去重键内容冲突'):
        validate_bundle(rows,datetime.fromisoformat('2026-10-15T00:00:00+08:00'))
    rows, _ = read_bundle(bundle)
    rows['device_state_history'].append(rows['device_state_history'][0])
    with pytest.raises(ValueError,match='重叠'):
        validate_bundle(rows,datetime.fromisoformat('2026-10-15T00:00:00+08:00'))


def test_import_is_immutable_readonly_and_app_scoped(push_platform, bundle):
    _, repo, source = push_platform
    with psycopg.connect(repo.db.dsn,autocommit=True) as conn:
        with pytest.raises(psycopg.errors.DuplicateSchema):
            provision(conn,bundle,'analytics_push',source.user,source.password,cutoff=source.data_cutoff,create_role=False)
        role=source.user+'_limited'
        provision(conn,bundle,'analytics_push_limited',role,source.password,cutoff=source.data_cutoff,app_ids=['app_1'])
    limited=source.model_copy(update={'schema_name':'analytics_push_limited','user':role})
    assert execute_push(limited,plan(['sent']),list(DEFINITIONS),{})['totals']['sent']['value']=='1240'
    with runtime.connect(limited) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute('SELECT * FROM push_events LIMIT 1')
        with pytest.raises((psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege)):
            conn.execute("DELETE FROM metric_device_activity")


def clarification_model(monkeypatch, responses):
    calls = []
    def respond(request):
        body = json.loads(request.content)
        calls.append(body['messages'])
        value = responses[len(calls)-1]
        return httpx.Response(200, json={'choices':[{'finish_reason':'stop', 'message':{'content':json.dumps(value)}}],
                                       'usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150}})
    monkeypatch.setattr(ModelTransport, 'client', lambda self:httpx.Client(transport=httpx.MockTransport(respond)))
    return calls


def start_clarification(client, repo):
    question = '查询最近两个月 app_1 各厂商发送量、到达率和点击率，按日查看'
    response = client.post('/api/tasks', json={'question':question})
    assert response.status_code == 202, response.text
    run_once(repo)
    task = client.get('/api/tasks/'+response.json()['id']).json()
    assert task['status'] == 'needs_clarification', task
    return task


def answer_body(task, answer='观察时长24小时，统计2026年7月'):
    return {'answer':answer, 'expected_version':task['clarification']['version'], 'request_id':str(uuid4())}


def test_query_clarification_persists_same_task_and_both_rounds(push_platform, monkeypatch):
    client, repo, _ = push_platform
    final = plan(['sent','delivered','delivery_rate','clicked','click_rate'], dimensions=['manufacturer','day'],
                 filters=[{'field':'app_id','values':['app_1']}]).model_dump(mode='json')
    calls = clarification_model(monkeypatch, [{'clarification':'请确认日期和回执观察时长'},
                                             {'clarification':'确认按设备厂商分组？'}, {'plan':final}])
    task = start_clarification(client, repo)
    url = '/api/tasks/'+task['id']
    original = task['input']
    for text in ['观察时长24小时，统计2026年7月', '是，按设备厂商和日分组，保留 app_1']:
        body = answer_body(task, text)
        response = client.post(url+'/answers', json=body)
        assert response.status_code == 202, response.text
        assert response.json()['id'] == task['id']
        assert response.json()['status'] == 'pending'
        assert client.post(url+'/answers', json=body).json()['clarification'] == response.json()['clarification']
        assert client.post(url+'/answers', json={**body,'answer':'不同内容'}).status_code == 409
        assert client.post(url+'/answers', json=answer_body(task)).status_code == 409
        run_once(repo)
        task = client.get(url).json()
        assert task['input'] == original
    assert task['status'] == 'completed', task
    assert task['result']['plan'] == final
    assert task['result']['totals']['sent']['value'] == '1240'
    assert len(calls) == 3
    context = json.loads(calls[-1][1]['content'])
    assert context['original_question'] == original['question']
    assert context['clarifications'] == task['clarification']['turns']
    assert len(context['clarifications']) == 2
    assert 'event: done' in client.get(url+'/events').text
    assert client.post(url+'/answers', json=answer_body(task)).status_code == 409
    saved = next(t for t in client.get('/api/tasks').json() if t['id'] == task['id'])
    assert saved['clarification'] == task['clarification']
    with repo.db.transaction() as conn:
        assert conn.execute('SELECT count(*) n FROM query_receipts').fetchone()['n'] == 1
        assert conn.execute('SELECT count(*) n FROM query_followup_receipts').fetchone()['n'] == 2
        assert conn.execute('SELECT count(*) n FROM tasks').fetchone()['n'] == 1


@pytest.mark.parametrize('guard', ['expired','canceled','limit','binding','owner','tenant'])
def test_query_clarification_rejects_invalid_resume(push_platform, monkeypatch, guard):
    client, repo, source = push_platform
    calls = clarification_model(monkeypatch, [{'clarification':'确认观察时长？'}]*3)
    task = start_clarification(client, repo)
    url = '/api/tasks/'+task['id']
    expected = 409
    if guard == 'expired':
        with repo.db.transaction() as conn:
            conn.execute("UPDATE tasks SET updated_at=now()-interval '25 hours' WHERE id=%s", (task['id'],))
    elif guard == 'canceled':
        assert client.post(url+'/stop').status_code == 200
    elif guard == 'limit':
        for _ in range(2):
            assert client.post(url+'/answers', json=answer_body(task)).status_code == 202
            run_once(repo)
            task = client.get(url).json()
        assert not task['clarification']['can_answer']
    elif guard == 'binding':
        monkeypatch.setattr(runtime, 'sources', lambda:{'demo:admin':source.model_copy(update={'context_revision':2})})
        expected = 404
    else:
        if guard == 'owner':
            # 同租户管理员可看单次查询，但不能代替任务所有者补充条件。
            with repo.db.transaction() as conn:
                conn.execute("INSERT INTO users(id,tenant_id,username,password_hash,role,allowed_metrics) SELECT 'peer',tenant_id,'peer',password_hash,role,allowed_metrics FROM users WHERE id='admin'")
            login = {'tenant':'demo','username':'peer','password':'fixture-password'}
            expected = 403
        else:
            login = {'tenant':'other','username':'admin','password':'fixture-password'}
            expected = 404
        assert client.post('/api/login',json=login).status_code == 200
    assert client.post(url+'/answers',json=answer_body(task)).status_code == expected
    assert len(calls) == (3 if guard == 'limit' else 1)


@pytest.mark.parametrize('completed', [False, True])
def test_query_clarification_crash_receipts_do_not_repeat_model(push_platform, monkeypatch, completed):
    client, repo, _ = push_platform
    calls = clarification_model(monkeypatch, [{'clarification':'确认日期？'}, {'plan':plan().model_dump(mode='json')}])
    task = start_clarification(client, repo)
    url = '/api/tasks/'+task['id']
    assert client.post(url+'/answers',json=answer_body(task)).status_code == 202
    if completed:
        finish = repo.finish
        monkeypatch.setattr(repo, 'finish', lambda *args,**kwargs:(_ for _ in ()).throw(RuntimeError('crash after receipt')))
        run_once(repo)
        monkeypatch.setattr(repo, 'finish', finish)
        with repo.db.transaction() as conn:
            conn.execute("UPDATE tasks SET lease_until=now()-interval '1 second' WHERE id=%s", (task['id'],))
    else:
        with repo.db.transaction() as conn:
            conn.execute("INSERT INTO query_followup_receipts(task_id,version,status) VALUES(%s,1,'running')", (task['id'],))
    run_once(repo)
    task = client.get(url).json()
    assert task['status'] == ('completed' if completed else 'failed'), task
    assert len(calls) == (2 if completed else 1)
    if not completed:
        assert '结果未知' in task['error']


def test_query_clarification_rechecks_permissions_and_legacy_wait(push_platform, monkeypatch):
    client, repo, _ = push_platform
    # 升级前仅有旧回执的等待任务，通过新列默认值就能继续。
    response = client.post('/api/tasks',json={'question':'升级前的发送量查询'})
    task_id = response.json()['id']
    with repo.db.transaction() as conn:
        from psycopg.types.json import Jsonb
        output = {'status':'needs_clarification','message':'确认日期？','usage':{'parser':'llm','tokens':150}}
        conn.execute("INSERT INTO query_receipts(task_id,status,output) VALUES(%s,'completed',%s)", (task_id,Jsonb(output)))
    calls = clarification_model(monkeypatch, [{'plan':plan().model_dump(mode='json')}])
    run_once(repo)
    task = client.get('/api/tasks/'+task_id).json()
    assert task['clarification']['can_answer']
    assert client.post('/api/tasks/'+task_id+'/answers',json=answer_body(task)).status_code == 202
    with repo.db.transaction() as conn:
        conn.execute("UPDATE users SET allowed_metrics='{}' WHERE id='admin'")
    run_once(repo)
    task = client.get('/api/tasks/'+task_id).json()
    assert task['status'] == 'failed', task
    assert len(calls) == 1


def test_query_clarification_requires_actual_question_and_server_context(push_platform, monkeypatch):
    client, repo, _ = push_platform
    assert client.post('/api/tasks',json={'question':'x','push_dialogue':[{'question':'x','answer':'x'}]}).status_code == 400
    calls = clarification_model(monkeypatch, [{'clarification':'  '}])
    created = client.post('/api/tasks',json={'question':'没有有效追问'})
    run_once(repo)
    assert client.get('/api/tasks/'+created.json()['id']).json()['status'] == 'failed'
    assert len(calls) == 1


@pytest.mark.parametrize('with_as_of', [False, True])
def test_invalid_model_plan_clarifies_and_resumes_same_task(push_platform, monkeypatch, with_as_of):
    client, repo, _ = push_platform
    invalid = plan(['sent','delivery_rate','click_rate'], dimensions=['day','manufacturer'], observation_hours=48).model_dump(mode='json')
    invalid['time_range'] = {'start':'2026-08-15T00:00:00+08:00','end_exclusive':'2026-10-15T00:00:00+08:00'}
    if with_as_of:
        invalid['as_of'] = '2026-10-15T00:00:00+08:00'
    final = {**invalid,'as_of':None,'time_range':{'start':'2026-08-01T00:00:00+08:00','end_exclusive':'2026-10-01T00:00:00+08:00'}}
    calls = clarification_model(monkeypatch,[{'clarification':'请确认观察时长'}, {'plan':invalid}, {'plan':final}])
    task = start_clarification(client, repo)
    original = task['input']
    url = '/api/tasks/'+task['id']
    assert client.post(url+'/answers',json=answer_body(task,'48小时')).status_code == 202
    claimed = run_once(repo)
    task = client.get(url).json()
    assert task['status'] == 'needs_clarification' and task['clarification']['can_answer'], task
    assert '2026-10-13T00:00+08:00' in task['result']['message']
    assert task['input'] == original
    assert len(calls) == 2
    with repo.db.transaction() as conn:
        trace = conn.execute('SELECT telemetry FROM task_attempts WHERE task_id=%s AND execution_version=%s', (task['id'],claimed['execution_version'])).fetchone()['telemetry']
        assert trace['error_code'] == ('plan_device_as_of' if with_as_of else 'plan_observation_pending')
        assert trace.get('sql_calls',0) == 0
    assert client.post(url+'/answers',json=answer_body(task,'统计2026年8月1日至10月1日，结束不含，仍观察48小时')).status_code == 202
    run_once(repo)
    task = client.get(url).json()
    assert task['status'] == 'completed' and task['result']['plan']['observation_hours'] == 48, task
    assert task['result']['plan']['dimensions'] == ['day','manufacturer']
    assert task['input'] == original and task['clarification']['turns'][0]['answer'] == '48小时'
    assert task['result']['totals']['sent']['value'] == '7320'
    assert len(calls) == 3 and repo.claim(True) is None


def test_structured_invalid_observation_still_fails_without_model(push_platform, monkeypatch):
    client, repo, _ = push_platform
    calls = clarification_model(monkeypatch,[])
    invalid = plan(observation_hours=48).model_dump(mode='json')
    invalid['time_range']['end_exclusive'] = '2026-10-15T00:00:00+08:00'
    created = client.post('/api/tasks',json={'question':'固定表单无效时间','push_plan':invalid}).json()
    run_once(repo)
    task = client.get('/api/tasks/'+created['id']).json()
    assert task['status'] == 'failed' and '尚未成熟' in task['error']
    assert calls == []
