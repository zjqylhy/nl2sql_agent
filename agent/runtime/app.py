import hmac
import asyncio
import json
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Literal

import httpx
import psycopg
from psycopg.rows import dict_row
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from fastapi.responses import JSONResponse
from . import telemetry
from . import readiness
from .model_transport import ModelTransport, ModelConfigurationError

from .intent import Clarification, RuleMiss
from .security import UnsafeSQL, validate_sql
from .catalog import catalog
from .plan import QueryPlan, compile_plan, normalize_result
from .plan_parser import plan_constraints
from .query_grammar import STATE_SCOPE_GUIDANCE
from .multiturn import QueryContext, source_metadata, resolve_followup
from .push import PushPlan


class QueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)
    metric: str = ""
    period: str = ""
    group_by: str = ""
    start: str = ""
    end: str = ""
    push_plan: PushPlan | None = None
    query_contract: dict | None = None


class Execution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(pattern=r"^[a-f0-9]{48}$")
    execution_version: int = Field(ge=1, strict=True)


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,30}$")
    role: Literal["admin", "data", "ops"]
    allowed_metrics: list[str]
    input: QueryInput
    execution: Execution | None = None
    context: QueryContext | None = None
    push_dialogue: list["PushClarification"] = Field(default_factory=list, max_length=2)
    initial_max_output_tokens: int = Field(default=2048,ge=256,le=4096,strict=True)
    initial_model: str | None = None


class PushClarification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=200)
    answer: str = Field(min_length=1, max_length=1000)


QueryRequest.model_rebuild()


class Datasource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str
    port: int = 5432
    database: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    user: str
    password: str = Field(repr=False)
    analysis_date: date
    ssl_ca: str | None = None
    context_revision: int = Field(default=1, ge=1, strict=True)
    dataset_revision: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    driver: Literal["postgresql"] = "postgresql"
    schema_name: str = Field(default="public", pattern=r"^[a-z][a-z0-9_]{1,63}$")
    domain: Literal['orders', 'push'] = 'orders'
    data_start: datetime | None = None
    data_cutoff: datetime | None = None
    is_synthetic: bool = False

    @model_validator(mode='after')
    def valid_domain(self):
        if any(t is not None and t.tzinfo is None for t in (self.data_start, self.data_cutoff)):
            raise ValueError('数据范围必须带时区')
        if self.domain == 'push' and (not self.dataset_revision or not self.data_cutoff):
            raise ValueError('推送源必须绑定不可变版本和截止时刻')
        if self.data_start and self.data_cutoff and self.data_start >= self.data_cutoff:
            raise ValueError('数据范围无效')
        return self


@lru_cache
def sources() -> dict[str, Datasource]:
    raw = json.loads(os.environ.get("TENANT_DATASOURCES", "{}"))
    return {key: Datasource.model_validate(value) for key, value in raw.items()}


def authenticated(x_service_key: str = Header(default="")):
    secret = os.environ.get("SERVICE_KEY", "")
    if len(secret) < 32:
        raise HTTPException(503, "内部服务尚未配置")
    if not hmac.compare_digest(x_service_key, secret):
        raise HTTPException(401, "内部服务鉴权失败")


