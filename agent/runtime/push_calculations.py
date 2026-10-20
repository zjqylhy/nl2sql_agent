"""对完整推送证据计算，保留分子分母，不对预览和比率做简单平均。"""
from copy import deepcopy
from decimal import Decimal

from .analysis import canonical
from .push import DEFINITIONS, values_for


def aggregate(data, dimensions):
    result = deepcopy(data)
    metrics = [m['id'] for m in data['metrics']]
    groups = {}
    for row in data['rows']:
        dims = {d:row['dimensions'][d] for d in dimensions}
        key = canonical(dims)
        group = groups.setdefault(key, {'dimensions':dims, 'components':{}})
        for metric, value in row['components'].items():
            group['components'][metric] = group['components'].get(metric, Decimal(0)) + Decimal(value)
    if not dimensions and not groups:
        groups['{}'] = {'dimensions':{},'components':{}}
    rows = []
    for key in sorted(groups):
        row = groups[key]
        rows.append({**row, 'components':{k:str(v) for k,v in row['components'].items()},
                     'label':' / '.join(row['dimensions'].values()) or '整体', 'values':values_for(row['components'], metrics)})
    result['rows'] = rows
    result['dimensions'] = [d for d in data['dimensions'] if d['id'] in dimensions]
    result['plan'] = {**data['plan'], 'dimensions':dimensions}
    components = {k:sum(Decimal(r['components'].get(k,'0')) for r in rows) for k in {k for r in rows for k in r['components']}}
    result['totals'] = values_for(components, metrics)
    return result


def select(data, step):
    result = deepcopy(data)
    eligible = [r for r in data['rows'] if r['values'][step.metric][step.value_field] is not None]
    # 稳定排序先固定同值顺序，再按指标排序；不逆转同值的标识顺序。
    eligible.sort(key=lambda r: tuple(r['dimensions'][x['field']] for x in step.tie_breakers))
    eligible.sort(key=lambda r: Decimal(r['values'][step.metric][step.value_field]), reverse=step.direction == 'desc')
    result['rows'] = eligible[:step.limit]
    result['selection'] = dict(metric=step.metric, direction=step.direction, limit=step.limit,
                               excluded_no_sample=len(data['rows'])-len(eligible), candidate_count=len(data['rows']))
    result['totals'] = None if data.get('comparison') else aggregate(result, [])['totals']
    return result


def filter_rows(data, predicate):
    import operator
    compare = {'lt':operator.lt,'lte':operator.le,'gt':operator.gt,'gte':operator.ge,'eq':operator.eq}[predicate.comparison]
    result = deepcopy(data)
    result['rows'] = [row for row in data['rows'] if row['values'][predicate.metric][predicate.field] is not None
                      and compare(Decimal(row['values'][predicate.metric][predicate.field]),Decimal(predicate.value))]
    result['totals'] = None if data.get('comparison') else aggregate(result,[])['totals']
    return result


def compare_data(left, right):
    result = deepcopy(right)
    groups = [{canonical(r['dimensions']):r for r in data['rows']} for data in (left,right)]
    rows = []
    for key in sorted(set(groups[0]) | set(groups[1])):
        a, b = [g.get(key) for g in groups]
        sample = a or b
        values = {}
        for metric in [m['id'] for m in right['metrics']]:
            av, bv = [(r or {}).get('values', {}).get(metric, {}).get('value') for r in (a,b)]
            delta = Decimal(bv)-Decimal(av) if av is not None and bv is not None else None
            values[metric] = {'baseline':av, 'current':bv, 'delta':str(delta) if delta is not None else None,
                'rate':str(delta/Decimal(av)*100) if delta is not None and Decimal(av) else None,
                'delta_unit':'百分点' if DEFINITIONS[metric]['unit'] == '%' else DEFINITIONS[metric]['unit']}
        rows.append({'dimensions':sample['dimensions'], 'label':sample['label'], 'values':values})
    result.update(rows=rows, totals=None, baseline_plan=left['plan'], baseline=left['period'], comparison=True)
    return result
