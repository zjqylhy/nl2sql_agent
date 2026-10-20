"""结构化查询直接执行；自然语言解析只产生受限计划，未知条件不得丢弃。"""
import json
import os
from pydantic import ValidationError

from fastapi import HTTPException

from . import telemetry
from .model_transport import ModelConfigurationError, ModelTransport
from .push_intent_context import initial_messages
from .model_budget import input_bytes
from .push_plan_validation import parse_model_json, validate_model_plan
from .task_contracts import TaskContract, form_contract


def run_push_query(request, source, *, hooks=None):
    from .query_executor import execute_program
    try:
        restored = hooks.restore() if hooks else None
        parsed = (restored, {'parser':'saved_plan','tokens':0}) if restored else parse_query(request, source)
        if isinstance(parsed, dict):
            return parsed
        contract, usage = parsed
        if contract.task_type == 'analysis':
            from .task_contracts import validate_contract, with_capabilities
            contract = with_capabilities(contract,request.allowed_metrics)
            validate_contract(contract,source,request.allowed_metrics)
            return {'status':'analysis_ready','task_type':'analysis','contract':contract.model_dump(mode='json'),'usage':usage}
        return execute_program(contract, source, request.allowed_metrics, hooks=hooks, usage=usage)
    except ValidationError as exc:
        natural = request.input.push_plan is None and request.input.query_contract is None
        if natural:
            telemetry.update(error_code='model_plan_fields')
        raise HTTPException(503 if natural else 422,'取数计划字段不完整或格式不正确，未执行后续查询；请重试或使用表单确认条件') from exc
    except ValueError as exc:
        if request.input.push_plan is None and request.input.query_contract is None:
            telemetry.update(error_code='model_plan_fields')
        raise HTTPException(422,'执行计划检查未通过：'+str(exc)[:500]) from exc


def parse_query(request, source):
    if request.context is not None:
        raise HTTPException(422, '推送查询使用独立计划；分析补充请从推送分析入口继续')
    if any(getattr(request.input, k) for k in ['metric', 'period', 'group_by', 'start', 'end']):
        raise HTTPException(422, '推送查询请提交完整推送计划或仅自然语言问题')
    usage = {'parser': 'form', 'tokens': 0}
    if request.input.query_contract is not None:
        if request.input.push_plan is not None:
            raise HTTPException(422,'只能提交一种查询格式')
        return TaskContract.model_validate(request.input.query_contract), usage
    plan = request.input.push_plan
    if plan is None:
        if not os.getenv('LLM_API_KEY'):
            return {'status': 'needs_clarification', 'message': '请用表单明确指标、起止时间、分组与观察时长；自然语言解析需配置模型。'}
        prompt, question = initial_messages(request, source)
        if input_bytes(prompt,question) > request.initial_max_input_bytes:
            telemetry.update(error_code="input_budget")
            raise HTTPException(422,"问题与补充条件超过输入上限，未调用模型；请精简重复内容或使用表单")
        try:
            transport = ModelTransport.from_env()
            if request.initial_model is not None and transport.model != request.initial_model:
                raise HTTPException(409,'模型配置与任务冻结预算不一致，请新建任务')
            with transport.client() as client:
                with telemetry.stage('initial_model'):
                    telemetry.update(parser='llm', model=transport.model, model_calls=1, total_tokens=None)
                    response = transport.post(client, prompt, question, request.initial_max_output_tokens)
                    response.raise_for_status()
                    raw = response.json()
        except HTTPException:
            raise
        except ModelConfigurationError as exc:
            telemetry.update(error_code='model_configuration')
            raise HTTPException(503, '模型尚未配置，请使用结构化表单') from exc
        except json.JSONDecodeError as exc:
            telemetry.update(error_code='model_json')
            raise HTTPException(503, '模型服务未返回有效 JSON；本任务不自动重发，请重新编辑或使用表单') from exc
        except Exception as exc:
            code = telemetry.model_error(exc)
            message = {'model_timeout':'模型服务响应超时', 'model_network':'模型服务连接失败',
                       'model_auth':'模型服务鉴权失败', 'model_rate_limit':'模型服务请求频率受限',
                       'model_quota':'模型网关额度不足或配额受限，请先检查并恢复可用额度',
                       'model_http':'模型服务返回 HTTP 错误'}.get(code, '模型服务调用失败')
            raise HTTPException(503, message+'；本任务不自动重发，请稍后新建查询或使用表单') from exc
        value = parse_model_json(raw)
        usage = {'parser': 'llm', 'tokens': (raw.get('usage') or {}).get('total_tokens')}
        if set(value) == {'clarification'} and isinstance(value['clarification'], str) and value['clarification'].strip():
            # 日期或条件可能在句尾，不能截成半句话后让用户确认另一组条件。
            if len(value['clarification']) > 600:
                telemetry.update(error_code='model_plan_fields')
                raise HTTPException(503, '模型追问过长，未接纳补充条件；请重试或使用表单')
            return {'status': 'needs_clarification', 'message': value['clarification'], 'usage': usage}
        if set(value) == {'contract'}:
            return TaskContract.model_validate({**value['contract'],'question':request.input.question}), usage
        # 部分模型会省略 contract 外层；只接纳唯一完整约定，仍经过相同字段与执行校验。
        if {'task_type', 'question', 'scope'} <= set(value) <= set(TaskContract.model_fields):
            return TaskContract.model_validate({**value,'question':request.input.question}), usage
        if set(value) != {'plan'}:
            telemetry.update(error_code='model_envelope')
            raise HTTPException(503, '模型未返回单一有效计划或追问；本任务不自动重发，请重新编辑或使用表单')
        plan = validate_model_plan(value['plan'], source, usage)
        if isinstance(plan, dict):
            return plan
    return form_contract(plan, request.input.question), usage
