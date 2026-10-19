"""开放探索按证据运行，范围、统计计算、摘要与报告由程序控制。"""
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter

from agent.runtime.analysis import AnalysisError, artifact
from agent.runtime.exploration import ExplorationAction, compatible, decompose, evidence_summaries, report, ReportArgs, tool
from agent.runtime.push import DEFINITIONS, result_for
from agent.runtime.task_contracts import TaskContract, with_capabilities
from agent.tests.test_query_program import source


def contract():
    return with_capabilities(TaskContract(task_type='analysis',question='核实点击率变化并分析原因',
        scope={'filters':[{'field':'app_id','values':['app_1']}],'periods':{
            'baseline':{'start':'2026-09-01T00:00:00+08:00','end_exclusive':'2026-09-08T00:00:00+08:00'},
            'current':{'start':'2026-09-08T00:00:00+08:00','end_exclusive':'2026-09-15T00:00:00+08:00'}},'observation_hours':24},
        target_metrics=['click_rate'],answer_items=[{'id':'a1','text':'核实变化并说明查过的方向'}]),list(DEFINITIONS))


def evidence(period, dimensions=None):
    spec = contract()
    plan = spec.plan_for(period,['sent','clicked','click_rate'],dimensions or [])
    raw = ([{'d_channel':'huawei_push','c_sent':3500,'c_clicked':350},
            {'d_channel':'fcm','c_sent':3500,'c_clicked':210 if period=='baseline' else 70}]
           if dimensions else [{'c_sent':7000,'c_clicked':560 if period=='baseline' else 420}])
    data = result_for(plan,raw,source(),{},'',[])
    data['period_name'] = period
    return artifact('query',data,data['metrics_used'],'a'*64)


def request(action, items=None):
    return SimpleNamespace(spec=contract(),action=TypeAdapter(ExplorationAction).validate_python(action),
        allowed_metrics=list(DEFINITIONS),binding='a'*64,artifacts=items or [])


def test_group_changes_and_ratio_decomposition_match_independent_expected():
    a,b = [evidence(p,['channel']) for p in ['baseline','current']]
    compatible(a['data'],b['data'])
    result = decompose(a['data'],b['data'],'click_rate')
    assert Decimal(result['delta']) == -2
    by_group = {r['dimensions']['channel']:r['values']['click_rate'] for r in result['rows']}
    assert Decimal(by_group['fcm']['within']) == -2
    assert Decimal(by_group['huawei_push']['value']) == 0
    assert sum(Decimal(r['values']['click_rate']['value']) for r in result['rows']) == -2


def test_structure_change_is_not_average_of_rates_or_causal_proof():
    a,b = [evidence(p,['channel'])['data'] for p in ['baseline','current']]
    for data, counts in [(a,[(800,80),(200,4)]),(b,[(200,20),(800,16)])]:
        for row,(sent,clicked) in zip(data['rows'],counts):
            row['components'].update(sent=str(sent),clicked=str(clicked))
    result = decompose(a,b,'click_rate')
    assert Decimal(result['delta']) == Decimal('-4.8')
    assert sum(Decimal(r['values']['click_rate']['within']) for r in result['rows']) == 0
    assert sum(Decimal(r['values']['click_rate']['structure']) for r in result['rows']) == Decimal('-4.8')
    assert '不证明业务因果' in result['limitations'][0]


def test_query_scope_cannot_be_expanded_or_use_unpublished_field():
    from pydantic import ValidationError
    with pytest.raises((ValueError,AnalysisError)):
        tool(request({'action':'query_metrics','arguments':{'period':'current','metrics':['click_rate'],
             'filters':[{'field':'app_id','values':['app_2']}]}}),source())
    with pytest.raises(ValidationError):
        request({'action':'query_metrics','arguments':{'period':'current','metrics':['click_rate'],'sql':'select *'}})


def test_down_drill_must_use_real_parent_group():
    a = evidence('current',['channel'])
    with pytest.raises(AnalysisError,match='不存在'):
        tool(request({'action':'query_metrics','arguments':{'period':'current','metrics':['click_rate'],
            'parent_ref':a['result_ref'],'parent_group':{'channel':'apns'}}},[a]),source())


def report_args(items):
    return ReportArgs(evidence_refs=[x['result_ref'] for x in items],
        answers=[{'item_id':'a1','status':'answered','text':'两期存在可核查差异，尚需调查业务原因','evidence_refs':[items[0]['result_ref']]}],
        claims=[{'result_ref':items[0]['result_ref'],'metric':'click_rate'}],hypotheses=['可能与消息内容有关，尚未验证'])


