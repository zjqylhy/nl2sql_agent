import hmac
import json
import os
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Literal

import httpx
import pymysql
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from fastapi.responses import JSONResponse
from . import telemetry
from . import readiness
from .model_transport import ModelTransport, ModelConfigurationError

from .intent import Clarification, Intent, RuleMiss, question_constraints, rules_intent, period_bounds
from .security import UnsafeSQL, validate_sql
from .catalog import catalog
from .plan import QueryPlan, from_intent, compile_plan, normalize_result
from .plan_parser import requires_plan, plan_constraints


class QueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)
    metric: str = ""
    period: str = ""
    group_by: str = ""
    start: str = ""
    end: str = ""


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


class Datasource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str
    port: int = 3306
    database: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    user: str
    password: str = Field(repr=False)
    analysis_date: date
    ssl_ca: str | None = None


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


def checked_window(intent: Intent, anchor: date) -> tuple[date, date]:
    start, end = period_bounds(intent, anchor)
    if (end - start).days > 3660 or end > anchor + timedelta(days=1):
        raise HTTPException(422, "时间范围超出已配置分析基准，请缩小范围")
    return start, end


def parse_intent(request: QueryRequest, anchor: date) -> tuple[Intent, dict]:
    overrides = request.input.model_dump(exclude={"question"}, exclude_defaults=True)
    constraints = question_constraints(request.input.question, overrides)
    if not request.allowed_metrics or (constraints.metric and constraints.metric not in request.allowed_metrics):
        raise HTTPException(403, "没有该指标权限")
    if constraints.period:
        # 时间约束可独立检查；占位指标仅用于复用日期模型，不参与查询或权限判断。
        checked_window(Intent(metric="gmv", period=constraints.period, start=constraints.start, end=constraints.end), anchor)
    try:
        with telemetry.stage("rules"):
            intent = rules_intent(request.input.question, overrides, constraints)
        telemetry.update(parser="rules", input_tokens=0, output_tokens=0, total_tokens=0)
        return intent, {"parser": "rules", "tokens": 0}
    except RuleMiss:
        if not os.getenv("LLM_API_KEY"):
            raise
    try:
        transport = ModelTransport.from_env()
    except ModelConfigurationError:
        telemetry.update(error_code="model_config")
        raise HTTPException(503, "模型配置不完整")
    model = transport.model
    prompt = "你是 Olist 电商单指标查询意图解析器。只输出 JSON，不生成 SQL。metric 必须是 " + ",".join(request.allowed_metrics) + "; period 为 today/yesterday/this_week/last_week/this_month/last_month/custom；group_by 只允许空字符串或 state（巴西州）；states 为巴西州代码数组；custom 的 start/end 为 YYYY-MM-DD 左闭右开区间，其余 start/end 为 null。不要猜测缺失时间，不支持归因/对比/报告/渠道/品类。无法明确解析时返回 {\"clarification\":\"简洁澄清问题\"}。"
    prompt += "成功时仅返回 metric、period、group_by、start、end、states 六个字段，禁止增加其他字段。分析基准日期为 " + str(anchor) + "。以下已确定条件必须保留：" + json.dumps(constraints.prompt_values(), ensure_ascii=False) + "。只补齐未识别字段，不得忽略任何筛选、分组或否定表达；条件无法用允许的字段表达时必须澄清。"
    if constraints.states_complete:
        prompt += "州筛选已经完整确定，states 必须与约束数组完全一致，不得新增州。"
    else:
        prompt += "请解析问题中的州名并补齐 states，同时保留约束中已有的州代码。"
    if constraints.requires_filter:
        prompt += "问题有明确的州筛选要求，states 不得为空；无法识别州名时返回澄清。"
    model_usage = {"parser": "llm", "model": model, "tokens": None}
    try:
        with telemetry.stage("model"), transport.client() as client:
            telemetry.update(parser="llm", model=model[:100], model_calls=1, input_tokens=None, output_tokens=None, total_tokens=None)
            response = transport.post(client, prompt, request.input.question, transport.intent_max_tokens)
            response.raise_for_status()
            data = response.json()
        usage = data.get("usage") or {}
        telemetry.usage(usage)
        model_usage["tokens"] = usage.get("total_tokens")
        if data["choices"][0].get("finish_reason") == "length":
            raise ValueError("truncated intent")
        content = json.loads(data["choices"][0]["message"]["content"])
        if "clarification" in content:
            if set(content) != {"clarification"} or not isinstance(content["clarification"], str) or not content["clarification"].strip():
                raise ValueError("invalid clarification")
            raise Clarification(content["clarification"][:200])
        with telemetry.stage("plan_validation"):
            intent = Intent.model_validate(content)
            constraints.validate(intent)
        return intent, model_usage
    except Clarification as exc:
        # 兜底后要求澄清同样消耗模型 Token，不能漏记这条路径的用量。
        exc.usage = model_usage
        raise
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        telemetry.model_error(exc)
        raise HTTPException(503, "模型解析失败，请使用明确的指标和时间重试") from exc


