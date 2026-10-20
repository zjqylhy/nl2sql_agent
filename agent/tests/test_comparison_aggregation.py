"""汇总两期比较必须从两期完整计数重算，不能累加比率差值。"""
from copy import deepcopy
from decimal import Decimal
import pytest
from agent.runtime.analysis import AnalysisError,artifact
from agent.runtime.exploration import tool
from agent.runtime.push import result_for
from agent.tests.test_exploration import contract,request
from agent.tests.test_query_program import source


def evidence_pair():
    items=[]
    for period,sent,clicked in [('baseline',[10,90],[1,81]),('current',[50,50],[5,45])]:
        plan=contract().plan_for(period,['sent','clicked','click_rate'],['manufacturer','failure_reason'])
        raw=[{'d_manufacturer':vendor,'d_failure_reason':'provider_error','c_sent':n,'c_clicked':c}
             for vendor,n,c in zip(['huawei','xiaomi'],sent,clicked)]
        data=result_for(plan,raw,source(),{},'',[]);data['period_name']=period
        items.append(artifact('query',data,data['metrics_used'],'a'*64))
    compared=tool(request({'action':'compare_periods','arguments':{'baseline_ref':items[0]['result_ref'],
        'current_ref':items[1]['result_ref'],'metrics':['click_rate']}},items),source())['artifact']
    return [*items,compared]


def test_comparison_aggregation_recomputes_weighted_ratios_and_supports_further_rollup():
    items=evidence_pair();original=deepcopy(items)
    assert all(Decimal(row['values']['click_rate']['delta'])==0 for row in items[-1]['data']['rows'])
    req=request({'action':'aggregate_metrics','arguments':{'result_ref':items[-1]['result_ref'],'dimensions':['failure_reason']}},items)
    result=tool(req,source())['artifact'];data=result['data'];value=data['rows'][0]['values']['click_rate']
    assert result['kind']=='comparison' and data['comparison']
    assert Decimal(value['baseline'])==82 and Decimal(value['current'])==50 and Decimal(value['delta'])==-32
    assert data['plan']['dimensions']==data['baseline_plan']['dimensions']==['failure_reason']
    assert data['plan']['metrics']==['click_rate'] and set(data['rows'][0]['values'])=={'click_rate'}
    assert data['sources']==[items[0]['result_ref'],items[1]['result_ref']]
    assert data['aggregation_source']==items[-1]['result_ref'] and items==original
    second=tool(request({'action':'aggregate_metrics','arguments':{'result_ref':result['result_ref'],'dimensions':[]}},[*items,result]),source())['artifact']
    assert second['kind']=='comparison' and second['data']['plan']['dimensions']==[]
    assert second['data']['rows'][0]['values']['click_rate']==value


@pytest.mark.parametrize('problem',['missing_source','missing_ref','extra_dimension','observation'])
def test_comparison_aggregation_rejects_invalid_source_or_dimension(problem):
    items=evidence_pair();ref=items[-1]['result_ref'];dims=['failure_reason']
    if problem=='missing_source':
        data=deepcopy(items[-1]['data']);data['sources']=data['sources'][:1]
        items[-1]=artifact('comparison',data,items[-1]['metrics_used'],'a'*64);ref=items[-1]['result_ref']
    elif problem=='missing_ref':items=items[1:]
    elif problem=='extra_dimension':dims=['app_id']
    elif problem=='observation':
        observation=tool(request({'action':'inspect_result','arguments':{'result_ref':ref,'limit':1}},items),source())['artifact']
        items.append(observation);ref=observation['result_ref']
    with pytest.raises(AnalysisError):
        tool(request({'action':'aggregate_metrics','arguments':{'result_ref':ref,'dimensions':dims}},items),source())