def test_report_numbers_are_resolved_from_saved_evidence():
    items = [evidence('baseline'),evidence('current')]
    result = report(report_args(items),items,contract(),'a'*64,source())['data']
    assert result['claims'][0]['value'] == '8.00'
    assert result['completeness'] == 'complete'
    assert result['hypotheses']
    assert len(result['investigated']) == 2


@pytest.mark.parametrize('change',[
    lambda args:args.answers.clear(),
    lambda args:setattr(args.answers[0],'evidence_refs',[]),
    lambda args:setattr(args.claims[0],'result_ref','not_in_task'),
    lambda args:setattr(args.claims[0],'row',100),
    lambda args:setattr(args.answers[0],'text','下降了99%，已查明原因'),
    lambda args:setattr(args.answers[0],'text','发送量变成999，已查明原因'),
])
def test_report_rejects_missing_answers_refs_or_self_written_numbers(change):
    items = [evidence('baseline'),evidence('current')]
    args = report_args(items)
    change(args)
    with pytest.raises(AnalysisError):
        report(args,items,contract(),'a'*64,source())


def test_subgroup_evidence_cannot_claim_complete_whole_scope():
    items = [evidence('baseline'),evidence('current')]
    items[1]['data']['plan']['filters'].append({'field':'channel','op':'in','values':['fcm']})
    result = report(report_args(items),items,contract(),'a'*64,source())['data']
    assert result['completeness']=='partial'


def test_large_result_preview_declares_omission_and_preserves_full_result():
    value = evidence('current',['channel'])
    value['data']['rows'] *= 200
    original = deepcopy(value)
    summaries = evidence_summaries([value],max_bytes=1500)
    assert summaries[0]['omitted_rows'] == 400-len(summaries[0]['preview'])
    assert summaries[0]['omitted_rows']>0 and summaries[0]['selection_rule']
    assert value == original


def test_unknown_problem_type_does_not_require_business_enum():
    value = contract().model_copy(update={'question':'观察厂商设备发送效果的阶段性变化'})
    assert value.task_type == 'analysis'
    assert 'goal' not in value.model_dump()


def test_inspect_saved_result_preserves_original_row_reference():
    item=evidence('current',['channel'])
    result=tool(request({'action':'inspect_result','arguments':{'result_ref':item['result_ref'],'sort_metric':'click_rate',
        'direction':'asc','limit':1}},[item]),source())['artifact']['data']
    assert result['source_ref']==item['result_ref'] and result['coverage']=='preview'
    assert result['rows'][0]['source_row']==1
    assert result['rows'][0]['dimensions']['channel']=='fcm'
    assert result['omitted_rows']==1


def test_decomposition_cannot_fill_unqueried_components_with_zero():
    a,b=[evidence(p,['channel'])['data'] for p in ['baseline','current']]
    with pytest.raises(AnalysisError,match='比率未查询'):
        decompose(a,b,'delivery_rate')


def test_compaction_keeps_all_result_handles_instead_of_forgetting_prior_queries():
    items=[evidence('baseline'),evidence('current'),evidence('baseline',['channel']),evidence('current',['channel'])]
    summaries=evidence_summaries(items,max_bytes=1500)
    assert [x['ref'] for x in summaries]==['e1','e2','e3','e4']
    assert any(x.get('totals_omitted') or x.get('scope_details_omitted') or x['omitted_rows'] for x in summaries)


def test_device_aggregation_preserves_application_grain():
    spec = contract().model_copy(update={'target_metrics':['devices']})
    spec = with_capabilities(spec,list(DEFINITIONS))
    plan = spec.plan_for('current',['devices'],['app_id','manufacturer'])
    data = result_for(plan,[{'d_app_id':'app_1','d_manufacturer':'huawei','c_devices':5}],source(),{},'',[])
    data['period_name']='current'
    item = artifact('query',data,data['metrics_used'],'a'*64)
    args = {'result_ref':item['result_ref'],'dimensions':[]}
    value = request({'action':'aggregate_metrics','arguments':args},[item])
    value.spec = spec
    with pytest.raises(AnalysisError,match='跨应用设备计数'):
        tool(value,source())
    value.action = TypeAdapter(ExplorationAction).validate_python({
        'action':'aggregate_metrics','arguments':{**args,'dimensions':['app_id']}})
    result = tool(value,source())['artifact']['data']
    assert result['rows'][0]['dimensions']=={'app_id':'app_1'}
    assert result['rows'][0]['values']['devices']['value']=='5'
