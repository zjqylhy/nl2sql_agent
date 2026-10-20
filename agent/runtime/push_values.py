"""查询发送记录中真实出现的维度组合；不引入指标或回执观察假设。"""
import json
from typing import Literal

from fastapi import HTTPException
from pydantic import Field, model_validator

from .catalog import StrictModel
from .push import DIMENSIONS, SHANGHAI, VERSION, Period, PushFilter, query_rows, whole_days


class ValuesPlan(StrictModel):
    dimensions: list[str] = Field(min_length=1,max_length=2)
    time_range: Period
    filters: list[PushFilter] = Field(default_factory=list,max_length=8)
    observation_hours: Literal[None] = None

    @model_validator(mode='after')
    def valid(self):
        if len(set(self.dimensions)) != len(self.dimensions) or not set(self.dimensions) <= set(DIMENSIONS):
            raise ValueError('维度不存在或重复')
        return self

    def check_source(self, source):
        if source.domain != 'push' or not source.data_cutoff:
            raise ValueError('需要已发布的推送数据源')
        if self.time_range.end_exclusive > source.data_cutoff or source.data_start and self.time_range.start < source.data_start:
            raise ValueError('查询时间超出已发布数据范围')


def compile_values(plan):
    daily = whole_days(plan.time_range)
    source = 'metric_push_daily_outcomes' if daily else 'metric_push_deliveries'
    date = 'd.sent_day' if daily else 'd.sent_at'
    params = ([t.astimezone(SHANGHAI).date() for t in (plan.time_range.start,plan.time_range.end_exclusive)]
              if daily else [plan.time_range.start,plan.time_range.end_exclusive])
    dims = {k:'d.'+k for k in ['app_id','manufacturer','channel','template_id']}
    dims.update(failure_reason="COALESCE(d.failure_reason,'none')",day='d.sent_day',
                month="TO_CHAR(d.sent_day,'YYYY-MM')" if daily else "TO_CHAR(d.sent_at AT TIME ZONE 'Asia/Shanghai','YYYY-MM')")
    where = [date+' >= ?',date+' < ?']
    for item in plan.filters:
        where.append(dims[item.field]+(' NOT IN (' if item.op=='not_in' else ' IN (')+','.join('?' for _ in item.values)+')')
        params.extend(item.values)
    fields = ','.join(dims[d]+' AS d_'+d for d in plan.dimensions)
    order = ','.join('d_'+d for d in plan.dimensions)
    return f'SELECT DISTINCT {fields} FROM {source} d WHERE '+ ' AND '.join(where)+f' ORDER BY {order} LIMIT 1001',params


def execute_values(source, plan, allowed, usage):
    plan.check_source(source)
    # 维度值也是数据，需要发送记录的访问权限；无指标输出不代表可绕过授权。
    if 'sent' not in allowed:
        raise HTTPException(403,'缺少发送记录的维度取值权限')
    statement,params=compile_values(plan)
    raw=query_rows(source,statement,params)
    rows=[]
    for row in raw:
        dimensions={d:str(row['d_'+d]) for d in plan.dimensions}
        rows.append({'dimensions':dimensions,'label':' / '.join(dimensions.values()),'values':{},'components':{}})
    result={'status':'completed','result_kind':'dimension_values','result_version':3,'domain':'push','catalog_version':VERSION,
        'plan':plan.model_dump(mode='json'),'metrics':[],'metrics_used':['sent'],
        'dimensions':[{'id':d,'name':DIMENSIONS[d]} for d in plan.dimensions],
        'rows':rows,'totals':{},'coverage':'complete','period':plan.time_range.model_dump(mode='json'),
        'data_cutoff':source.data_cutoff.isoformat(),'is_synthetic':source.is_synthetic,'usage':usage,
        'sql':statement,'parameters':[str(v) for v in params],
        'limitations':['仅列出该发送期间、授权范围内实际出现的维度值；未查询回执效果。']}
    if len(json.dumps(result,ensure_ascii=False).encode())>524288:
        raise HTTPException(422,'结果超过 512 KiB，请缩小范围')
    return result
