"""离线修正比较任务冗余 checks 的判分；保留冻结原分数，不重跑模型。"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.analysis.dataset import load, ROOT as DATA
from evals.analysis.oracle import expected, score
from evals.analysis.report import summary, paired_difference
from scripts.evaluate_analysis import spec_correct
from scripts.run_analysis_study import source_identity


def regrade(original):
    if not original.get('completed') or original['dataset_sha256'] != hashlib.sha256((DATA/'cases.json').read_bytes()).hexdigest():
        raise ValueError('completed_matching_dataset_required')
    cases = {c['id']:c for c in load()['cases']}
    report, changes = deepcopy(original), []
    for group, values in report['groups'].items():
        values['original_frozen_summary'] = deepcopy(values['summary'])
        if group != 'baseline':
            for result in values['results']:
                case = cases[result['id']]
                spec = case.get('spec')
                if not spec or spec['goal'] != 'compare_declines' or result['status'] != 'completed':
                    continue
                success = spec_correct(result.get('observed_spec'), spec) and score(result.get('observed_report'), expected(DATA/case['fixture'], spec))
                if case['category'] == 'clarification':
                    tools = [s for s in result.get('tool_trace', []) if s['phase']=='tool']
                    clarify = next((i for i,s in enumerate(tools) if s['action']=='request_clarification'), None)
                    query = next((i for i,s in enumerate(tools) if s['action']=='query_metrics'), None)
                    success &= clarify is not None and (query is None or clarify < query)
                if success != result['complete_success']:
                    changes.append({'group':group,'id':result['id'],'before':result['complete_success'],'after':success})
                    result['complete_success'] = success
                    result['behavior_correct'] = success
        values['summary'] = summary(values['results'])
    report['paired_adaptive_minus_baseline'] = paired_difference(report['groups']['baseline']['results'], report['groups']['adaptive']['results'])
    report['posthoc_regrade'] = {'revision':'compare-redundant-checks-v2','model_calls_added':0,'changes':changes,
        'reason':'compare_declines always verifies orders and aov; redundant declarations preserve meaning; share remains invalid',
        'original_execution_source_commit':original['source_commit']}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = regrade(json.loads(args.input.read_text(encoding='utf-8')))
    report['posthoc_regrade'].update(scorer_identity=source_identity(), recorded_at=datetime.now(timezone.utc).isoformat(),
                                    original_report_sha256=hashlib.sha256(args.input.read_bytes()).hexdigest())
    # 新文件保存复核结果，禁止覆盖原始冻结报告。
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(report['posthoc_regrade'], ensure_ascii=False))


if __name__ == '__main__':
    main()
