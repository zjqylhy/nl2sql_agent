"""完整计划的静态连接检查与真实值传递；不调用模型和数据库。"""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from agent.runtime.push import DEFINITIONS, result_for
from agent.runtime.query_executor import execute_program
from agent.runtime.query_program import validate_program
from agent.runtime.task_contracts import TaskContract


def source():
    return SimpleNamespace(domain='push',data_start=datetime.fromisoformat('2026-04-01T00:00:00+08:00'),
        data_cutoff=datetime.fromisoformat('2026-10-15T00:00:00+08:00'),dataset_revision='a'*64,is_synthetic=True)


def contract():
    return TaskContract.model_validate({
        'task_type':'query','question':'按到达率选最低通道，再查每日厂商发送和到达率',
        'scope':{'filters':[{'field':'app_id','values':['app_1']}],
            'periods':{'current':{'start':'2026-09-08T00:00:00+08:00','end_exclusive':'2026-09-15T00:00:00+08:00'}},'observation_hours':24},
        'result_requirements':{'metrics':['sent','delivery_rate'],'dimensions':['manufacturer','day'],'period_names':['current'],
            'selection':{'field':'channel','rank_metric':'delivery_rate','direction':'asc','limit':1,'tie_breakers':[{'field':'channel','direction':'asc'}]}},
        'execution_plan':{'schema_version':1,'steps':[
            {'id':'A','op':'query','period':'current','dimensions':['channel'],'metrics':['sent','delivery_rate']},
            {'id':'B','op':'sort_limit','input':'A','metric':'delivery_rate','direction':'asc','limit':1,'tie_breakers':[{'field':'channel','direction':'asc'}]},
            {'id':'C','op':'query','period':'current','dimensions':['manufacturer','day'],'metrics':['sent','delivery_rate'],
             'filters_from':[{'field':'channel','step':'B','column':'channel'}]}],
            'outputs':[{'step':'C','metrics':['sent','delivery_rate'],'dimensions':['manufacturer','day']}]}})


def test_valid_program_infers_semantic_columns_and_topological_order():
    value = contract()
    value.execution_plan['steps'].reverse()
    checked = validate_program(value,source(),list(DEFINITIONS))
    assert checked.order == ['A','B','C']
    assert checked.descriptions['B']['max_rows'] == 1
    assert checked.descriptions['C']['selection']['field'] == 'channel'


@pytest.mark.parametrize('change',[
    lambda v:v.execution_plan['steps'][1].update(input='C'),
    lambda v:v.execution_plan['steps'][1].update(input='missing'),
    lambda v:v.execution_plan['steps'][2]['filters_from'][0].update(column='device_id'),
    lambda v:v.execution_plan['steps'][2]['filters_from'][0].update(field='manufacturer'),
    lambda v:v.execution_plan['steps'][2].update(dimensions=['day']),
    lambda v:v.execution_plan['steps'][0].update(op='run_sql',sql='select 1'),
    lambda v:v.execution_plan['steps'][0].update(output_columns=['channel','device_id']),
    lambda v:v.execution_plan['steps'][0].update(metrics=['sent']),
    lambda v:v.execution_plan['steps'][0].update(filters=[{'field':'app_id','values':['app_2']}]),
    lambda v:v.execution_plan['steps'][1].update(limit=1001),
    lambda v:v.execution_plan['steps'].append(deepcopy(v.execution_plan['steps'][0])),
    lambda v:v.execution_plan['outputs'][0].update(metrics=['sent']),
    lambda v:setattr(v.result_requirements.selection,'direction','desc'),
])
def test_invalid_entire_plan_never_executes_first_query(change):
    value = contract()
    change(value)
    calls = []
    with pytest.raises(ValueError):
        execute_program(value,source(),list(DEFINITIONS),query=lambda *a:calls.append(a))
    assert calls == []


def query_fixture(calls, empty=False, tamper=False):
    def query(src,plan,allowed,usage):
        calls.append(plan)
        if plan.dimensions == ['channel']:
            rows = [] if empty else [
                {'d_channel':'huawei_push','c_sent':10,'c_delivered':1},
                {'d_channel':'fcm','c_sent':3,'c_delivered':2},
                {'d_channel':'apns','c_sent':0,'c_delivered':0}]
        else:
            assert plan.filters[-1].values == ['huawei_push']
            assert plan.filters[0].values == ['app_1']
            rows = [{'d_manufacturer':'huawei','d_day':'2026-09-08','c_sent':10,'c_delivered':1}]
        data = result_for(plan,rows,src,usage,'',[])
        if tamper:
            data['plan']['filters'] = []
        return data
    return query


def test_steps_bind_saved_values_and_compute_rates_without_model():
    calls = []
    result = execute_program(contract(),source(),list(DEFINITIONS),query=query_fixture(calls))
    assert result['status'] == 'completed'
    assert len(calls) == 2
    assert result['rows'][0]['values']['delivery_rate']['value'] == '10.0'
    assert [x['step_id'] for x in result['steps']] == ['A','B','C']


def test_empty_parent_never_drops_filter_and_queries_whole_scope():
    calls = []
    result = execute_program(contract(),source(),list(DEFINITIONS),query=query_fixture(calls,empty=True))
    assert result['status'] == 'completed' and result['rows'] == []
    assert len(calls) == 1
    assert '无可选分组' in result['message']


def test_runtime_scope_mismatch_stops_before_dependent_query():
    calls = []
    result = execute_program(contract(),source(),list(DEFINITIONS),query=query_fixture(calls,tamper=True))
    assert result['status'] == 'failed'
    assert len(calls) == 1
    assert '实际查询范围' in result['message']


def test_query_failure_does_not_become_zero_or_execute_dependents():
    def failed(*args):
        raise HTTPException(503,'模拟查询超时')
    result = execute_program(contract(),source(),list(DEFINITIONS),query=failed)
    assert result['status']=='failed' and result['completeness']=='none'
    assert not result['partial_outputs']


def test_missing_metric_dependency_rejected_before_query():
    with pytest.raises(ValueError,match='权限'):
        validate_program(contract(),source(),['sent','delivery_rate'])
