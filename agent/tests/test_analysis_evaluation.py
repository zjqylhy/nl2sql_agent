"""独立预期与评测分母检查；不把未执行用例算作成功。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from evals.analysis.dataset import ROOT, load
from evals.analysis.oracle import expected, score
from evals.analysis.report import paired_difference, summary
from scripts.evaluate_analysis import baseline_complete, parameter_correct


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
    step={'status':'failed','error_code':'sql_timeout','action':'query_metrics','arguments':{'plan':plan}}
    assert parameter_correct(step,{'spec':spec},gold)
    step['arguments']['plan']['time_range']={'start':'2017-01-01','end_exclusive':'2017-02-01'}
    assert not parameter_correct(step,{'spec':spec},gold)


def test_paired_report_rejects_different_task_sets():
    a=[{'id':'one','repeat':0,'intent_group':'g1','expected_complete':True,'complete_success':False}]
    b=[{**a[0],'complete_success':True}]
    assert paired_difference(a,b)['difference']==1
    assert not paired_difference(a,[])['available']
