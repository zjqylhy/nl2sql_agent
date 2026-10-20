"""结构化查询直接执行；自然语言解析只产生受限计划，未知条件不得丢弃。"""
import json
import os
from pydantic import ValidationError

from fastapi import HTTPException

from . import telemetry
from .model_transport import ModelConfigurationError, ModelTransport
from .push import DEFINITIONS, DIMENSIONS, ENUMS, PushPlan, execute_push
from .push_plan_validation import parse_model_json, source_rules, validate_model_plan
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
        prompt = (
            '按完整问题生成两类推送/设备任务约定：'
            '明确取数：数值、趋势、排名、比较，允许多步依赖；'
            '开放分析：依中间结果自主调查原因。多步取数不是开放分析。'
            '不枚举业务题型。只返回一个JSON对象。'
            '明确取数：单步用 {"plan":PLAN}，多步用 {"contract":QUERY_CONTRACT}。'
            '开放分析：用 {"contract":ANALYSIS_CONTRACT}，不预先编造完整调查步骤。'
            '指标任务缺时间、范围、观察窗口或口径，或存在未支持条件，返回 {"clarification":"简洁中文问题"}；'
            '不擅自默认七天、24小时、全部应用或增加基期；保留原问题和补充中全部有效条件。'
            '禁止SQL、代码、账号、数据源和不存在的字段。中文日期用+08:00，结束时刻不含。'
            '设备与消息指标不能混查；as_of仅设备使用。'
            'PLAN={metrics:[指标],dimensions:[维度],time_range:{start:ISO时间,end_exclusive:ISO时间},observation_hours:小时,as_of:null或时点,filters:[{field,op:"in"或"not_in",values:[字符串]}]}。'
            '共同约定字段={schema_version:1,domain:"push",task_type:"query"或"analysis",question:原问题,'
            'scope:{filters:[],periods:{current:{start:ISO时间,end_exclusive:ISO时间},baseline:可选期间},observation_hours:小时,as_of:null}}。'
            'QUERY_CONTRACT另有result_requirements:{metrics:[],dimensions:[],period_names:[],coverage:"complete",selection:null或{field:维度,rank_metric:指标,direction:"asc"或"desc",limit:N,value_field:"value"或"delta"或"rate",filter:null或行条件,tie_breakers:[{field:维度,direction:"asc"}]}},'
            'execution_plan:{schema_version:1,steps:[STEP],outputs:[{step:编号,metrics:[],dimensions:[]}]}}。'
            'STEP：{id:"A",op:"query",period:"current",metrics:[],dimensions:[],filters:[],filters_from:[]}; '
            '{id:"B",op:"sort_limit",input:"A",metric:指标,direction:"asc"或"desc",limit:N,value_field:"value",tie_breakers:[{field:维度,direction:"asc"}]}; '
            '{id:"C",op:"query",period:"current",metrics:[],dimensions:[],filters_from:[{field:维度,op:"in",step:"B",column:同维度,on_empty:"return_empty"}]}; '
            '{id:"D",op:"aggregate",input:"C",dimensions:[]}; {id:"E",op:"compare",baseline:"A",current:"C"}; '
            '{id:"F",op:"filter_rows",input:"E",predicate:{metric:指标,field:"delta",comparison:"lt",value:"0"}}。'
            '所有query继承scope，一次生成有限无循环的完整计划。排序排除无样本，同值按分组标识升序。比较须等长且不重叠。'
            'ANALYSIS_CONTRACT另有target_metrics:[目标指标],answer_items:[{id:"a1",text:用户要回答的问题,source:"question"}]；不填写exploration_permissions，由程序生成。'
            '仅列举发送记录中出现的维度值（如有哪些应用/模板/厂商）使用QUERY_CONTRACT：'
            'scope.observation_hours=null,as_of=null；result_requirements={kind:"dimension_values",metrics:[],dimensions:[维度],period_names:["current"]}；'
            'steps=[{id:"A",op:"query_values",period:"current",dimensions:[维度]}],outputs=[{step:"A",metrics:[],dimensions:[维度]}]。'
            '这种查询不读取回执，不要求观察时长；仍需用户提供时间范围。其他指标查询不能使用空指标、0或null观察时长。'
            '授权指标='+json.dumps({k:{"name":v["name"],"unit":v["unit"]} for k,v in DEFINITIONS.items() if k in request.allowed_metrics},ensure_ascii=False)+
            '；维度='+json.dumps(DIMENSIONS,ensure_ascii=False)+'；枚举='+json.dumps(ENUMS,ensure_ascii=False)+'；'+source_rules(source))
        try:
            transport = ModelTransport.from_env()
            if request.initial_model is not None and transport.model != request.initial_model:
                raise HTTPException(409,'模型配置与任务冻结预算不一致，请新建任务')
            telemetry.update(parser='llm', model=transport.model, model_calls=1, total_tokens=None)
            with transport.client() as client:
                question = request.input.question
                if request.push_dialogue:
                    question = json.dumps({'original_question': question,
                        'clarifications': [turn.model_dump() for turn in request.push_dialogue]}, ensure_ascii=False)
                if len(prompt.encode())+len(question.encode())+256>6000:
                    raise HTTPException(422,'初始任务条件超过输入大小上限，请缩短问题或使用表单')
                with telemetry.stage('initial_model'):
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
            return {'status': 'needs_clarification', 'message': value['clarification'][:200], 'usage': usage}
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
