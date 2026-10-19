"""明确取数仅运行已检查的计划；步骤之间直接读取完整结果，不再请求模型。"""
from copy import deepcopy
import json
import hashlib
import time

from fastapi import HTTPException
from .analysis import canonical

from .push import PushFilter, execute_push, result_for, values_for
from .push_calculations import aggregate, compare_data, filter_rows, select
from .query_program import AggregateStep, CompareStep, FilterStep, ProgramError, QueryStep, SortStep, validate_program


def check_result(data, description):
    if data.get('status') != 'completed' or data.get('coverage') != 'complete':
        raise ProgramError('result', '步骤未成功或结果不完整')
    if len(data.get('rows', [])) > 1000 or len(json.dumps(data, ensure_ascii=False).encode()) > 524288:
        raise ProgramError('result', '实际结果超过行数或大小上限')
    if [m['id'] for m in data['metrics']] != description['metrics'] or [d['id'] for d in data['dimensions']] != description['dimensions']:
        raise ProgramError('result', '实际指标或分组与推导结构不一致')
    if data['plan']['observation_hours'] != description['observation_hours']:
        raise ProgramError('result', '实际观察口径改变')
    for row in data['rows']:
        if set(row['dimensions']) != set(description['dimensions']) or set(row['values']) != set(description['metrics']):
            raise ProgramError('result', '实际结果字段缺失或多出')
        if description['kind'] != 'comparison' and row['values'] != values_for(row.get('components',{}),description['metrics']):
            raise ProgramError('result','指标值与实际分子分母计算不一致')


def execute_program(contract, source, allowed, *, hooks=None, usage=None, query=None):
    validated = validate_program(contract, source, allowed)
    if hooks:
        hooks.accept(contract, validated)
    results, receipts, started = {}, [], time.monotonic()
    steps = {s.id:s for s in validated.program.steps}
    for key in validated.order:
        step = steps[key]
        try:
            if time.monotonic()-started > 180:
                raise ProgramError(key, '活跃执行时间超过 180 秒')
            current_allowed = hooks.authorize(contract, validated) if hooks else allowed
            validate_program(contract, source, current_allowed)
            saved = hooks.load(key, validated.plan_hash) if hooks else None
            if saved is not None:
                data = saved
            else:
                if hooks:
                    hooks.begin(key, validated.plan_hash, isinstance(step, QueryStep))
                if isinstance(step, QueryStep):
                    filters, empty = list(step.filters), False
                    for ref in step.filters_from:
                        parent = results[ref.step]
                        values = sorted({r['dimensions'][ref.column] for r in parent['rows']})
                        if any(not isinstance(v,str) or not v for v in values) or len(values) > 1000:
                            raise ProgramError(key, '前序筛选值无效或超量')
                        if not values:
                            empty = True
                            break
                        filters.append(PushFilter(field=ref.field, values=values))
                    plan = contract.plan_for(step.period, step.metrics, step.dimensions, filters)
                    data = (result_for(plan, [], source, {}, '', []) if empty else
                            (query or execute_push)(source, plan, current_allowed, usage or {'parser':'program','tokens':0}))
                    if data.get('plan') != plan.model_dump(mode='json') or data.get('period') != plan.time_range.model_dump(mode='json'):
                        raise ProgramError(key,'实际查询范围或参数与已绑定计划不一致')
                    if empty:
                        data['message'] = '无可选分组，未发送无筛选查询。'
                elif isinstance(step, SortStep):
                    data = select(results[step.input], step)
                elif isinstance(step,FilterStep):
                    data = filter_rows(results[step.input],step.predicate)
                elif isinstance(step, AggregateStep):
                    data = aggregate(results[step.input], step.dimensions)
                elif isinstance(step, CompareStep):
                    data = compare_data(results[step.baseline], results[step.current])
                check_result(data, validated.descriptions[key])
                if hooks:
                    hooks.save(key, validated.plan_hash, data)
            check_result(data, validated.descriptions[key])
            results[key] = data
            receipts.append({'step_id':key, 'status':'completed', 'cached':saved is not None,
                             'result_ref':hashlib.sha256(canonical(data).encode()).hexdigest()})
        except (ValueError, HTTPException) as exc:
            if hooks:
                hooks.fail(key, validated.plan_hash, str(getattr(exc,'detail',exc)))
            return partial_result(contract, validated.plan_hash, results, receipts, key, str(getattr(exc,'detail',exc)),persisted=hooks is not None)
    # 再做整份要求检查，不能以“所有步骤调用成功”代替交付验收。
    if hooks:
        validate_program(contract, source, hooks.authorize(contract, validated))
    outputs = [deepcopy(results[o.step]) for o in validated.program.outputs]
    for output, spec in zip(outputs, validated.program.outputs):
        check_result(output, validated.descriptions[spec.step])
    if any(out.get('comparison') and any(v['delta'] is None for row in out['rows'] for v in row['values'].values()) for out in outputs):
        return partial_result(contract, validated.plan_hash, results, receipts, 'final', '部分分组无有效两期样本，无法完整比较',persisted=hooks is not None)
    result = deepcopy(outputs[0])
    result.update(status='completed', task_type='query', completeness='complete',
        contract=contract.model_dump(mode='json'), plan_hash=validated.plan_hash, steps=receipts,
        outputs=outputs if len(outputs)>1 else [], summary='指定查询与计算已完成，结果通过范围、依赖和完整性检查。')
    return result


def partial_result(contract, plan_hash, results, receipts, failed_step, message,*,persisted=False):
    outputs=[]
    for key,data in results.items():
        preview=deepcopy(data)
        if persisted and len(preview['rows'])>20:
            preview.update(rows=preview['rows'][:20],coverage='preview',omitted_rows=len(data['rows'])-20,
                           message='仅展示原始顺序前 20 行，完整数据按结果引用另存。')
        outputs.append({'step_id':key,'result_ref':hashlib.sha256(canonical(data).encode()).hexdigest(),'data':preview})
    return {'status':'failed', 'task_type':'query', 'completeness':'partial' if results else 'none',
        'contract':contract.model_dump(mode='json'), 'plan_hash':plan_hash, 'steps':receipts,
        'partial_outputs':outputs,
        'unanswered':[{'step_id':failed_step, 'reason':message}], 'message':message}
