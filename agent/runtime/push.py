"""推送与设备的受限语义计划；时间、比率分母和设备粒度由代码控制。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from fastapi import HTTPException
from pydantic import Field, model_validator

from .catalog import StrictModel

VERSION = 'push-2'
SHANGHAI = timezone(timedelta(hours=8))
COUNTS = {
    'sent': ('发送量', '次'), 'accepted': ('厂商受理量', '次'),
    'delivered': ('已确认到达量', '次'), 'clicked': ('点击量', '次'),
    'failed': ('已确认失败量', '次'), 'unknown_delivery': ('到达状态未知量', '次'),
    'devices': ('设备总量', '台'), 'valid_devices': ('有效设备', '台'),
    'invalid_devices': ('无效设备', '台'), 'unknown_devices': ('状态未知设备', '台'),
    'active_devices': ('活跃设备', '台'), 'active_valid_devices': ('活跃有效设备', '台'),
}
RATIOS = {'delivery_rate': ('已确认到达率', 'delivered', 'sent'),
          'click_rate': ('点击/发送率', 'clicked', 'sent'),
          'click_to_delivery_rate': ('点击/到达率', 'delivered_clicked', 'delivered'),
          'active_rate': ('设备活跃率', 'active_devices', 'devices')}
DEVICE_METRICS = {'devices', 'valid_devices', 'invalid_devices', 'unknown_devices', 'active_devices', 'active_valid_devices', 'active_rate'}
PUSH_METRICS = set(COUNTS) - DEVICE_METRICS | (set(RATIOS) - {'active_rate'}) | {'delivery_seconds'}
DEFINITIONS = {
    **{k: {'name': n, 'unit': u, 'definition': n+'，按逻辑投递或应用设备去重', 'version': VERSION} for k, (n, u) in COUNTS.items()},
    **{k: {'name': n, 'unit': '%', 'definition': f'{a}/{b}，同一批次与观察窗口；零分母不计算', 'version': VERSION} for k, (n, a, b) in RATIOS.items()},
    'delivery_seconds': {'name': '平均到达时延', 'unit': '秒', 'definition': '有效到达回执的到达时间减首次发送时间，缺回执不补零', 'version': VERSION},
}
DEFINITIONS['valid_devices']['definition'] = '时点上通知权限 granted，且至少一个通道 Token 有效'
DEFINITIONS['invalid_devices']['definition'] = '时点上通知权限 denied，或所有通道 Token 明确失效'
DEFINITIONS['unknown_devices']['definition'] = '已注册但无法明确判定有效或无效，不将未知计作无效'
DEFINITIONS['active_devices']['definition'] = '查询期间至少有一次应用前台事件的应用设备去重数'
DEFINITIONS['active_valid_devices']['definition'] = '活跃期间有前台事件，且在所选状态时点有效的应用设备数'
DEFINITIONS['active_rate']['definition'] = '活跃期间有前台事件的应用设备数 / 所选状态时点已注册设备数'
DEFINITIONS['delivered']['definition'] = '同一发送批次，在约定观察窗口内有到达回执的逻辑投递数'
DEFINITIONS['unknown_delivery']['definition'] = '观察窗口内既无到达/点击证据，也无明确失败的已受理投递数'
DEFINITIONS['click_to_delivery_rate']['definition'] = '窗口内已确认到达且点击的交集 / 窗口内已确认到达数；缺到达回执的点击另列'
DIMENSIONS = {'app_id': '应用', 'manufacturer': '设备厂商', 'channel': '实际推送通道', 'template_id': '模板',
              'day': '发送日期', 'month': '发送月份', 'failure_reason': '已知失败原因'}
ENUMS = {'manufacturer': ['huawei', 'xiaomi', 'oppo', 'vivo', 'apple'],
         'channel': ['huawei_push', 'xiaomi_push', 'oppo_push', 'vivo_push', 'apns', 'fcm']}


class PlanIssue(ValueError):
    """固定错误标识供自然语言入口追问；表单仍沿用严格校验。"""
    def __init__(self, code, message, fields):
        super().__init__(message)
        self.code, self.fields = code, fields


class Period(StrictModel):
    start: datetime
    end_exclusive: datetime

    @model_validator(mode='after')
    def valid(self):
        if any(t.tzinfo is None for t in (self.start, self.end_exclusive)) or self.start >= self.end_exclusive:
            raise ValueError('时间必须带时区，开始早于结束；结束时刻不含在内')
        return self


class PushFilter(StrictModel):
    field: Literal['app_id', 'manufacturer', 'channel', 'failure_reason', 'template_id']
    op: Literal['in', 'not_in'] = 'in'
    values: list[str] = Field(min_length=1, max_length=1000)

    @model_validator(mode='after')
    def valid(self):
        if len(set(self.values)) != len(self.values) or any(not v or len(v) > 100 for v in self.values):
            raise ValueError('筛选值不能为空、重复或超过 100 字符')
        if self.field in ENUMS and not set(self.values) <= set(ENUMS[self.field]):
            raise ValueError('未知厂商或通道')
        return self


class PushPlan(StrictModel):
    domain: Literal['push'] = 'push'
    version: Literal[1] = 1
    metrics: list[str] = Field(min_length=1, max_length=12)
    dimensions: list[str] = Field(default_factory=list, max_length=2)
    time_range: Period
    observation_hours: int = Field(default=24, ge=1, le=8760, strict=True)
    as_of: datetime | None = Field(default=None, description='仅用于设备状态指标的带时区时点；推送消息指标必须省略或为 null，不是数据截止时间')
    filters: list[PushFilter] = Field(default_factory=list, max_length=8)

    @model_validator(mode='after')
    def valid(self):
        if len(set(self.metrics)) != len(self.metrics) or not set(self.metrics) <= set(DEFINITIONS):
            raise ValueError('未知或重复推送指标')
        device = bool(set(self.metrics) & DEVICE_METRICS)
        if device and not set(self.metrics) <= DEVICE_METRICS:
            raise PlanIssue('mixed_grain', '设备与消息为不同粒度，请分别查询', ['metrics'])
        permitted = {'app_id', 'manufacturer'} if device else set(DIMENSIONS)
        if len(set(self.dimensions)) != len(self.dimensions) or not set(self.dimensions) <= permitted or {'day', 'month'} <= set(self.dimensions):
            raise ValueError('分组维度不符合当前数据粒度')
        if device and any(f.field not in {'app_id', 'manufacturer'} for f in self.filters):
            raise ValueError('设备指标不按多值通道或推送失败原因筛选')
        if self.as_of is not None and (self.as_of.tzinfo is None or not device):
            raise PlanIssue('device_as_of', '设备状态时点必须带时区，且只能用于设备指标', ['as_of'])
        # 同字段所有包含/排除条件必须有可能同时满足。
        for field in {f.field for f in self.filters}:
            include = [set(f.values) for f in self.filters if f.field == field and f.op == 'in']
            excluded = set().union(*(set(f.values) for f in self.filters if f.field == field and f.op == 'not_in'))
            if include and not set.intersection(*include) - excluded:
                raise ValueError('筛选条件相互矛盾')
        return self

    @property
    def device(self):
        return bool(set(self.metrics) & DEVICE_METRICS)

    @property
    def state_at(self):
        return self.as_of or self.time_range.end_exclusive - timedelta(microseconds=1)

    def check_source(self, source):
        cutoff = getattr(source, 'data_cutoff', None)
        if source.domain != 'push' or cutoff is None:
            raise ValueError('推送数据源必须配置带时区截止时刻')
        if self.time_range.end_exclusive > cutoff or (self.device and self.state_at > cutoff):
            raise PlanIssue('source_cutoff', '查询时间或设备时点超出已发布数据截止时间', ['time_range', 'as_of'])
        if source.data_start and self.time_range.start < source.data_start:
            raise PlanIssue('source_start', '查询开始早于已发布数据范围', ['time_range.start'])
        # 逐投递确认成熟，不能仅因结束时间早于截止就当作完整观察。
        if not self.device and self.time_range.end_exclusive + timedelta(hours=self.observation_hours) > cutoff:
            raise PlanIssue('observation_pending', '发送区间后的回执观察窗口尚未成熟，请缩短范围或观察时长', ['time_range.end_exclusive', 'observation_hours'])
        if self.device and self.state_at < self.time_range.end_exclusive - timedelta(microseconds=1) and set(self.metrics) & {'active_devices', 'active_valid_devices', 'active_rate'}:
            raise PlanIssue('activity_as_of', '活跃窗口不能晚于所选设备状态时点', ['time_range', 'as_of'])


def required_metrics(plan):
    result = set(plan.metrics)
    if not plan.device:
        result.add('sent')
    for metric in plan.metrics:
        if metric in RATIOS:
            _, numerator, denominator = RATIOS[metric]
            result |= {numerator, denominator} - {'delivered_clicked'}
            if numerator == 'delivered_clicked':
                result.add('clicked')
        if metric == 'delivery_seconds':
            result.add('delivered')
        if metric == 'active_valid_devices':
            result |= {'active_devices', 'valid_devices'}
    return sorted(result)


def compile_push(plan):
    params = []
    def parameter(value):
        params.append(value)
        return '?'
    dims = {'app_id': 'd.app_id', 'manufacturer': 'd.manufacturer', 'channel': 'd.channel', 'template_id': 'd.template_id',
            'failure_reason': "COALESCE(d.failure_reason,'none')",
            'day': 'd.sent_day',
            'month': "TO_CHAR(d.sent_at AT TIME ZONE 'Asia/Shanghai','YYYY-MM')"}
    if plan.device:
        at = parameter(plan.state_at)
        start, end = parameter(plan.time_range.start), parameter(plan.time_range.end_exclusive)
        prefix = f'''WITH point AS (SELECT CAST({at} AS timestamptz) at),
          active AS (SELECT app_id,device_id FROM metric_device_activity
            WHERE occurred_at >= {start} AND occurred_at < {end} GROUP BY app_id,device_id)
        '''
        source = '''metric_devices d JOIN metric_device_states s ON d.app_id=s.app_id AND d.device_id=s.device_id
          JOIN point p ON s.valid_from<=p.at AND (s.valid_to IS NULL OR s.valid_to>p.at)
          LEFT JOIN active a ON d.app_id=a.app_id AND d.device_id=a.device_id'''
        where = ['d.registered_at <= p.at']
        aggregates = {'devices': 'COUNT(*)', 'valid_devices': "SUM(CASE WHEN s.push_status='valid' THEN 1 ELSE 0 END)",
                      'invalid_devices': "SUM(CASE WHEN s.push_status='invalid' THEN 1 ELSE 0 END)",
                      'unknown_devices': "SUM(CASE WHEN s.push_status='unknown' THEN 1 ELSE 0 END)",
                      'active_devices': 'SUM(CASE WHEN a.device_id IS NOT NULL THEN 1 ELSE 0 END)',
                      'active_valid_devices': "SUM(CASE WHEN a.device_id IS NOT NULL AND s.push_status='valid' THEN 1 ELSE 0 END)"}
    else:
        hours = parameter(plan.observation_hours)
        prefix = f'''WITH windowed AS (SELECT *,sent_at + {hours} * interval '1 hour' horizon FROM metric_push_deliveries) '''
        source = 'windowed d'
        start, end = parameter(plan.time_range.start), parameter(plan.time_range.end_exclusive)
        where = [f'd.sent_at >= {start}', f'd.sent_at < {end}']
        arrived = 'd.delivered_at <= d.horizon'
        clicked = 'd.clicked_at <= d.horizon'
        aggregates = {'sent': 'COUNT(*)', 'accepted': 'SUM(CASE WHEN d.accepted_at IS NOT NULL THEN 1 ELSE 0 END)',
                      'delivered': f'SUM(CASE WHEN {arrived} THEN 1 ELSE 0 END)',
                      'clicked': f'SUM(CASE WHEN {clicked} THEN 1 ELSE 0 END)',
                      'delivered_clicked': f'SUM(CASE WHEN {arrived} AND {clicked} THEN 1 ELSE 0 END)',
                      'click_missing_delivery': f'SUM(CASE WHEN {clicked} AND (d.delivered_at IS NULL OR d.delivered_at>d.horizon) THEN 1 ELSE 0 END)',
                      'failed': "SUM(CASE WHEN d.accepted_at IS NULL OR d.expired_at<=d.horizon THEN 1 ELSE 0 END)",
                      'unknown_delivery': f'SUM(CASE WHEN d.accepted_at IS NOT NULL AND (d.expired_at IS NULL OR d.expired_at>d.horizon) AND (d.delivered_at IS NULL OR d.delivered_at>d.horizon) AND (d.clicked_at IS NULL OR d.clicked_at>d.horizon) THEN 1 ELSE 0 END)',
                      'delivery_duration_sum': f'SUM(CASE WHEN {arrived} THEN EXTRACT(EPOCH FROM (d.delivered_at-d.sent_at)) ELSE 0 END)'}
    for f in plan.filters:
        placeholders = ','.join(parameter(v) for v in f.values)
        where.append(f"{dims[f.field]} {'NOT IN' if f.op == 'not_in' else 'IN'} ({placeholders})")
    fields = [f'{dims[d]} AS d_{d}' for d in plan.dimensions]
    # 只输出授权指标需要的分子/分母，防止查询一个比例时泄漏其他指标。
    needed = set(plan.metrics)
    for metric in plan.metrics:
        if metric in RATIOS:
            needed |= set(RATIOS[metric][1:])
        if metric == 'delivery_seconds':
            needed |= {'delivered', 'delivery_duration_sum'}
    if not plan.device:
        needed.add('sent')
        if 'clicked' in needed:
            needed.add('click_missing_delivery')
    fields += [f'{aggregates[k]} AS c_{k}' for k in sorted(needed) if k in aggregates]
    statement = prefix + 'SELECT ' + ','.join(fields) + ' FROM ' + source + ' WHERE ' + ' AND '.join(where)
    if plan.dimensions:
        statement += ' GROUP BY ' + ','.join(dims[d] for d in plan.dimensions)
        statement += ' ORDER BY ' + ','.join('d_'+d for d in plan.dimensions)
    return statement + ' LIMIT 1001', params


def values_for(components, metrics):
    out = {}
    for metric in metrics:
        if metric in RATIOS:
            _, a, b = RATIOS[metric]
            den = Decimal(str(components.get(b) or 0))
            value = Decimal(str(components.get(a) or 0)) / den * 100 if den else None
            sample = int(den)
        elif metric == 'delivery_seconds':
            sample = int(components.get('delivered') or 0)
            value = Decimal(str(components.get('delivery_duration_sum') or 0)) / sample if sample else None
        else:
            value = components.get(metric)
            # 空总体的计数为零，但比例和均值仍保留无样本。
            value = int(value or 0)
            sample = int(components.get('devices' if metric in DEVICE_METRICS else 'sent') or value)
        out[metric] = {'value': str(value) if value is not None else None, 'sample_size': sample}
    return out


def result_for(plan, raw, source, usage, sql, params):
    rows = []
    for r in raw:
        components = {k[2:]: str(v or 0) for k, v in r.items() if k.startswith('c_')}
        dimensions = {d: str(r['d_'+d]) for d in plan.dimensions}
        rows.append({'dimensions': dimensions, 'label': ' / '.join(dimensions.values()) or '整体',
                     'values': values_for(components, plan.metrics), 'components': components})
    total = {k: sum(Decimal(r['components'].get(k, '0')) for r in rows) for k in {k for r in rows for k in r['components']}}
    return {'status': 'completed', 'result_version': 3, 'domain': 'push', 'catalog_version': VERSION,
            'plan': plan.model_dump(mode='json'), 'metrics': [{'id': m, **DEFINITIONS[m]} for m in plan.metrics],
            'metrics_used': required_metrics(plan), 'dimensions': [{'id': d, 'name': DIMENSIONS[d]} for d in plan.dimensions],
            'rows': rows, 'totals': values_for(total, plan.metrics), 'coverage': 'complete',
            'data_cutoff': source.data_cutoff.isoformat(), 'is_synthetic': source.is_synthetic,
            'period': plan.time_range.model_dump(mode='json'), 'usage': usage, 'sql': sql,
            'parameters': [str(v) for v in params],
            'message': '模拟数据。' if source.is_synthetic else '',
            'limitations': ([] if plan.device else ['已确认到达率受回执覆盖影响；缺回执不等于失败。']) + ['比率按总分子/总分母计算，零分母不计算。',
                            '设备有效性是指定时点存量；应用活跃由前台事件统计。' if plan.device else '发送窗口与回执观察时长分别限定；不作因果归因。']}


def execute_push(source, plan, allowed, usage):
    from . import app as runtime, telemetry
    try:
        plan.check_source(source)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not set(required_metrics(plan)) <= set(allowed):
        raise HTTPException(403, '缺少指标或其分子分母的完整权限')
    sql, params = compile_push(plan)
    try:
        import asyncio
        import os
        import psycopg
        from .push_sql import validate_push_sql
        validate_push_sql(sql, params)
        telemetry.sql_call()
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if os.name == 'nt' else asyncio.new_event_loop) as runner:
            raw = runner.run(asyncio.wait_for(runtime.execute_postgres(source, sql, params), timeout=15))
        if len(raw) > 1000:
            raise HTTPException(422, '结果超过 1000 组，请缩小查询范围')
    except (psycopg.Error, TimeoutError) as exc:
        raise HTTPException(503, '推送数据暂时不可用或查询超时') from exc
    result = result_for(plan, raw, source, usage, sql, params)
    # 行数之外限制实际载荷，避免宽行结果拖垮保存与后续分析。
    import json
    if len(json.dumps(result, ensure_ascii=False).encode()) > 524288:
        raise HTTPException(422, '结果超过 512 KiB，请缩小范围或分组后重查')
    return result


def metric_catalog():
    return DEFINITIONS


def metadata_payload():
    return {'version': VERSION, 'metrics': DEFINITIONS, 'dimensions': DIMENSIONS, 'enums': ENUMS}