def parse_query_plan(request: QueryRequest, anchor: date) -> tuple[QueryPlan, dict]:
    overrides = request.input.model_dump(exclude={"question"}, exclude_defaults=True)
    if not request.allowed_metrics:
        raise HTTPException(403, "没有已授权指标")
    constraints = plan_constraints(request.input.question, overrides, anchor)
    telemetry.rule_decision(constraints.recognition, "initial")
    if not request.allowed_metrics or any(m not in request.allowed_metrics for m in constraints.metrics):
        raise HTTPException(403, "没有全部所请求指标的权限")
    if constraints.time_range and constraints.time_range.end_exclusive > anchor + timedelta(days=1):
        raise HTTPException(422, "时间范围超出已配置分析基准，请缩小范围")
    try:
        with telemetry.stage("rules"):
            plan = constraints.rules_plan()
        telemetry.update(parser="rules", input_tokens=0, output_tokens=0, total_tokens=0)
        return plan, {"parser": "rules", "tokens": 0}
    except RuleMiss:
        if not os.getenv("LLM_API_KEY"):
            raise
    try:
        transport = ModelTransport.from_env()
    except ModelConfigurationError:
        telemetry.update(error_code="model_config")
        raise HTTPException(503, "模型配置不完整")
    model = transport.model
    c = catalog()
    values = constraints.prompt_values()
    requires_state, complete = values.pop("requires_state"), values.pop("filters_complete")
    prompt = "你是 Olist 查询计划解析器。仅输出 JSON，禁止 SQL、表达式、租户或数据源。无法完整表示问题时仅返回 {\"clarification\":\"简洁澄清问题\"}。成功响应必须符合以下 schema，禁止额外字段：" + json.dumps(QueryPlan.model_json_schema(), ensure_ascii=False)
    prompt += "仅允许指标 " + json.dumps(request.allowed_metrics) + "；维度 " + json.dumps(list(c.dimensions)) + "；筛选字段及值 " + json.dumps({k:v.model_dump(exclude={"column"}) for k,v in c.filters.items()}, ensure_ascii=False)
    prompt += "指标业务定义=" + json.dumps({k: {"name": v.name, "definition": v.definition, "unit": v.unit}
        for k, v in c.metrics.items() if k in request.allowed_metrics}, ensure_ascii=False)
    prompt += "分析基准日=" + str(anchor) + "。根据完整用户原文理解否定、修饰和条件作用范围。confirmed 为必须保留的明确条件，hints 仅供参考，不能覆盖原文含义：" + json.dumps(values, ensure_ascii=False) + "。禁止猜测或遗漏条件。金额为订单商品金额，不含运费。日期左闭右开。不支持新维度、关联、同比环比、归因和报告，不得删去不支持条件后返回部分计划。"
    prompt += STATE_SCOPE_GUIDANCE
    if complete:
        prompt += "筛选已完整确定，不得新增筛选。"
    else:
        prompt += "根据完整原文确定未识别的筛选及其包含、排除或取消含义，不能新增未提及条件。"
    if requires_state:
        prompt += "必须保留州筛选，州名无法确定则澄清。"
    usage = {"parser": "llm", "model": model, "tokens": None}
    try:
        with telemetry.stage("model"), transport.client() as client:
            telemetry.update(parser="llm", model=model[:100], model_calls=1, input_tokens=None, output_tokens=None, total_tokens=None)
            response = transport.post(client, prompt, request.input.question, transport.plan_max_tokens)
            response.raise_for_status()
            data = response.json()
        usage["tokens"] = (data.get("usage") or {}).get("total_tokens")
        telemetry.usage(data.get("usage") or {})
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("truncated plan")
        content = json.loads(choice["message"]["content"])
        if isinstance(content, dict) and set(content) == {"clarification"} and isinstance(content["clarification"], str) and content["clarification"].strip():
            raise Clarification(content["clarification"][:200])
        with telemetry.stage("plan_validation"):
            plan = QueryPlan.model_validate(content)
            constraints.validate(plan)
            plan.check_anchor(anchor)
            if any(m not in request.allowed_metrics for m in plan.metrics):
                raise HTTPException(403, "没有全部所请求指标的权限")
        return plan, usage
    except Clarification as exc:
        exc.usage = usage
        raise
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        telemetry.model_error(exc)
        raise HTTPException(503, "模型解析失败，请使用明确的指标和时间重试") from exc


class ResultTooLarge(ValueError):
    pass


def connect(source: Datasource):
    return psycopg.connect(host=source.host, port=source.port, user=source.user,
        password=source.password, dbname=source.database, connect_timeout=3,
        sslmode="verify-full" if source.ssl_ca else "prefer", sslrootcert=source.ssl_ca,
        options=f"-c search_path={source.schema_name},pg_catalog -c statement_timeout=10000 -c default_transaction_read_only=on",
        row_factory=dict_row, autocommit=True)


def execute(source: Datasource, sql: str, params: list) -> list[dict]:
    with telemetry.stage("sql_validation"):
        validate_sql(sql, params, overflow_probe=sql.endswith(" LIMIT 1001"))
    # 语句超时之外限制客户端等待，断网不能永久占用 Worker。
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if os.name == "nt" else asyncio.new_event_loop) as runner:
        rows = runner.run(asyncio.wait_for(execute_postgres(source, sql, params), timeout=15))
    if len(rows) > 1000:
        raise ResultTooLarge("结果超过 1000 组，请缩小日期范围或明确 Top N")
    return normalize_rows(rows)


async def execute_postgres(source, sql, params):
    with telemetry.stage("db_connect"):
        connection = await psycopg.AsyncConnection.connect(host=source.host, port=source.port,
            user=source.user, password=source.password, dbname=source.database, connect_timeout=3,
            sslmode="verify-full" if source.ssl_ca else "prefer", sslrootcert=source.ssl_ca,
            options=f"-c search_path={source.schema_name},pg_catalog -c statement_timeout=10000 -c default_transaction_read_only=on",
            autocommit=True, row_factory=dict_row)
    try:
        async with connection.cursor() as cursor:
            with telemetry.stage("sql"):
                await cursor.execute(sql.replace("%", "%%").replace("?", "%s"), params)
                return await cursor.fetchmany(1001)
    finally:
        await connection.close()


def normalize_rows(rows):
    counts = {"sample_size", "total_orders", "excluded_orders", "unavailable_orders"}
    return [{key: int(value or 0) if key in counts or key.endswith("_sample") else str(value) if isinstance(value, (Decimal, date)) else value for key, value in row.items()} for row in rows]


app = FastAPI(title="Olist 受限分析执行服务", docs_url=None, redoc_url=None)


