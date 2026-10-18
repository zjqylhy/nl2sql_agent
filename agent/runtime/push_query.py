"""结构化查询直接执行；自然语言解析只产生受限计划，未知条件不得丢弃。"""
import json
import os

from fastapi import HTTPException

from . import telemetry
from .model_transport import ModelConfigurationError, ModelTransport
from .push import DEFINITIONS, DIMENSIONS, ENUMS, PushPlan, execute_push
from .push_plan_validation import parse_model_json, source_rules, validate_model_plan


def run_push_query(request, source):
    if request.context is not None:
        raise HTTPException(422, '推送查询使用独立计划；分析补充请从推送分析入口继续')
    if any(getattr(request.input, k) for k in ['metric', 'period', 'group_by', 'start', 'end']):
        raise HTTPException(422, '推送查询请提交完整推送计划或仅自然语言问题')
    usage = {'parser': 'form', 'tokens': 0}
    plan = request.input.push_plan
    if plan is None:
        if not os.getenv('LLM_API_KEY'):
            return {'status': 'needs_clarification', 'message': '请用表单明确指标、起止时间、分组与观察时长；自然语言解析需配置模型。'}
        example_metrics = [k for k in ['sent','delivery_rate','click_rate'] if k in request.allowed_metrics]
        example = ('\n消息查询合法示例（仅展示结构，不默认这些条件）：'+json.dumps({'plan': {
            'metrics':example_metrics, 'dimensions':['day','manufacturer'],
            'time_range':{'start':'2026-07-01T00:00:00+08:00','end_exclusive':'2026-08-01T00:00:00+08:00'},
            'observation_hours':48, 'as_of':None, 'filters':[]}}, ensure_ascii=False)) if example_metrics else ''
        prompt = ('你是推送查询计划解析器。仅返回 {"plan":PLAN} 或 {"clarification":"简洁中文问题"}。'
                  '不允许 SQL、代码、数据源、租户、未支持条件和因果归因。所有明确条件必须保留；有无法表示的条件就澄清。'
                  '时间带时区，中文日期按 +08:00，结束不含。日期、指标、范围或观察时长缺失就澄清，不默认七天或24小时。'
                  '只能使用授权指标。设备与推送指标不能混查。'
                  'metrics 只列用户要求输出的指标；分子分母依赖由代码补充，不擅自增加输出指标。'
                  '如收到原问题和补充记录，结合全部已确认条件解析；补充回答用于消除对应歧义，不能丢弃原问题的指标、分组、时间和筛选条件。'
                  'PLAN schema='+json.dumps(PushPlan.model_json_schema(), ensure_ascii=False)+
                  '\n指标='+json.dumps({k: v for k, v in DEFINITIONS.items() if k in request.allowed_metrics}, ensure_ascii=False)+
                  '\n维度='+json.dumps(DIMENSIONS, ensure_ascii=False)+
                  '\n厂商与通道枚举='+json.dumps(ENUMS, ensure_ascii=False)+
                  '\n'+source_rules(source)+example)
        try:
            transport = ModelTransport.from_env()
            telemetry.update(parser='llm', model=transport.model, model_calls=1, total_tokens=None)
            with transport.client() as client:
                question = request.input.question
                if request.push_dialogue:
                    question = json.dumps({'original_question': question,
                        'clarifications': [turn.model_dump() for turn in request.push_dialogue]}, ensure_ascii=False)
                response = transport.post(client, prompt, question, transport.plan_max_tokens)
                response.raise_for_status()
                raw = response.json()
        except ModelConfigurationError as exc:
            telemetry.update(error_code='model_configuration')
            raise HTTPException(503, '模型尚未配置，请使用结构化表单') from exc
        except json.JSONDecodeError as exc:
            telemetry.update(error_code='model_json')
            raise HTTPException(503, '模型服务未返回有效 JSON；本任务不自动重发，请重新编辑或使用表单') from exc
        except Exception as exc:
            code = telemetry.model_error(exc)
            message = {'model_timeout':'模型服务响应超时', 'model_network':'模型服务连接失败',
                       'model_auth':'模型服务鉴权失败', 'model_rate_limit':'模型服务请求受限',
                       'model_http':'模型服务返回 HTTP 错误'}.get(code, '模型服务调用失败')
            raise HTTPException(503, message+'；本任务不自动重发，请稍后新建查询或使用表单') from exc
        value = parse_model_json(raw)
        usage = {'parser': 'llm', 'tokens': (raw.get('usage') or {}).get('total_tokens')}
        if set(value) == {'clarification'} and isinstance(value['clarification'], str) and value['clarification'].strip():
            return {'status': 'needs_clarification', 'message': value['clarification'][:200], 'usage': usage}
        if set(value) != {'plan'}:
            telemetry.update(error_code='model_envelope')
            raise HTTPException(503, '模型未返回单一有效计划或追问；本任务不自动重发，请重新编辑或使用表单')
        plan = validate_model_plan(value['plan'], source, usage)
        if isinstance(plan, dict):
            return plan
    return execute_push(source, plan, request.allowed_metrics, usage)
