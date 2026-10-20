"""执行前推导完整计划的字段、范围与依赖；检查函数不调用模型或数据库。"""
import hashlib
from dataclasses import dataclass
from typing import Annotated, Literal, Union

from pydantic import Field

from .analysis import canonical
from .catalog import StrictModel
from .push import DEFINITIONS, PushFilter, compile_push, required_metrics
from .push_values import ValuesPlan, compile_values
from .task_contracts import RowPredicate, Selection, TaskContract, validate_contract


class ProgramError(ValueError):
    def __init__(self, field, message):
        self.field = field
        super().__init__(f'{field}：{message}')


class FilterFrom(StrictModel):
    field: str
    op: Literal['in'] = 'in'
    step: str
    column: str
    on_empty: Literal['return_empty'] = 'return_empty'


class QueryStep(StrictModel):
    id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,39}$')
    op: Literal['query']
    period: str
    metrics: list[str] = Field(min_length=1, max_length=12)
    dimensions: list[str] = Field(default_factory=list, max_length=2)
    filters: list[PushFilter] = Field(default_factory=list, max_length=4)
    filters_from: list[FilterFrom] = Field(default_factory=list, max_length=4)


class SortStep(StrictModel):
    id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,39}$')
    op: Literal['sort_limit']
    input: str
    metric: str
    direction: Literal['asc', 'desc']
    limit: int = Field(ge=1, le=1000, strict=True)
    nulls: Literal['exclude'] = 'exclude'
    value_field: Literal['value','delta','rate'] = 'value'
    tie_breakers: list[dict[str, str]] = Field(min_length=1, max_length=2)


class ValuesStep(StrictModel):
    id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,39}$')
    op: Literal['query_values']
    period: str
    dimensions: list[str] = Field(min_length=1,max_length=2)
    filters: list[PushFilter] = Field(default_factory=list,max_length=4)


class AggregateStep(StrictModel):
    id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,39}$')
    op: Literal['aggregate']
    input: str
    dimensions: list[str] = Field(default_factory=list, max_length=2)


class CompareStep(StrictModel):
    id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,39}$')
    op: Literal['compare']
    baseline: str
    current: str


class FilterStep(StrictModel):
    id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,39}$')
    op: Literal['filter_rows']
    input: str
    predicate: RowPredicate


Step = Annotated[Union[QueryStep, ValuesStep, SortStep, AggregateStep, CompareStep, FilterStep], Field(discriminator='op')]


class Output(StrictModel):
    step: str
    metrics: list[str] = Field(default_factory=list, max_length=12)
    dimensions: list[str] = Field(default_factory=list, max_length=2)


class Program(StrictModel):
    schema_version: Literal[1] = 1
    steps: list[Step] = Field(min_length=1, max_length=20)
    outputs: list[Output] = Field(min_length=1, max_length=4)


@dataclass
class ValidatedProgram:
    program: Program
    order: list[str]
    descriptions: dict
    plan_hash: str


def dependencies(step):
    if isinstance(step,ValuesStep):
        return set()
    if isinstance(step, QueryStep):
        return {x.step for x in step.filters_from}
    if isinstance(step, CompareStep):
        return {step.baseline, step.current}
    return {step.input}


