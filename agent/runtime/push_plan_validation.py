"""自然语言计划的安全诊断与澄清；不修补或执行被拒绝的计划。"""
from datetime import timedelta
import json

from fastapi import HTTPException
from pydantic import ValidationError

from . import telemetry
from .push import DEVICE_METRICS, DEFINITIONS, PlanIssue, PushPlan, SHANGHAI


PLAN_RULES = ('as_of 只用于设备状态指标，推送消息指标必须省略或为 null，不能填数据截止时间。'
              '发送区间结束加 observation_hours 必须不晚于数据截止；不满足时请用户确认发送起止时间，不能自行截断。'
              '数据起止与截止是可查询边界，不代表用户已选择该范围。')


def source_rules(source):
    return (PLAN_RULES+'\n数据开始='+ (source.data_start.isoformat() if source.data_start else '未配置')+
            '\n数据截止='+source.data_cutoff.isoformat())


def clarification(issue, value, source, usage):
    telemetry.update(error_code='plan_'+issue.code, error_fields=issue.fields)
    cutoff = source.data_cutoff.astimezone(SHANGHAI)
    boundary = '数据截止 '+cutoff.isoformat(timespec='minutes')+'。'
    metrics = value.get('metrics')
    hours = value.get('observation_hours')
    push = isinstance(metrics, list) and bool(metrics) and all(isinstance(m, str) and m in DEFINITIONS and m not in DEVICE_METRICS for m in metrics)
    if push and type(hours) is int and 1 <= hours <= 8760:
        latest = (cutoff-timedelta(hours=hours)).isoformat(timespec='minutes')
        boundary += f'按 {hours} 小时观察，发送区间结束最晚为 {latest}（结束不含）。'
    messages = {
        'device_as_of': '设备状态时点仅用于设备指标；请确认本次查询的发送起止时间，或说明所需设备指标。',
        'mixed_grain': '设备与推送消息的统计粒度不同，请选择先查询哪一类指标。',
        'source_cutoff': '查询时间或设备状态时点超出数据截止，请确认可用范围内的起止时间及状态时点。',
        'source_start': '查询开始早于数据开始，请确认查询起止时间。',
        'observation_pending': '所选发送区间的回执观察期尚未完整，请确认发送起止时间或调整观察时长。',
        'activity_as_of': '活跃期间不能晚于设备状态时点，请确认活跃起止时间与设备状态时点。',
        'missing_observation': '请确认发送后回执观察时长（如 24、48 或 72 小时），系统不会自动采用默认值。',
    }
    if issue.code == 'device_as_of' and not push:
        messages['device_as_of'] = '设备状态时点必须带时区，请确认状态时点（中文日期使用 +08:00）。'
    if issue.code == 'source_start' and source.data_start:
        boundary += '数据开始 '+source.data_start.astimezone(SHANGHAI).isoformat(timespec='minutes')+'。'
    return {'status': 'needs_clarification', 'message': messages[issue.code]+boundary, 'usage': usage}


def validate_model_plan(value, source, usage):
    """可澄清的语义约束返回追问；额外字段、类型和未知指标仍拒绝。"""
    try:
        plan = PushPlan.model_validate(value)
    except ValidationError as exc:
        errors = exc.errors()
        issues = [(e.get('ctx') or {}).get('error') for e in errors]
        if issues and all(isinstance(i, PlanIssue) for i in issues):
            return clarification(issues[0], value, source, usage)
        # 字段名来自固定 schema，不把模型提供的未知键或错误正文写入日志。
        known = set(PushPlan.model_fields) | {'start', 'end_exclusive', 'field', 'op', 'values'}
        paths = ['.'.join(str(p) if isinstance(p, int) or p in known else '<extra>' for p in e['loc']) or '<plan>' for e in errors[:8]]
        telemetry.update(error_code='model_plan_fields', error_fields=paths)
        raise HTTPException(503, '模型返回的计划字段不合法，请核对指标、时间、分组及筛选条件后新建查询；本任务不自动重发') from exc
    if not plan.device and 'observation_hours' not in plan.model_fields_set:
        return clarification(PlanIssue('missing_observation', '', ['observation_hours']), value, source, usage)
    try:
        plan.check_source(source)
    except PlanIssue as exc:
        return clarification(exc, plan.model_dump(mode='json'), source, usage)
    return plan


def parse_model_json(raw):
    """仅接受完整 JSON 对象；分类固定，不透出供应商响应。"""
    try:
        telemetry.usage(raw.get('usage') or {})
        choice = raw['choices'][0]
        if choice.get('finish_reason') == 'length':
            telemetry.update(error_code='model_truncated')
            raise HTTPException(503, '模型输出被截断，未执行查询；请缩短问题或使用表单，本任务不自动重发')
        content = choice['message']['content']
        if not isinstance(content, str):
            raise TypeError('invalid_content')
        value = json.loads(content)
    except HTTPException:
        raise
    except json.JSONDecodeError as exc:
        telemetry.update(error_code='model_json')
        raise HTTPException(503, '模型未返回有效 JSON，未执行查询；请重新编辑或使用表单，本任务不自动重发') from exc
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        telemetry.update(error_code='model_envelope')
        raise HTTPException(503, '模型响应结构无效，未执行查询；请重新编辑或使用表单，本任务不自动重发') from exc
    if not isinstance(value, dict):
        telemetry.update(error_code='model_envelope')
        raise HTTPException(503, '模型响应应为 JSON 对象，未执行查询；本任务不自动重发')
    return value
