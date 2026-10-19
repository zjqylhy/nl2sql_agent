"""两类推送任务的约定；用户边界与执行步骤分别保存，不能靠改计划放宽范围。"""
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .catalog import StrictModel
from .push import DEFINITIONS, DIMENSIONS, Period, PushFilter, PushPlan, required_metrics


class Scope(StrictModel):
    filters: list[PushFilter] = Field(default_factory=list, max_length=8)
    periods: dict[str, Period] = Field(min_length=1, max_length=4)
    observation_hours: int = Field(ge=1, le=8760, strict=True)
    as_of: datetime | None = None


class RowPredicate(StrictModel):
    metric: str
    field: Literal['value','delta','rate'] = 'value'
    comparison: Literal['lt','lte','gt','gte','eq']
    value: str = Field(pattern=r'^-?\d+(?:\.\d+)?$')


class Selection(StrictModel):
    field: str
    rank_metric: str
    direction: Literal['asc', 'desc']
    limit: int = Field(ge=1, le=1000, strict=True)
    nulls: Literal['exclude'] = 'exclude'
    value_field: Literal['value','delta','rate'] = 'value'
    filter: RowPredicate | None = None
    tie_breakers: list[dict[str, str]] = Field(default_factory=list, max_length=2)


class ResultRequirements(StrictModel):
    metrics: list[str] = Field(min_length=1, max_length=12)
    dimensions: list[str] = Field(default_factory=list, max_length=2)
    period_names: list[str] = Field(min_length=1, max_length=4)
    selection: Selection | None = None
    coverage: Literal['complete'] = 'complete'


class AnswerItem(StrictModel):
    id: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=300)
    source: Literal['question', 'clarification'] = 'question'


class ExplorationPermissions(StrictModel):
    metrics: list[str] = Field(default_factory=list, max_length=24)
    dimensions: list[str] = Field(default_factory=list, max_length=12)
    filter_fields: list[str] = Field(default_factory=list, max_length=8)


class TaskContract(StrictModel):
    schema_version: Literal[1] = 1
    domain: Literal['push'] = 'push'
    task_type: Literal['query', 'analysis']
    question: str = Field(min_length=1, max_length=2000)
    scope: Scope
    target_metrics: list[str] = Field(default_factory=list, max_length=12)
    exploration_permissions: ExplorationPermissions = Field(default_factory=ExplorationPermissions)
    answer_items: list[AnswerItem] = Field(default_factory=list, max_length=12)
    result_requirements: ResultRequirements | None = None
    execution_plan: dict | None = None
    capability_version: str = 'push-2'

    @model_validator(mode='after')
    def valid(self):
        if self.task_type == 'query' and (not self.result_requirements or not self.execution_plan):
            raise ValueError('明确取数必须同时提供结果要求与完整计划')
        if self.task_type == 'analysis' and (not self.target_metrics or not self.answer_items or self.execution_plan is not None):
            raise ValueError('开放分析需要目标指标和回答事项，不能提交固定执行计划')
        metrics = self.target_metrics or self.result_requirements.metrics
        if len(set(metrics)) != len(metrics) or not set(metrics) <= set(DEFINITIONS):
            raise ValueError('目标指标未知或重复')
        if len({x.id for x in self.answer_items}) != len(self.answer_items):
            raise ValueError('回答事项编号重复')
        for name in self.scope.periods:
            if not name.isidentifier() or len(name) > 40:
                raise ValueError('期间名称无效')
        return self

    def check_anchor(self, anchor):
        from datetime import timedelta
        if any(p.end_exclusive.date() > anchor + timedelta(days=1) for p in self.scope.periods.values()):
            raise ValueError('查询超出数据基准')

    def plan_for(self, period, metrics, dimensions=(), filters=()):
        if period not in self.scope.periods:
            raise ValueError('引用未约定的期间')
        return PushPlan(metrics=list(metrics), dimensions=list(dimensions), time_range=self.scope.periods[period],
                        observation_hours=self.scope.observation_hours, as_of=self.scope.as_of,
                        filters=[*self.scope.filters, *filters])

    def required_metrics(self):
        metrics = self.target_metrics or self.result_requirements.metrics
        return required_metrics(self.plan_for(next(iter(self.scope.periods)), metrics))


def validate_contract(contract, source, allowed):
    """能力由服务端裁定；约定只是本次边界，调用时仍必须重查当前权限。"""
    from .push import VERSION
    if contract.capability_version != VERSION:
        raise ValueError('约定的指标目录版本已过期，请重新解析任务')
    if not set(contract.required_metrics()) <= set(allowed):
        raise ValueError('缺少目标指标及其计算依赖的权限')
    metrics = contract.target_metrics or contract.result_requirements.metrics
    for period in contract.scope.periods:
        contract.plan_for(period, metrics).check_source(source)
    permissions = contract.exploration_permissions
    if not set(permissions.metrics) <= set(allowed) & set(DEFINITIONS):
        raise ValueError('调查指标未授权')
    if not set(permissions.dimensions) <= set(DIMENSIONS) or not set(permissions.filter_fields) <= {'app_id','manufacturer','channel','failure_reason','template_id'}:
        raise ValueError('调查维度或筛选字段不存在')
    return contract


def with_capabilities(contract, allowed):
    """初次确认时生成本次可探索能力；后续权限收缩由每次调用重新核对。"""
    from .push import DEVICE_METRICS, VERSION
    device = set(contract.target_metrics) <= DEVICE_METRICS
    dimensions = ['app_id','manufacturer'] if device else list(DIMENSIONS)
    metrics = []
    for metric in DEFINITIONS:
        if (metric in DEVICE_METRICS) != device:
            continue
        if set(required_metrics(contract.plan_for(next(iter(contract.scope.periods)),[metric]))) <= set(allowed):
            metrics.append(metric)
    return contract.model_copy(update={'capability_version':VERSION,'exploration_permissions':ExplorationPermissions(
        metrics=metrics,dimensions=dimensions,filter_fields=['app_id','manufacturer'] if device else ['app_id','manufacturer','channel','failure_reason','template_id'])})


def form_contract(plan, question):
    """表单直接生成与自然语言相同的单步计划，无须额外模型调用。"""
    return TaskContract(task_type='query', question=question,
        scope=Scope(filters=plan.filters, periods={'current': plan.time_range}, observation_hours=plan.observation_hours, as_of=plan.as_of),
        result_requirements=ResultRequirements(metrics=plan.metrics, dimensions=plan.dimensions, period_names=['current']),
        execution_plan={'schema_version':1, 'steps':[{'id':'A','op':'query','period':'current','metrics':plan.metrics,'dimensions':plan.dimensions}],
                        'outputs':[{'step':'A','metrics':plan.metrics,'dimensions':plan.dimensions}]})


def load_spec(value):
    if value.get('domain')=='push':
        return TaskContract.model_validate(value)
    from .analysis import Spec
    return Spec.model_validate(value)
