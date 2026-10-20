"""复现错误响应和观察边界；不访问真实模型或业务数据库。"""
from datetime import datetime
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from agent.runtime import telemetry
from agent.runtime.app import QueryRequest
from agent.runtime.model_transport import ModelTransport
from agent.runtime.push import DEFINITIONS, PushPlan
from agent.runtime.push_plan_validation import validate_model_plan
from agent.runtime.push_query import run_push_query


@pytest.fixture
def source():
    return SimpleNamespace(domain='push', data_start=datetime.fromisoformat('2026-04-01T00:00:00+08:00'),
                           data_cutoff=datetime.fromisoformat('2026-10-15T00:00:00+08:00'))


def value(**patch):
    return {'metrics':['sent','delivery_rate','click_rate'], 'dimensions':['day','manufacturer'],
            'time_range':{'start':'2026-08-15T00:00:00+08:00','end_exclusive':'2026-10-15T00:00:00+08:00'},
            'observation_hours':48, **patch}


def model(monkeypatch, response=None, *, content=None, finish='stop', status=200, error=None):
    calls=[]
    monkeypatch.setenv('LLM_API_KEY','test-no-network')
    monkeypatch.setenv('LLM_MODEL','fixture')
    monkeypatch.setenv('LLM_BASE_URL','https://fixture.invalid/v1')
    def respond(request):
        calls.append(json.loads(request.content))
        if error:
            raise error
        return httpx.Response(status,json={'choices':[{'finish_reason':finish,'message':{'content': content if content is not None else json.dumps(response)}}],
                                          'usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150}})
    monkeypatch.setattr(ModelTransport,'client',lambda self:httpx.Client(transport=httpx.MockTransport(respond)))
    return calls


def request():
    return QueryRequest(tenant_id='push', role='admin', allowed_metrics=list(DEFINITIONS),
                        input={'question':'按日期分析各厂商最近两个月的发送、到达率、点击率变化'},
                        push_dialogue=[{'question':'请确认观察时长','answer':'48小时'}])


def test_reproduced_as_of_preserves_rejected_plan_and_never_runs_sql(source,monkeypatch):
    from agent.runtime import query_executor
    invalid=value(as_of='2026-10-15T00:00:00+08:00')
    calls=model(monkeypatch,{'plan':invalid})
    monkeypatch.setattr(query_executor,'execute_push',lambda *args:pytest.fail('invalid plan executed'))
    with telemetry.capture(None) as trace:
        result=run_push_query(request(),source)
    assert result['status']=='needs_clarification'
    assert '设备状态时点' in result['message'] and '2026-10-13T00:00+08:00' in result['message']
    assert result['usage']['parser']=='llm'
    assert trace['error_code']=='plan_device_as_of' and trace['error_fields']==['as_of']
    assert invalid['as_of']=='2026-10-15T00:00:00+08:00'
    assert len(calls)==1
    sent=json.loads(calls[0]['messages'][1]['content'])
    assert sent['original_question']==request().input.question and sent['clarifications'][0]['answer']=='48小时'
    prompt=calls[0]['messages'][0]['content']
    assert 'as_of device-only' in prompt and 'Data start=2026-04-01' in prompt and 'Preserve ALL original and clarified' in prompt


def test_observation_boundary_keeps_48_hours_without_shifting_dates(source):
    with telemetry.capture(None) as trace:
        response=validate_model_plan(value(),source,{'parser':'llm'})
    assert response['status']=='needs_clarification' and '48 小时' in response['message']
    assert '2026-10-13T00:00+08:00' in response['message']
    assert trace['error_code']=='plan_observation_pending'
    edge=value(time_range={'start':'2026-08-15T00:00:00+08:00','end_exclusive':'2026-10-13T00:00:00+08:00'})
    assert isinstance(validate_model_plan(edge,source,{}),PushPlan)
    edge['time_range']['end_exclusive']='2026-10-13T00:00:01+08:00'
    assert validate_model_plan(edge,source,{})['status']=='needs_clarification'


@pytest.mark.parametrize('patch,code',[
    ({'time_range':{'start':'2026-03-01T00:00:00+08:00','end_exclusive':'2026-08-01T00:00:00+08:00'}},'source_start'),
    ({'time_range':{'start':'2026-09-01T00:00:00+08:00','end_exclusive':'2026-11-01T00:00:00+08:00'}},'source_cutoff'),
    ({'metrics':['sent','devices']},'mixed_grain'),
    ({'metrics':['active_devices'],'dimensions':['manufacturer'],'as_of':'2026-09-01T00:00:00+08:00'},'activity_as_of'),
])
def test_semantic_issues_become_specific_clarifications(source,patch,code):
    with telemetry.capture(None) as trace:
        result=validate_model_plan(value(**patch),source,{})
    assert result['status']=='needs_clarification' and trace['error_code']=='plan_'+code


def test_device_as_of_remains_valid_without_message_observation_restriction(source):
    result=validate_model_plan(value(metrics=['devices'],dimensions=['manufacturer'],as_of='2026-10-15T00:00:00+08:00'),source,{})
    assert isinstance(result,PushPlan) and result.device


def test_missing_model_observation_does_not_use_plan_default(source):
    proposed=value()
    proposed.pop('observation_hours')
    with telemetry.capture(None) as trace:
        result=validate_model_plan(proposed,source,{'parser':'llm'})
    assert result['status']=='needs_clarification' and '默认值' in result['message']
    assert trace['error_code']=='plan_missing_observation'


def test_transport_error_message_does_not_depend_on_telemetry_context(source,monkeypatch):
    model(monkeypatch,status=401)
    with pytest.raises(HTTPException) as raised:
        run_push_query(request(),source)
    assert '鉴权失败' in raised.value.detail


@pytest.mark.parametrize('content,finish,status,error,code',[
    ('not json','stop',200,None,'model_json'),
    ('{}','length',200,None,'model_truncated'),
    ('[]','stop',200,None,'model_envelope'),
    ('{"plan":{},"clarification":"x"}','stop',200,None,'model_envelope'),
    ('{}','stop',401,None,'model_auth'),
    ('{}','stop',429,None,'model_rate_limit'),
    ('{}','stop',500,None,'model_http'),
    ('{}','stop',200,httpx.ReadTimeout('provider-secret'),'model_timeout'),
    ('{}','stop',200,httpx.ConnectError('provider-secret'),'model_network'),
])
def test_model_failures_are_classified_without_retry(source,monkeypatch,content,finish,status,error,code):
    calls=model(monkeypatch,content=content,finish=finish,status=status,error=error)
    with telemetry.capture(None) as trace:
        with pytest.raises(HTTPException) as raised:
            run_push_query(request(),source)
    assert trace['error_code']==code and len(calls)==1
    assert 'provider-secret' not in raised.value.detail and '不自动重发' in raised.value.detail


def test_unknown_fields_are_rejected_without_leaking_keys_or_values(source,monkeypatch):
    logs=[]
    monkeypatch.setattr(telemetry.logger,'info',logs.append)
    with telemetry.capture(None) as trace:
        with pytest.raises(HTTPException):
            validate_model_plan(value(**{'secret-key':'secret-value'}),source,{})
    assert trace['error_code']=='model_plan_fields' and trace['error_fields']==['<extra>']
    assert 'secret-key' not in logs[0] and 'secret-value' not in logs[0]


def test_live_query_scorer_rejects_wrong_filters_and_extra_metrics():
    from copy import deepcopy
    from agent.tools.eval_push_queries import mismatches
    from evals.push_queries import CASES
    case=CASES[3]
    actual={'status':'completed','plan':deepcopy(case['plan'])}
    assert mismatches(case,actual)==[]
    actual['plan']['filters']=[]
    actual['plan']['metrics'].append('delivered')
    assert set(mismatches(case,actual))=={'filters','metrics'}


def test_live_runner_stops_on_rate_limit_and_cannot_reuse_ledger(monkeypatch,tmp_path):
    from agent.tools.eval_push_queries import run
    calls=model(monkeypatch,status=429)
    results=run(tmp_path/'report',2)
    assert len(calls)==len(results)==1
    assert results[0]['error_code']=='model_rate_limit'
    assert len((tmp_path/'report/requests.jsonl').read_text().splitlines())==1
    with pytest.raises(FileExistsError):
        run(tmp_path/'report',2)
    assert len(calls)==1


def test_live_runner_respects_request_interval(monkeypatch,tmp_path):
    from agent.tools import eval_push_queries as evaluation
    calls=model(monkeypatch,{'clarification':'请确认起止时间'})
    sleeps=[]
    monkeypatch.setattr(evaluation.time,'sleep',sleeps.append)
    evaluation.run(tmp_path/'paced',2)
    assert len(calls)==2 and len(sleeps)==1 and sleeps[0]>19


def test_live_runner_valid_plan_uses_current_executor_without_database(monkeypatch,tmp_path):
    from agent.tools.eval_push_queries import run
    from evals.push_queries import CASES
    case=CASES[0]
    calls=model(monkeypatch,{'plan':case['plan']})
    results=run(tmp_path/'valid',1,[case['id']])
    assert len(calls)==1 and results[0]['passed'] and results[0]['actual']['rows']==[]
