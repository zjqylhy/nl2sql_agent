"""隔离评测专用观测端点；生产 API 不加载本模块。"""
import hmac
import os

from fastapi import Header, HTTPException, Request
from agent.platform_app.api import app


def authenticate(value):
    key = os.environ["SERVICE_KEY"]
    if len(key) < 32 or not hmac.compare_digest(value, key):
        raise HTTPException(401, "evaluation authentication failed")


@app.get("/evaluation/tasks/{task_id}")
def observation(task_id: str, request: Request, x_service_key: str = Header(default="")):
    authenticate(x_service_key)
    with request.app.state.repo.db.transaction() as conn:
        rows = conn.execute("SELECT telemetry FROM task_attempts WHERE task_id=%s ORDER BY execution_version", (task_id,)).fetchall()
    if not rows:
        raise HTTPException(404, "observation unavailable")
    traces = [r["telemetry"] or {} for r in rows]
    return {"attempts": len(rows), "model_calls": sum(t.get("model_calls") or 0 for t in traces),
        "sql_calls": sum(t.get("sql_calls") or 0 for t in traces),
        "known_input_tokens": sum(t.get("input_tokens") or 0 for t in traces),
        "known_output_tokens": sum(t.get("output_tokens") or 0 for t in traces),
        "known_total_tokens": sum(t.get("total_tokens") or 0 for t in traces),
        "unknown_usage_calls": sum((t.get("model_calls") or 0) if t.get("total_tokens") is None else 0 for t in traces)}


@app.get("/evaluation/stats")
def stats(request: Request, x_service_key: str = Header(default="")):
    authenticate(x_service_key)
    with request.app.state.repo.db.transaction() as conn:
        value = conn.execute("SELECT issued,maximum FROM evaluation_budget WHERE id=1").fetchone()
    return {"issued_calls": value["issued"], "budget_exhausted": value["issued"] >= value["maximum"]}