def parse_query_plan(request: QueryRequest, anchor: date) -> tuple[QueryPlan, dict]:
    overrides = request.input.model_dump(exclude={"question"}, exclude_defaults=True)
    if not requires_plan(request.input.question, overrides):
        intent, usage = parse_intent(request, anchor)
        return from_intent(intent, anchor), usage
    constraints = plan_constraints(request.input.question, overrides, anchor)
    if not request.allowed_metrics or any(m not in request.allowed_metrics for m in constraints.metrics):
        raise HTTPException(403, "没有全部所请求指标的权限")
    if constraints.time_range.end_exclusive > anchor + timedelta(days=1):
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
    prompt += "以下明确条件必须保留，仅补齐未识别项：" + json.dumps(values, ensure_ascii=False) + "。禁止猜测或遗漏条件。金额为订单商品金额，不含运费。日期左闭右开。"
    prompt += "筛选已完整确定，不得新增筛选。" if complete else "可补齐问题中的州名，不能新增未提及条件。"
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
    return pymysql.connect(host=source.host, port=source.port, user=source.user, password=source.password, database=source.database, charset="utf8mb4", connect_timeout=3, read_timeout=12, write_timeout=3, cursorclass=pymysql.cursors.DictCursor, autocommit=True, ssl_ca=source.ssl_ca, ssl_verify_cert=bool(source.ssl_ca), ssl_verify_identity=bool(source.ssl_ca))


def execute(source: Datasource, sql: str, params: list) -> list[dict]:
    with telemetry.stage("sql_validation"):
        validate_sql(sql, params, overflow_probe=sql.endswith(" LIMIT 1001"))
    with telemetry.stage("db_connect"):
        connection = connect(source)
    try:
        with connection.cursor() as cursor:
            # 数据库账号仅获授权视图 SELECT；服务同时限制执行时间与返回规模。
            # PyMySQL 的参数绑定使用百分号格式化，日期格式中的 % 必须先转义。
            executable = sql.replace("%", "%%").replace("SELECT ", "SELECT /*+ MAX_EXECUTION_TIME(10000) */ ", 1).replace("?", "%s")
            with telemetry.stage("sql"):
                cursor.execute(executable, params)
                rows = cursor.fetchmany(1001)
            if len(rows) > 1000:
                raise ResultTooLarge("结果超过 1000 组，请缩小日期范围或明确 Top N")
        counts = {"sample_size", "total_orders", "excluded_orders", "unavailable_orders"}
        return [{key: int(value or 0) if key in counts or key.endswith("_sample") else str(value) if isinstance(value, Decimal) else str(value) if isinstance(value, date) else value for key, value in row.items()} for row in rows]
    finally:
        connection.close()


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


def run_query(request: QueryRequest):
    try:
        source = sources().get(request.tenant_id + ":" + request.role)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(503, "数据源配置无效") from exc
    if source is None:
        raise HTTPException(403, "租户或角色尚未授权数据源")
    try:
        with telemetry.stage("parse"):
            plan, usage = parse_query_plan(request, source.analysis_date)
    except (Clarification, ValidationError) as exc:
        from .intent import Unsupported
        telemetry.update(error_code="unsupported" if isinstance(exc, Unsupported) else "clarification")
        message = str(exc) if isinstance(exc, Clarification) else "指标、时间或地区格式不明确，请确认后重试。"
        result = {"status": "needs_clarification", "message": message}
        if isinstance(exc, Clarification) and exc.usage is not None:
            result["usage"] = exc.usage
        return result
    with telemetry.stage("plan_validation"):
        if any(m not in request.allowed_metrics for m in plan.metrics):
            raise HTTPException(403, "没有全部所请求指标的权限")
    with telemetry.stage("compile"):
        sql, params = compile_plan(plan)
    try:
        rows = execute(source, sql, params)
    except ResultTooLarge as exc:
        telemetry.update(error_code="result_limit")
        raise HTTPException(422, str(exc)) from exc
    except UnsafeSQL as exc:
        telemetry.update(error_code="sql_safety")
        raise HTTPException(403, "查询未通过安全校验") from exc
    except pymysql.MySQLError as exc:
        telemetry.update(error_code="sql_timeout" if exc.args and exc.args[0] in (3024, 1317) else "db_error")
        raise HTTPException(503, "分析数据暂时不可用或查询超时，请联系数据管理员") from exc
    telemetry.update(rows=len(rows))
    with telemetry.stage("normalize"):
        return normalize_result(plan, rows, source.analysis_date, usage, sql, params)