def validate_program(contract: TaskContract, source, allowed):
    validate_contract(contract, source, allowed)
    if contract.task_type != 'query':
        raise ProgramError('task_type', '此执行器只处理明确取数')
    program = Program.model_validate(contract.execution_plan)
    steps = {s.id:s for s in program.steps}
    if len(steps) != len(program.steps):
        raise ProgramError('steps.id', '步骤编号重复')
    order, visiting = [], set()
    def visit(key):
        if key not in steps:
            raise ProgramError(key, '引用不存在的步骤')
        if key in visiting:
            raise ProgramError(key, '存在循环依赖')
        if key in order:
            return
        visiting.add(key)
        for parent in sorted(dependencies(steps[key])):
            visit(parent)
        visiting.remove(key)
        order.append(key)
    for output in program.outputs:
        visit(output.step)
    if set(order) != set(steps):
        raise ProgramError('steps', '存在不服务最终输出的多余步骤')
    if sum(isinstance(s, (QueryStep,ValuesStep)) for s in program.steps) > 6:
        raise ProgramError('steps', 'SQL 查询数超过 6 次')
    if sum(isinstance(s, (QueryStep,ValuesStep)) for s in program.steps) > 1 and not source.dataset_revision:
        raise ProgramError('scope', '多查询依赖需要已发布的不可变数据版本')
    descriptions = {}
    for key in order:
        step = steps[key]
        if isinstance(step, ValuesStep):
            if step.period not in contract.scope.periods:
                raise ProgramError(key,'引用未约定的期间')
            plan = ValuesPlan(dimensions=step.dimensions,time_range=contract.scope.periods[step.period],filters=[*contract.scope.filters,*step.filters])
            plan.check_source(source)
            compile_values(plan)
            if 'sent' not in allowed:
                raise ProgramError(key,'维度取值所需发送记录权限未授权')
            descriptions[key]=dict(metrics=[],dimensions=step.dimensions,periods=[step.period],
                filters=[f.model_dump(mode='json') for f in plan.filters],selection=None,max_rows=1000,
                kind='dimension_values',observation_hours=None,device=False)
        elif isinstance(step, QueryStep):
            plan = contract.plan_for(step.period, step.metrics, step.dimensions, step.filters)
            plan.check_source(source)
            if not set(required_metrics(plan)) <= set(allowed):
                raise ProgramError(key, '指标及依赖未授权')
            compile_push(plan)
            selected = None
            for ref in step.filters_from:
                parent = descriptions[ref.step]
                if ref.column not in parent['dimensions'] or ref.field != ref.column:
                    raise ProgramError(f'{key}.filters_from', '引用列不存在或标识含义不匹配')
                if ref.field not in {'app_id','channel','manufacturer','failure_reason','template_id'}:
                    raise ProgramError(f'{key}.filters_from', '不能把该列作为查询筛选')
                if parent['max_rows'] > 1000:
                    raise ProgramError(key, '前序参数可能超过筛选数量上限')
                if not parent['selection'] or parent['periods'] != [step.period]:
                    raise ProgramError(key, '选组需来自同期间已定义的排序结果')
                if selected is not None:
                    raise ProgramError(key, '第一阶段每个查询只支持一个动态选组来源')
                selected = parent['selection']
                if parent['filters'] != [f.model_dump(mode='json') for f in contract.scope.filters]:
                    raise ProgramError(key, '选组候选不是约定的完整筛选范围')
            descriptions[key] = dict(metrics=step.metrics, dimensions=step.dimensions, periods=[step.period],
                filters=[f.model_dump(mode='json') for f in plan.filters], selection=selected, max_rows=1000,
                kind='query', observation_hours=plan.observation_hours, device=plan.device)
        elif isinstance(step,FilterStep):
            parent = descriptions[step.input]
            predicate = step.predicate
            valid_fields = {'delta','rate'} if parent['kind']=='comparison' else {'value'}
            if predicate.metric not in parent['metrics'] or predicate.field not in valid_fields or parent.get('row_filter'):
                raise ProgramError(key,'行筛选指标或数值项不适用，或重复筛选')
            descriptions[key] = {**parent,'row_filter':predicate.model_dump(mode='json')}
        elif isinstance(step, SortStep):
            parent = descriptions[step.input]
            valid_fields = {'delta','rate'} if parent['kind']=='comparison' else {'value'}
            if step.value_field not in valid_fields or step.metric not in parent['metrics'] or not parent['dimensions']:
                raise ProgramError(key, '排序指标不存在或输入不支持该排序')
            if step.tie_breakers != [{'field':d,'direction':'asc'} for d in parent['dimensions']]:
                raise ProgramError(key, '同值时必须按全部分组标识升序，保证顺序确定')
            if len(parent['dimensions']) != 1 or parent['selection']:
                raise ProgramError(key, '第一阶段只支持对单维完整候选选组')
            selection = Selection(field=parent['dimensions'][0], rank_metric=step.metric, direction=step.direction,
                                  limit=step.limit, nulls=step.nulls, tie_breakers=step.tie_breakers,
                                  value_field=step.value_field,filter=parent.get('row_filter'))
            descriptions[key] = {**parent, 'max_rows':step.limit, 'selection':selection.model_dump(mode='json')}
        elif isinstance(step, AggregateStep):
            parent = descriptions[step.input]
            if not set(step.dimensions) <= set(parent['dimensions']) or parent['kind'] != 'query':
                raise ProgramError(key, '汇总不能创建新维度或直接合并比较值')
            if parent['device'] and not {'app_id'} <= set(step.dimensions) and 'app_id' in parent['dimensions']:
                raise ProgramError(key, '跨应用设备计数不能冒充物理设备去重数')
            descriptions[key] = {**parent, 'dimensions':step.dimensions}
        else:
            left, right = descriptions[step.baseline], descriptions[step.current]
            if any(left[k] != right[k] for k in ['metrics','dimensions','filters','selection','observation_hours','device','kind']) or left['kind'] != 'query':
                raise ProgramError(key, '两期粒度、指标、筛选或观察口径不可比较')
            a, b = [contract.scope.periods[d['periods'][0]] for d in (left,right)]
            if a.end_exclusive-a.start != b.end_exclusive-b.start or max(a.start,b.start) < min(a.end_exclusive,b.end_exclusive):
                raise ProgramError(key, '比较期间须等长且不重叠')
            descriptions[key] = {**left, 'periods':left['periods']+right['periods'], 'kind':'comparison'}
    req = contract.result_requirements
    expected_selection = req.selection.model_dump(mode='json') if req.selection else None
    covered = set()
    for output in program.outputs:
        desc = descriptions[output.step]
        if (desc['kind']=='dimension_values') != (req.kind=='dimension_values'):
            raise ProgramError('outputs','维度取值与指标取数的结果类型不符')
        if desc.get('row_filter') and expected_selection is None:
            raise ProgramError('outputs','用户要求完整结果，计划却额外筛掉分组')
        if output.metrics != req.metrics or output.dimensions != req.dimensions or desc['metrics'] != req.metrics or desc['dimensions'] != req.dimensions:
            raise ProgramError('outputs', '最终指标或分组未覆盖用户结果要求')
        if desc['selection'] != expected_selection or desc['filters'] != [f.model_dump(mode='json') for f in contract.scope.filters]:
            raise ProgramError('outputs', '选组或实际范围与结果要求不符')
        covered.update(desc['periods'])
    if covered != set(req.period_names) or len(req.period_names) != len(set(req.period_names)):
        raise ProgramError('outputs', '交付期间缺失或多出')
    digest = hashlib.sha256(canonical(contract.model_dump(mode='json')).encode()).hexdigest()
    return ValidatedProgram(program, order, descriptions, digest)
