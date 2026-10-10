"""评测探针包裹真实入口，仅观测调用次数；不替换解析、SQL 或模型输出。"""
import hmac
import json
import os
from contextvars import ContextVar
from time import perf_counter
from threading import Lock

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from runtime import app as runtime

active = ContextVar("evaluation_observation", default=None)
observations = {}
execute = runtime.execute
client = httpx.Client
budget_lock = Lock()
issued_calls = 0


def observed_execute(*args, **kwargs):
    value = active.get()
    if value is not None:
        value["sql_calls"] += 1
    return execute(*args, **kwargs)


class ObservedClient(client):
    def post(self, *args, **kwargs):
        global issued_calls
        with budget_lock:
            if issued_calls >= int(os.environ.get("EVAL_MAX_MODEL_CALLS", "75")):
                raise httpx.RequestError("evaluation model budget exhausted")
            issued_calls += 1
        value = active.get()
        if value is not None:
            value["model_calls"] += 1
        return super().post(*args, **kwargs)


runtime.execute = observed_execute
runtime.httpx.Client = ObservedClient
app = FastAPI(docs_url=None, redoc_url=None)


def authenticate(key):
    if not hmac.compare_digest(key, os.environ["SERVICE_KEY"]):
        raise HTTPException(401, "evaluation authentication failed")


@app.get("/healthz")
def health():
    return runtime.health()


@app.get("/evaluation/{task_id}")
def observation(task_id: str, x_service_key: str = Header(default="")):
    authenticate(x_service_key)
    if task_id not in observations:
        raise HTTPException(404, "observation unavailable")
    return observations[task_id]


@app.post("/internal/query")
def query(body: dict, x_service_key: str = Header(default="")):
    authenticate(x_service_key)
    execution = body.get("execution", {})
    task_id = execution.get("task_id", "")
    value = {"sql_calls": 0, "model_calls": 0, "usage": None}
    token = active.set(value)
    started = perf_counter()
    try:
        # 旧基线协议不含执行上下文，只移除新平台的关联元数据。
        if "execution" not in runtime.QueryRequest.model_fields:
            body = {k: v for k, v in body.items() if k != "execution"}
        result = runtime.query(runtime.QueryRequest.model_validate(body))
        parsed = json.loads(result.body) if isinstance(result, JSONResponse) else result
        value["usage"] = parsed.get("usage") or parsed.get("telemetry")
        value["error_code"] = (parsed.get("telemetry") or {}).get("error_code", "")
        value["stages_ms"] = (parsed.get("telemetry") or {}).get("stages_ms")
        return result
    except HTTPException:
        raise
    except ValidationError:
        raise HTTPException(422, "evaluation request invalid")
    finally:
        value["duration_ms"] = round((perf_counter() - started) * 1000, 3)
        usage = value["usage"] or {}
        tokens = usage.get("tokens", usage.get("total_tokens"))
        value["known_tokens"] = tokens if type(tokens) is int and tokens >= 0 else 0
        value["unknown_usage_calls"] = value["model_calls"] if tokens is None else 0
        value["parser"] = usage.get("parser", "unknown")
        if task_id:
            previous = observations.get(task_id, {})
            if value.get("stages_ms") is not None:
                for name, elapsed in (previous.get("stages_ms") or {}).items():
                    value["stages_ms"][name] = value["stages_ms"].get(name, 0) + elapsed
            for field in ("sql_calls", "model_calls", "duration_ms", "known_tokens", "unknown_usage_calls"):
                value[field] += previous.get(field, 0)
            observations[task_id] = value
        active.reset(token)
