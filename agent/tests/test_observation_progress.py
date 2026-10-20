"""重复读取反例：压缩必须返回实际行，改变分页参数不能伪装成新证据。"""
import json
from uuid import uuid4

import httpx
import pytest

from agent.runtime.analysis import AnalysisError, artifact, canonical
from agent.runtime.exploration import evidence_summaries, tool
from agent.runtime.model_transport import ModelTransport
from agent.tests.test_exploration import contract,evidence,request
from agent.tests.test_open_tasks import model_reply
from agent.tests.test_push import push_platform,bundle,run_once
from agent.tests.test_python_platform import platform
from agent.tests.test_query_program import source


def test_latest_inspection_keeps_real_values_under_tight_preview_budget():
    items=[evidence('baseline'),evidence('current')]
    compared=tool(request({'action':'compare_periods','arguments':{
        'baseline_ref':items[0]['result_ref'],'current_ref':items[1]['result_ref']}},items),source())['artifact']
    items.append(compared)
    for limit in [5,20,1]:
        items.append(tool(request({'action':'inspect_result','arguments':{'result_ref':compared['result_ref'],'limit':limit}},items),source())['artifact'])
    result=evidence_summaries(items,max_bytes=1400)
    assert len(canonical(result).encode())<=1400
    assert [x['ref'] for x in result]==['e1','e2','e3','e4','e5','e6']
    assert result[-1]['preview'][0]['source_row']==0
    assert result[-1]['preview'][0]['values']['click_rate']['delta']=='-2.00'
    with pytest.raises(AnalysisError,match='最近读取内容'):
        evidence_summaries(items,max_bytes=100)


def test_changing_inspection_page_size_cannot_exhaust_model_budget(push_platform,monkeypatch):
    client,repo,_=push_platform;calls=[]
    def reply(request):
        value=json.loads(json.loads(request.content)['messages'][1]['content']);calls.append(value)
        n=len(calls)
        if n<=2:action={'action':'query_metrics','arguments':{'period':['baseline','current'][n-1],'metrics':['click_rate']}}
        elif n==3:action={'action':'compare_periods','arguments':{'baseline_ref':'e1','current_ref':'e2'}}
        else:
            if n>=5:
                observed=next(e for e in reversed(value['evidence']) if e['kind']=='observation')
                assert observed['preview'],value
            action={'action':'inspect_result','arguments':{'result_ref':'e3','limit':{4:5,5:20,6:1}[n]}}
        return model_reply({'action':action})
    monkeypatch.setattr(ModelTransport,'bounded_client',lambda self,**_:httpx.Client(transport=httpx.MockTransport(reply)))
    task=client.post('/api/analysis-runs',json={'question':'检查重复读取','request_id':str(uuid4()),'spec':contract().model_dump(mode='json')}).json()
    run_once(repo);result=client.get('/api/analysis-runs/'+task['id']).json()
    assert result['status']=='failed'
    assert result['state']['feedback']['error_code']=='no_progress'
    assert len(calls)==result['state']['model_calls']==6
    assert result['state']['sql_calls']==2
    assert any(a['kind']=='report' and a['data']['completeness']=='partial' for a in result['artifacts'])
    assert any('本期' in step['description'] and '点击' in step['description'] for step in result['steps'])
