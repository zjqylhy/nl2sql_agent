"""请求级遥测仅保留白名单元数据，不采集问题、SQL 或供应商响应。"""
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

current = ContextVar("query_telemetry", default=None)
FIELDS = ("version", "task_id", "execution_version", "parser", "model_calls", "model", "input_tokens", "output_tokens", "total_tokens", "stages_ms", "error_code", "error_fields", "rows")
logger = logging.getLogger("olist.telemetry")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


@contextmanager
def stage(name):
    started = perf_counter()
    try:
        yield
    finally:
        value = current.get()
        if value is not None:
            value["stages_ms"][name] = round(value["stages_ms"].get(name, 0) + (perf_counter() - started) * 1000, 3)


def update(**values):
    value = current.get()
    if value is not None:
        value.update(values)


def rule_decision(recognition, mode):
    # 只记录语法标识和区间，既能定位错误直出，也不记录用户原文及筛选值。
    logger.info(json.dumps({"event": "rule_decision", "mode": mode,
        "status": "complete" if recognition.complete else "uncovered",
        "reason": "unknown_clause" if recognition.unknown else "full_grammar" if recognition.complete else "unbound_entity",
        "clauses": [{"kind": c.kind, "start": c.start, "end": c.end} for c in recognition.clauses],
        "unknown_spans": recognition.unknown}, ensure_ascii=False))


def usage(data):
    # 提供商未报告用量时保持未知，不能从输出长度推算成真实账单。
    def token(name):
        number = data.get(name)
        return number if type(number) is int and 0 <= number <= 10000000 else None
    update(input_tokens=token("prompt_tokens"), output_tokens=token("completion_tokens"), total_tokens=token("total_tokens"))


def model_error(exc):
    import httpx
    if isinstance(exc, httpx.TimeoutException):
        code = "model_timeout"
    elif isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        code = "model_rate_limit" if status == 429 else "model_auth" if status in (401, 403) else "model_http"
    elif isinstance(exc, httpx.HTTPError):
        code = "model_network"
    else:
        code = "model_format"
    update(error_code=code)
    return code


@contextmanager
def capture(execution):
    value = {"version": 1, "task_id": execution.task_id if execution else "", "execution_version": execution.execution_version if execution else 0,
             "parser": "unknown", "model_calls": 0, "model": "", "input_tokens": 0, "output_tokens": 0,
             "total_tokens": 0, "stages_ms": {}, "error_code": "", "error_fields": [], "rows": None}
    reset = current.set(value)
    try:
        yield value
    finally:
        # 日志内容由本模块构造，禁止附加异常正文、请求内容或任意字段。
        logger.info(json.dumps({"event": "query_attempt", **{key: value[key] for key in FIELDS}}, ensure_ascii=False))
        current.reset(reset)
