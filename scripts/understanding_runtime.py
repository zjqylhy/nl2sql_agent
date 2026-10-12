"""只观测真实调用和限制预算；保持模型响应及 SQL 执行原样。"""
import hmac
import json
import os
from contextvars import ContextVar
from threading import Lock
from time import perf_counter

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from runtime import app as runtime

active = ContextVar("understanding_trace", default=None)
observations = {}
lock = Lock()
issued = 0
exhausted = False
sql_execute = runtime.execute
base_client = httpx.Client


class ObservedClient(base_client):
    def post(self, *args, **kwargs):
        global issued, exhausted
        with lock:
            if issued >= int(os.environ["EVAL_MAX_MODEL_CALLS"]):
                exhausted = True
                raise httpx.RequestError("evaluation budget exhausted")
            issued += 1
        value = active.get()
        if value is not None:
            value["model_calls"] += 1
        return super().post(*args, **kwargs)


def observed_sql(*args, **kwargs):
    value = active.get()
    if value is not None:
        value["sql_calls"] += 1
    return sql_execute(*args, **kwargs)


runtime.httpx.Client = ObservedClient
runtime.execute = observed_sql
app = FastAPI(docs_url=None, redoc_url=None)


def authenticate(key):
    if not hmac.compare_digest(key, os.environ["SERVICE_KEY"]):
        raise HTTPException(401, "evaluation authentication failed")


@app.get("/healthz")
def health():
    return runtime.health()


@app.get("/evaluation/stats")
def stats(x_service_key: str = Header(default="")):
    authenticate(x_service_key)
    with lock:
        return {"issued_calls": issued, "budget_exhausted": exhausted}


@app.get("/evaluation/tasks/{task_id}")
def observation(task_id: str, x_service_key: str = Header(default="")):
    authenticate(x_service_key)
    if task_id not in observations:
        raise HTTPException(404, "observation unavailable")
    return observations[task_id]


@app.post("/internal/query")
def query(body: dict, x_service_key: str = Header(default="")):
    authenticate(x_service_key)
    task_id = body.get("execution", {}).get("task_id", "")
    value = {"sql_calls": 0, "model_calls": 0}
    token = active.set(value)
    started = perf_counter()
    try:
        response = runtime.query(runtime.QueryRequest.model_validate(body))
        parsed = json.loads(response.body) if isinstance(response, JSONResponse) else response
        trace = parsed.get("telemetry") or {}
        value.update(parser=trace.get("parser", "unknown"), error_code=trace.get("error_code", ""),
                     stages_ms=dict(trace.get("stages_ms", {})))
        for field in ("input_tokens", "output_tokens", "total_tokens"):
            number = trace.get(field)
            value[field] = number if type(number) is int and number >= 0 else None
        return response
    finally:
        value["duration_ms"] = round((perf_counter() - started) * 1000, 3)
        value["attempts"] = 1
        value["unknown_usage_calls"] = value["model_calls"] if value.get("total_tokens") is None else 0
        with lock:
            previous = observations.get(task_id, {})
            for field in ("sql_calls", "model_calls", "attempts", "duration_ms", "unknown_usage_calls"):
                value[field] += previous.get(field, 0)
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                value["known_" + field] = (value.get(field) or 0) + previous.get("known_" + field, 0)
            for name, duration in previous.get("stages_ms", {}).items():
                value.setdefault("stages_ms", {})[name] = value["stages_ms"].get(name, 0) + duration
            observations[task_id] = value
        active.reset(token)
