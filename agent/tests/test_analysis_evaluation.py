"""独立预期与评测分母检查；不把未执行用例算作成功。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from evals.analysis.dataset import ROOT, load
from evals.analysis.oracle import expected, score
from evals.analysis.report import paired_difference, summary
from scripts.evaluate_analysis import baseline_complete, parameter_correct, negative_behavior_correct


def test_intent_splits_and_manual_annotation_status():
    dataset = load()
    assert len(dataset["cases"]) == 80
    assert len({c["group"] for c in dataset["cases"]}) == 40
    assert dataset["annotation_status"] == "draft"
    assert {c["split"] for c in dataset["cases"]} == {"development", "acceptance"}


def test_oracle_from_raw_orders_and_score_numeric_tampering():
    period = {"start": "2018-07-01", "end_exclusive": "2018-08-01"}
    spec = {"goal": "top_n_total", "current": period, "n": 2, "checks": ["orders", "aov", "share"]}
    gold = expected(ROOT / "orders.csv", spec)
    assert gold["states"] == ["MG", "RJ"] and gold["total"] == "1600"
    report = {"complete": True, "total": {"states": gold["states"], "value": "1600"},
              "extras": {k: {"value": v} for k, v in gold["extras"].items()}}
    assert score(report, gold)
    report["total"]["value"] = "1601"
    assert not score(report, gold)
    assert not expected(ROOT / "edge-orders.csv", spec)["complete"]


def test_failure_and_not_run_stay_in_complete_task_denominator():
    result = summary([
        {"expected_complete": True, "complete_success": True, "duration_ms": 10, "tool_proposals": 2, "correct_parameters": 2, "cost_known": True},
        {"expected_complete": True, "complete_success": False, "duration_ms": 30, "tool_proposals": 2, "correct_parameters": 1, "unknown_calls": 1},
        {"expected_complete": True, "status": "not_run"},
    ])
    assert result["complete_success_rate"] == 1 / 3
    assert result["parameter_correct_rate"] == 3 / 4
    assert result["not_run"] == 1 and result["latency_ms"]["p95"] == 30
    assert not result["total_cost_known"]
    assert result['reserved_and_known_cost_micros'] is None
    assert summary([])["complete_success_rate"] is None


def test_baseline_not_penalized_for_single_state_and_sql_timeout_not_bad_parameters():
    spec={'goal':'top_n_total','current':{'start':'2018-07-01','end_exclusive':'2018-08-01'},'n':1,'checks':[]}
    plan={'metrics':['gmv'],'dimensions':['state'],'time_range':spec['current'],'filters':[],
          'order_by':[{'field':'gmv','direction':'desc'}],'top_n':1}
    task={'status':'completed','result':{'plan':plan,'rows':[{'dimensions':{'state':'MG'},'values':{'gmv':{'value':'800'}}}]}}
    gold={'complete':True,'goal':'top_n_total','states':['MG'],'total':'800','extras':{}}
    assert baseline_complete(task,spec,gold)
    spec['n']=2
    assert not baseline_complete(task,spec,gold)
    spec['n']=1
    step={'status':'failed','error_code':'sql_timeout','action':'query_metrics','arguments':{'plan':plan}}
    assert parameter_correct(step,{'spec':spec},gold)
    step['arguments']['plan']['time_range']={'start':'2017-01-01','end_exclusive':'2017-02-01'}
    assert not parameter_correct(step,{'spec':spec},gold)


def test_paired_report_rejects_different_task_sets():
    a=[{'id':'one','repeat':0,'intent_group':'g1','expected_complete':True,'complete_success':False}]
    b=[{**a[0],'complete_success':True}]
    assert paired_difference(a,b)['difference']==1
    assert not paired_difference(a,[])['available']


def test_wrong_topn_and_order_are_not_correct_tool_parameters():
    case = next(c for c in load()['cases'] if c['id'] == 'analysis-14-1')
    plan = {'time_range':case['spec']['current'], 'metrics':['gmv'], 'dimensions':['state'], 'filters':[],
            'top_n':1, 'order_by':[{'field':'gmv','direction':'desc'}]}
    step = {'status':'completed', 'action':'query_metrics', 'arguments':{'plan':plan}}
    assert not parameter_correct(step, case, {'states':['SP','RJ','MG']})
    plan['top_n'] = 3
    assert parameter_correct(step, case, {})
    plan['order_by'][0]['direction'] = 'asc'
    assert not parameter_correct(step, case, {})


def test_negative_case_requires_domain_evidence_not_gateway_failure():
    case = next(c for c in load()['cases'] if c['id'] == 'analysis-37-1')
    run = {'status':'failed', 'state':{'feedback':{'error_code':'unknown_execution'}}, 'steps':[]}
    assert not negative_behavior_correct(run, {'unknown_calls':1}, case, 'adaptive', {'complete':False})
    run = {'status':'needs_clarification', 'state':{}, 'steps':[{'phase':'tool', 'action':'request_clarification',
           'status':'completed', 'arguments':{'missing_fields':['expression']}}]}
    assert negative_behavior_correct(run, {'unknown_calls':0,'sql_calls':0}, case, 'adaptive', {'complete':False})
    run['steps'][0]['arguments']['missing_fields'] = ['current']
    assert not negative_behavior_correct(run, {'sql_calls':0}, case, 'adaptive', {'complete':False})
    assert not negative_behavior_correct({'status':'failed'}, {'sql_calls':0,'error_codes':['model_http']}, case, 'baseline', {})
    assert negative_behavior_correct({'status':'needs_clarification'}, {'sql_calls':0,'error_codes':['unsupported']}, case, 'baseline', {})


def test_no_data_behavior_requires_correct_scope_and_observed_empty_query():
    case = next(c for c in load()['cases'] if c['id'] == 'analysis-29-1')
    plan = {'time_range':case['spec']['current'], 'metrics':['gmv'], 'dimensions':['state'], 'filters':[], 'top_n':None}
    run = {'status':'needs_clarification','state':{'spec':case['spec']},
           'steps':[{'phase':'tool','action':'query_metrics','status':'completed','arguments':{'plan':plan}}],
           'artifacts':[{'kind':'query','data':{'outcome':'no_data'}}]}
    assert negative_behavior_correct(run, {}, case, 'adaptive', {'complete':False})
    run['state']['feedback'] = {'error_code':'input_limit'}
    assert not negative_behavior_correct(run, {}, case, 'adaptive', {})
    run['state'].pop('feedback')
    run['artifacts'] = []
    assert not negative_behavior_correct(run, {}, case, 'adaptive', {})


def test_pilot_selects_entire_groups_and_exported_source_is_bound(monkeypatch, tmp_path):
    import pytest
    from scripts import run_analysis_study as study
    selected = study.select_cases(load(), 'development', ['intent-01', 'intent-13'])
    assert {c['id'] for c in selected} == {'analysis-01-1','analysis-01-2','analysis-13-1','analysis-13-2'}
    with pytest.raises(ValueError, match='intent_group_not_in_split'):
        study.select_cases(load(), 'development', ['intent-02'])
    monkeypatch.setattr(study, 'ROOT', tmp_path)
    monkeypatch.delenv('STUDY_SOURCE_COMMIT', raising=False)
    with pytest.raises(ValueError, match='exported_commit_identity_required'):
        study.source_identity()
    monkeypatch.setenv('STUDY_SOURCE_COMMIT', 'a'*40)
    (tmp_path/'agent').mkdir()
    (tmp_path/'agent/test.py').write_text('version1')
    before = study.source_identity()
    (tmp_path/'agent/test.py').write_text('version2')
    assert before['execution_tree_sha256'] != study.source_identity()['execution_tree_sha256']


def test_compare_checks_equivalence_regrade_preserves_original_and_rejects_share():
    import hashlib
    from copy import deepcopy
    from scripts.evaluate_analysis import spec_correct
    from scripts.regrade_analysis_study import regrade
    case = next(c for c in load()['cases'] if c['id']=='analysis-01-2')
    spec = {**case['spec'],'checks':['orders','aov']}
    assert spec_correct(spec, case['spec'])
    assert not spec_correct({**spec,'checks':['share']}, case['spec'])
    assert not spec_correct({**spec,'n':3}, case['spec'])
    gold = expected(ROOT/case['fixture'],case['spec'])
    result = dict(id=case['id'],repeat=0,intent_group=case['group'],category=case['category'],expected_complete=True,
                  status='completed',complete_success=False,behavior_correct=False,observed_spec=spec,
                  observed_report={'complete':True,**gold},cost_known=True)
    original = {'completed':True,'source_commit':'a'*40,'dataset_sha256':hashlib.sha256((ROOT/'cases.json').read_bytes()).hexdigest(),
                'groups':{name:{'results':[deepcopy(result)],'summary':summary([result])} for name in ['baseline','adaptive','fixed']}}
    corrected = regrade(original)
    assert all(not g['results'][0]['complete_success'] for g in original['groups'].values())
    assert corrected['groups']['adaptive']['summary']['complete_successes']==1
    assert corrected['groups']['fixed']['summary']['complete_successes']==1
    assert corrected['groups']['baseline']['summary']['complete_successes']==0
    assert corrected['groups']['adaptive']['original_frozen_summary']['complete_successes']==0
    assert corrected['posthoc_regrade']['model_calls_added']==0
    bad = deepcopy(original)
    bad['groups']['adaptive']['results'][0]['observed_report']['rows'][0]['values']['gmv']['delta']='9999'
    assert regrade(bad)['groups']['adaptive']['summary']['complete_successes']==0
