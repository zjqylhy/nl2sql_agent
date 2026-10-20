"""维度取值仍使用真实只读查询、范围与完整性检查，不补造回执观察口径。"""
from datetime import datetime

import pytest
import psycopg
from psycopg import sql
from pydantic import ValidationError
from fastapi import HTTPException

from agent.runtime.push_values import ValuesPlan, compile_values, execute_values
from agent.runtime.query_program import validate_program
from agent.runtime.query_executor import execute_program
from agent.runtime.task_contracts import TaskContract
from agent.tests.test_python_platform import platform
from agent.tests.test_push import bundle, push_platform, run_once, answer_body
from agent.tests.test_model_retry import model


def values_contract():
    return TaskContract(task_type='query',question='发送记录一共有哪些 app？',
        scope={'periods':{'current':{'start':'2026-08-01T00:00:00+08:00','end_exclusive':'2026-09-15T00:00:00+08:00'}},'observation_hours':None},
        result_requirements={'kind':'dimension_values','dimensions':['app_id'],'period_names':['current']},
        execution_plan={'steps':[{'id':'A','op':'query_values','period':'current','dimensions':['app_id']}],
                        'outputs':[{'step':'A','metrics':[],'dimensions':['app_id']}]})


def test_value_query_matches_independent_raw_sql_and_does_not_require_horizon(push_platform):
    client,repo,source=push_platform;contract=values_contract()
    response=client.post('/api/tasks',json={'question':contract.question,'query_contract':contract.model_dump(mode='json')})
    assert response.status_code==202,response.text
    run_once(repo);task=client.get('/api/tasks/'+response.json()['id']).json()
    assert task['status']=='completed',task
    result=task['result'];period=contract.scope.periods['current']
    with psycopg.connect(repo.db.dsn) as conn:
        expected=[r[0] for r in conn.execute('SELECT DISTINCT app_id FROM analytics_push.push_deliveries WHERE sent_at>=%s AND sent_at<%s ORDER BY app_id',(period.start,period.end_exclusive))]
    assert [r['dimensions']['app_id'] for r in result['rows']]==expected==['app_1','app_2','app_3']
    assert result['metrics']==[] and result['metrics_used']==['sent']
    assert result['plan']['observation_hours'] is None and result['result_kind']=='dimension_values'
    assert 'metric_push_daily_outcomes' in result['sql']
    assert client.get('/api/tasks/'+task['id']+'/evidence/'+result['steps'][0]['result_ref']).status_code==200
    partial=ValuesPlan(dimensions=['app_id','manufacturer'],time_range={'start':'2026-08-01T01:00:00+08:00','end_exclusive':'2026-08-02T02:00:00+08:00'},filters=[{'field':'app_id','values':['app_1']}])
    actual=execute_values(source,partial,['sent'],{})
    assert 'metric_push_deliveries' in actual['sql'] and all(r['dimensions']['app_id']=='app_1' for r in actual['rows'])
    # 取值不读取回执，允许时间范围刚好到数据截止；无需额外增加一小时。
    ValuesPlan(dimensions=['app_id'],time_range={'start':'2026-10-14T00:00:00+08:00','end_exclusive':'2026-10-15T00:00:00+08:00'}).check_source(source)


def test_natural_app_list_only_clarifies_time_and_then_returns_values(push_platform,monkeypatch):
    client,repo,_=push_platform
    calls=model(monkeypatch,[{'clarification':'请确认要查询的分发记录时间范围（可查询区间为2026-04-01至2026-10-15，结束时刻不含）。'}, {'contract':values_contract().model_dump(mode='json')}])
    task=client.post('/api/tasks',json={'question':'分发记录一共有哪些 app?'}).json()
    run_once(repo);task=client.get('/api/tasks/'+task['id']).json();assert task['status']=='needs_clarification'
    assert client.post('/api/tasks/'+task['id']+'/answers',json=answer_body(task,'2026-08-01 至 2026-09-15，结束不含')).status_code==202
    run_once(repo);task=client.get('/api/tasks/'+task['id']).json()
    assert task['status']=='completed',task
    assert [r['label'] for r in task['result']['rows']]==['app_1','app_2','app_3']
    assert len(calls)==2 and task['result']['contract']['scope']['observation_hours'] is None
    assert 'query_values' in calls[0]['messages'][0]['content']


def test_invalid_model_contract_is_readable_and_manually_retryable(push_platform,monkeypatch):
    client,repo,_=push_platform
    invalid=values_contract().model_dump(mode='json');invalid['scope']['observation_hours']=0
    invalid['result_requirements']['kind']='metrics'
    model(monkeypatch,[{'contract':invalid}])
    task=client.post('/api/tasks',json={'question':'分发记录一共有哪些 app?'}).json();run_once(repo)
    task=client.get('/api/tasks/'+task['id']).json()
    assert task['status']=='failed' and task['retry']['available']
    assert '计划字段' in task['error'] and 'pydantic' not in task['error'] and 'validation errors' not in task['error']
    with repo.db.transaction() as conn:
        trace=conn.execute('SELECT telemetry FROM task_attempts WHERE task_id=%s',(task['id'],)).fetchone()['telemetry']
        assert trace['error_code']=='model_plan_fields' and trace['sql_calls']==0