@app.get("/healthz")
def health():
    try:
        configured = bool(catalog()) and bool(sources()) and len(os.getenv("SERVICE_KEY", "")) >= 32
    except (ValidationError, ValueError):
        configured = False
    if not configured:
        raise HTTPException(503, "数据源配置不完整")
    return {"status": "ok"}


@app.get("/readyz")
def ready():
    health()
    if not readiness.check(sources().values()):
        raise HTTPException(503, "分析数据库尚未就绪")
    return {"status": "ok"}


@app.post("/internal/query", dependencies=[Depends(authenticated)])
def query(request: QueryRequest):
    with telemetry.capture(request.execution) as trace:
        try:
            result = run_query(request)
            return {**result, "telemetry": trace}
        except HTTPException as exc:
            if not trace["error_code"]:
                telemetry.update(error_code={403: "permission", 422: "invalid_input", 503: "datasource_config"}.get(exc.status_code, "internal"))
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "telemetry": trace})
        except Exception:
            telemetry.update(error_code="internal")
            return JSONResponse(status_code=500, content={"detail": "分析服务内部错误", "telemetry": trace})


class ContextIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    role: Literal["admin", "data", "ops"]


@app.post("/internal/context", dependencies=[Depends(authenticated)])
def context_metadata(identity: ContextIdentity):
    source = sources().get(identity.tenant_id + ":" + identity.role)
    if source is None:
        raise HTTPException(403, "租户或角色尚未授权数据源")
    return source_metadata(source, identity.tenant_id, identity.role)


def run_query(request: QueryRequest):
    try:
        source = sources().get(request.tenant_id + ":" + request.role)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(503, "数据源配置无效") from exc
    if source is None:
        raise HTTPException(403, "租户或角色尚未授权数据源")
    if source.domain == 'push':
        from .push_query import run_push_query
        return run_push_query(request, source)
    if request.input.push_plan is not None or request.input.query_contract is not None:
        raise HTTPException(422, '当前数据源不支持推送计划')
    binding = source_metadata(source, request.tenant_id, request.role) if request.context else None
    if request.context and request.context.binding != binding["binding"]:
        raise HTTPException(409, "数据基准或授权范围已变化，请新建问数")
    turn_info = None
    try:
        with telemetry.stage("parse"):
            if request.context:
                plan, usage, turn_info = resolve_followup(request, source.analysis_date, parse_query_plan)
                if plan is None:
                    telemetry.update(error_code="clarification")
                    return {"status":"needs_clarification", "message":turn_info["message"], "usage":usage, "multiturn":turn_info, "context_binding":binding["binding"]}
            else:
                plan, usage = parse_query_plan(request, source.analysis_date)
    except (Clarification, ValidationError) as exc:
        from .intent import Unsupported
        telemetry.update(error_code="unsupported" if isinstance(exc, Unsupported) else "clarification")
        message = str(exc) if isinstance(exc, Clarification) else "指标、时间或地区格式不明确，请确认后重试。"
        result = {"status": "needs_clarification", "message": message}
        if request.context:
            result.update(context_binding=binding["binding"],multiturn={"version":1,"changes":[],"notes":[]})
        if isinstance(exc, Clarification) and exc.usage is not None:
            result["usage"] = exc.usage
        return result
    result = execute_plan(source, plan, request.allowed_metrics, usage)
    if turn_info is not None:
        result.update(multiturn=turn_info, context_binding=binding["binding"])
    return result


def execute_plan(source: Datasource, plan: QueryPlan, allowed_metrics: list[str], usage: dict):
    """共享受限查询内核；结构化工具不再经过自然语言解析。"""
    with telemetry.stage("plan_validation"):
        plan.check_anchor(source.analysis_date)
        if any(m not in allowed_metrics for m in plan.metrics):
            raise HTTPException(403, "没有全部所请求指标的权限")
    with telemetry.stage("compile"):
        sql, params = compile_plan(plan)
    try:
        telemetry.update(sql_calls=1)
        rows = execute(source, sql, params)
    except ResultTooLarge as exc:
        telemetry.update(error_code="result_limit")
        raise HTTPException(422, str(exc)) from exc
    except UnsafeSQL as exc:
        telemetry.update(error_code="sql_safety")
        raise HTTPException(403, "查询未通过安全校验") from exc
    except (psycopg.Error, TimeoutError) as exc:
        timeout = isinstance(exc, psycopg.errors.QueryCanceled)
        telemetry.update(error_code="sql_timeout" if timeout else "db_error")
        raise HTTPException(503, "分析数据暂时不可用或查询超时，请联系数据管理员") from exc
    telemetry.update(rows=len(rows))
    with telemetry.stage("normalize"):
        result = normalize_result(plan, rows, source.analysis_date, usage, sql, params)
        return result


# 在共享查询内核定义后注册，避免工具适配器和旧接口相互初始化。
from .analysis_api import router as analysis_router
app.include_router(analysis_router)
