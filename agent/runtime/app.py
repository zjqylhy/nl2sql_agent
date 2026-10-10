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

from .intent import Clarification, Intent, RuleMiss, question_constraints, rules_intent, period_bounds
from .metrics import METRICS, compile_query
from .security import UnsafeSQL, validate_sql


class QueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)
    metric: str = ""
    period: str = ""
    group_by: str = ""
    start: str = ""
    end: str = ""


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,30}$")
    role: Literal["admin", "data", "ops"]
    allowed_metrics: list[str]
    input: QueryInput


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
        intent = rules_intent(request.input.question, overrides, constraints)
        return intent, {"parser": "rules", "tokens": 0}
    except RuleMiss:
        if not os.getenv("LLM_API_KEY"):
            raise
    base = os.getenv("LLM_BASE_URL", "").rstrip("/")
    model = os.getenv("LLM_MODEL", "")
    if not base.startswith("https://") or not model:
        raise HTTPException(503, "模型配置不完整")
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
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            response = client.post(base + "/chat/completions", headers={"Authorization": "Bearer " + os.environ["LLM_API_KEY"]}, json={"model": model, "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": request.input.question}], "response_format": {"type": "json_object"}, "max_tokens": 800, "temperature": 0})
            response.raise_for_status()
            data = response.json()
        usage = data.get("usage") or {}
        model_usage["tokens"] = usage.get("total_tokens")
        content = json.loads(data["choices"][0]["message"]["content"])
        if "clarification" in content:
            if set(content) != {"clarification"} or not isinstance(content["clarification"], str) or not content["clarification"].strip():
                raise ValueError("invalid clarification")
            raise Clarification(content["clarification"][:200])
        intent = Intent.model_validate(content)
        constraints.validate(intent)
        return intent, model_usage
    except Clarification as exc:
        # 兜底后要求澄清同样消耗模型 Token，不能漏记这条路径的用量。
        exc.usage = model_usage
        raise
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        raise HTTPException(503, "模型解析失败，请使用明确的指标和时间重试") from exc


def connect(source: Datasource):
    return pymysql.connect(host=source.host, port=source.port, user=source.user, password=source.password, database=source.database, charset="utf8mb4", connect_timeout=3, read_timeout=12, write_timeout=3, cursorclass=pymysql.cursors.DictCursor, autocommit=True, ssl_ca=source.ssl_ca, ssl_verify_cert=bool(source.ssl_ca), ssl_verify_identity=bool(source.ssl_ca))


def execute(source: Datasource, sql: str, params: list) -> list[dict]:
    validate_sql(sql, params)
    connection = connect(source)
    try:
        with connection.cursor() as cursor:
            # 数据库账号仅获授权视图 SELECT；服务同时限制执行时间与返回规模。
            executable = sql.replace("SELECT ", "SELECT /*+ MAX_EXECUTION_TIME(10000) */ ", 1).replace("?", "%s")
            cursor.execute(executable, params)
            rows = cursor.fetchmany(1001)
            if len(rows) > 1000:
                raise UnsafeSQL("返回结果超限")
        counts = {"sample_size", "total_orders", "excluded_orders", "unavailable_orders"}
        return [{key: int(value or 0) if key in counts else str(value) if isinstance(value, Decimal) else value for key, value in row.items()} for row in rows]
    finally:
        connection.close()


app = FastAPI(title="Olist 受限分析执行服务", docs_url=None, redoc_url=None)


@app.get("/healthz")
def health():
    try:
        configured = bool(sources()) and len(os.getenv("SERVICE_KEY", "")) >= 32
    except (ValidationError, ValueError):
        configured = False
    if not configured:
        raise HTTPException(503, "数据源配置不完整")
    return {"status": "ok"}


@app.post("/internal/query", dependencies=[Depends(authenticated)])
def query(request: QueryRequest):
    try:
        source = sources().get(request.tenant_id + ":" + request.role)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(503, "数据源配置无效") from exc
    if source is None:
        raise HTTPException(403, "租户或角色尚未授权数据源")
    try:
        intent, usage = parse_intent(request, source.analysis_date)
    except (Clarification, ValidationError) as exc:
        message = str(exc) if isinstance(exc, Clarification) else "指标、时间或地区格式不明确，请确认后重试。"
        result = {"status": "needs_clarification", "message": message}
        if isinstance(exc, Clarification) and exc.usage is not None:
            result["usage"] = exc.usage
        return result
    if intent.metric not in request.allowed_metrics:
        raise HTTPException(403, "没有该指标权限")
    start, end = checked_window(intent, source.analysis_date)
    sql = compile_query(intent.metric, intent.group_by == "state", len(intent.states))
    params = [str(start), str(end), *intent.states]
    try:
        rows = execute(source, sql, params)
    except UnsafeSQL as exc:
        raise HTTPException(403, "查询未通过安全校验") from exc
    except pymysql.MySQLError as exc:
        raise HTTPException(503, "分析数据暂时不可用或查询超时，请联系数据管理员") from exc
    metric = METRICS[intent.metric]
    return {"status": "completed", "metric": intent.metric, "metric_name": metric.name, "metric_version": "1", "definition": metric.definition, "unit": metric.unit, "analysis_date": str(source.analysis_date), "period": {"start": str(start), "end_exclusive": str(end)}, "group_by": intent.group_by, "states": intent.states, "rows": rows, "sql": sql, "parameters": params, "usage": usage, "chart_type": "bar" if intent.group_by else "number", "message": "无有效样本，未将无数据解释为零。" if not rows or all(row.get("value") is None for row in rows) else "结果由指标口径 v1 确定性计算，币种为 BRL。"}
