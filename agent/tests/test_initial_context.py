"""两轮中文补充完整进入模型；本地超限不记为外部调用或可重复重试。"""
import json

import pytest
from fastapi import HTTPException

from agent.runtime import telemetry
from agent.runtime.app import QueryRequest
from agent.runtime.push import DEFINITIONS
from agent.runtime.push_query import parse_query
from agent.tests.test_model_retry import model
from agent.tests.test_push import bundle, push_platform, run_once
from agent.tests.test_python_platform import platform


QUESTION = 'app_3 最近1个月的的到达率、点击率指标是否有下降？如果有，请分析出原因\n'
DIALOGUE = [
    {'question':'请确认两项口径：1）“最近1个月”按 2026-09-15 00:00 至 2026-10-15 00:00（+08:00）统计是否可以；2）到达率、点击率的观察窗口（例如发送后 24 小时内回执）以及与哪个基期对比来判断“下降”（例如上一个月的同期）。',
     'answer':'1）可以 2）观察窗口：发送后 24 小时内回执，基期：当前时间窗口往前再移动1个月'},
    {'question':'按发送后24小时观察，当前统计区间 2026-09-15 00:00 至 2026-10-15 00:00（+08:00）的最后一天回执尚未满24小时，且数据截止为 2026-10-15 00:00。请确认是否将当前统计区间调整为 2026-09-14 00:00 至 2026-10-14 00:00（+08:00），基期相应调整为 2026-08-14 00:00 至 2026-09-14 00',
     'answer':'确认'},
]


def test_screenshot_two_round_context_is_preserved_without_extra_model_compression(push_platform,monkeypatch):
    _,_,source=push_platform
    contract={'task_type':'analysis','question':QUESTION,
        'scope':{'filters':[{'field':'app_id','values':['app_3']}],
                 'periods':{'current':{'start':'2026-09-14T00:00:00+08:00','end_exclusive':'2026-10-14T00:00:00+08:00'},
                            'baseline':{'start':'2026-08-14T00:00:00+08:00','end_exclusive':'2026-09-14T00:00:00+08:00'}},
                 'observation_hours':24},
        'target_metrics':['delivery_rate','click_rate'],
        'answer_items':[{'id':'a1','text':'核实是否下降并调查原因'}]}
    calls=model(monkeypatch,[{'contract':contract}])
    request=QueryRequest(tenant_id='demo',role='admin',allowed_metrics=list(DEFINITIONS),
        input={'question':QUESTION},push_dialogue=DIALOGUE)
    parsed,_=parse_query(request,source)
    assert parsed.scope.filters[0].values==['app_3'] and parsed.scope.observation_hours==24
    assert len(calls)==1
    system,payload=[x['content'] for x in calls[0]['messages']]
    assert len(system.encode())+len(payload.encode())+256<=6000
    assert json.loads(payload)=={'original_question':QUESTION,'clarifications':DIALOGUE}


def test_input_rejected_locally_does_not_count_as_model_call_or_offer_retry(push_platform,monkeypatch):
    client,repo,_=push_platform;calls=model(monkeypatch,[])
    task=client.post('/api/tasks',json={'question':'问'*2000}).json();run_once(repo)
    task=client.get('/api/tasks/'+task['id']).json()
    assert task['status']=='failed' and not task['retry']['available']
    assert '未调用模型' in task['error'] and calls==[]
    with repo.db.transaction() as conn:
        trace=conn.execute('SELECT telemetry FROM task_attempts WHERE task_id=%s',(task['id'],)).fetchone()['telemetry']
        assert trace['model_calls']==trace['sql_calls']==trace['total_tokens']==0
        assert trace['error_code']=='input_budget'


@pytest.mark.parametrize('excess',[0,1])
def test_input_budget_includes_fixed_protocol_and_message_overhead(push_platform,monkeypatch,excess):
    _,_,source=push_platform
    monkeypatch.setattr('agent.runtime.push_query.initial_messages',lambda *_:('a'*100,'b'*(5644+excess)))
    calls=model(monkeypatch,[{'clarification':'请确认日期'}])
    request=QueryRequest(tenant_id='demo',role='admin',allowed_metrics=['sent'],input={'question':'查询发送'})
    with telemetry.capture(None) as trace:
        if excess:
            with pytest.raises(HTTPException,match='未调用模型'):parse_query(request,source)
        else:assert parse_query(request,source)['status']=='needs_clarification'
    assert len(calls)==trace['model_calls']==(0 if excess else 1)


@pytest.mark.parametrize('length',[250,601])
def test_clarification_must_be_complete_never_silently_truncated(push_platform,monkeypatch,length):
    _,_,source=push_platform;question='问'*(length-4)+'结束不含'
    model(monkeypatch,[{'clarification':question}])
    request=QueryRequest(tenant_id='demo',role='admin',allowed_metrics=['sent'],input={'question':'查询发送'})
    if length>600:
        with pytest.raises(HTTPException,match='追问过长'):parse_query(request,source)
    else:
        reply=parse_query(request,source)
        assert reply['message']==question
        QueryRequest.model_validate({**request.model_dump(),'push_dialogue':[{'question':reply['message'],'answer':'确认'}]})