def test_real_model_direct_contract_keeps_dates_and_uses_the_same_execution_checks(push_platform,monkeypatch):
    client,repo,_=push_platform
    direct=values_contract().model_dump(mode='json')
    direct['question']='模型改写的问题'
    model(monkeypatch,[{'clarification':'请确认发送起止时间，结束不含'},direct])
    question='分发记录一共有哪些 app?'
    task=client.post('/api/tasks',json={'question':question}).json();run_once(repo)
    task=client.get('/api/tasks/'+task['id']).json()
    client.post('/api/tasks/'+task['id']+'/answers',json=answer_body(task,'2026-08-01 至 2026-09-15'))
    run_once(repo);task=client.get('/api/tasks/'+task['id']).json()
    assert task['status']=='completed',task
    result=task['result']
    assert result['contract']['question']==question
    assert result['contract']['scope']['periods']==values_contract().model_dump(mode='json')['scope']['periods']
    assert [r['dimensions']['app_id'] for r in result['rows']]==['app_1','app_2','app_3']


@pytest.mark.parametrize('change',['ambiguous_envelope','invalid_horizon','sql_in_step'])
def test_direct_contract_does_not_bypass_rejection_before_sql(push_platform,monkeypatch,change):
    client,repo,_=push_platform;direct=values_contract().model_dump(mode='json')
    if change=='ambiguous_envelope':direct['clarification']='同时追问不能执行'
    elif change=='invalid_horizon':direct['scope']['observation_hours']=0
    else:direct['execution_plan']['steps'][0]['sql']='SELECT * FROM users'
    model(monkeypatch,[direct])
    task=client.post('/api/tasks',json={'question':'分发记录一共有哪些 app?'}).json();run_once(repo)
    task=client.get('/api/tasks/'+task['id']).json();assert task['status']=='failed'
    with repo.db.transaction() as conn:
        trace=conn.execute('SELECT telemetry FROM task_attempts WHERE task_id=%s',(task['id'],)).fetchone()['telemetry']
        assert trace['sql_calls']==0


def test_existing_legacy_plan_failure_is_retryable_without_rewriting_diagnostics(push_platform,monkeypatch):
    client,repo,_=push_platform
    model(monkeypatch,[429])
    created=client.post('/api/tasks',json={'question':'有哪些 app？'}).json();run_once(repo)
    raw_error='执行计划检查未通过：2 validation errors for TaskContract https://errors.pydantic.dev/2.14/v/too_short'
    with repo.db.transaction() as conn:
        conn.execute('UPDATE tasks SET error=%s WHERE id=%s',(raw_error,created['id']))
        conn.execute("UPDATE task_attempts SET telemetry=telemetry || '{\"error_code\":\"\"}'::jsonb WHERE task_id=%s",(created['id'],))
    public=client.get('/api/tasks/'+created['id']).json()
    assert public['retry']['available'] and public['retry']['error_code']=='model_plan_fields'
    assert 'pydantic' not in public['error'] and '计划字段' in public['error']
    with repo.db.transaction() as conn:
        assert conn.execute('SELECT error FROM tasks WHERE id=%s',(created['id'],)).fetchone()['error']==raw_error


@pytest.mark.parametrize('change',['metric_empty','horizon','unknown_dimension','wrong_kind','extra_sql'])
def test_value_protocol_cannot_weaken_metric_contract_or_accept_sql(push_platform,change):
    _,_,source=push_platform;value=values_contract().model_dump(mode='json')
    if change=='metric_empty':value['result_requirements']['kind']='metrics'
    elif change=='horizon':value['scope']['observation_hours']=24
    elif change=='unknown_dimension':value['execution_plan']['steps'][0]['dimensions']=['title']
    elif change=='wrong_kind':value['execution_plan']['steps'][0]={'id':'A','op':'query','period':'current','metrics':['sent'],'dimensions':['app_id']}
    else:value['execution_plan']['steps'][0]['sql']='SELECT * FROM users'
    with pytest.raises((ValueError,ValidationError)):
        validate_program(TaskContract.model_validate(value),source,['sent'])


def test_values_apply_permission_date_filter_and_parameter_guards(push_platform):
    _,_,source=push_platform;contract=values_contract()
    with pytest.raises(ValueError,match='权限'):validate_program(contract,source,['clicked'])
    p=ValuesPlan(dimensions=['app_id'],time_range=contract.scope.periods['current'],filters=[{'field':'app_id','values':["app_1' OR 1=1 --"]}])
    statement,params=compile_values(p)
    assert 'OR 1=1' not in statement and params[-1].endswith('--')
    assert execute_values(source,p,['sent'],{})['rows']==[]
    with pytest.raises(HTTPException):execute_values(source,p,[],{})
    p=p.model_copy(update={'time_range':p.time_range.model_copy(update={'end_exclusive':datetime.fromisoformat('2026-10-16T00:00:00+08:00')})})
    with pytest.raises(ValueError,match='范围'):p.check_source(source)


def test_value_row_1001_is_rejected_in_real_postgres(push_platform):
    _,repo,source=push_platform
    with psycopg.connect(repo.db.dsn,autocommit=True) as conn:
        conn.execute('CREATE SCHEMA analytics_values_boundary')
        conn.execute("CREATE VIEW analytics_values_boundary.metric_push_daily_outcomes AS SELECT '2026-08-01'::date sent_day,'app_'||n app_id FROM generate_series(1,1001) n")
        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA analytics_values_boundary TO {}').format(sql.Identifier(source.user)))
        conn.execute(sql.SQL('GRANT SELECT ON analytics_values_boundary.metric_push_daily_outcomes TO {}').format(sql.Identifier(source.user)))
    p=ValuesPlan(dimensions=['app_id'],time_range=values_contract().scope.periods['current'])
    with pytest.raises(HTTPException,match='1000'):
        execute_values(source.model_copy(update={'schema_name':'analytics_values_boundary'}),p,['sent'],{})
