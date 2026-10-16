"""HTTP 只负责接纳与读取持久任务；不在请求生命周期里执行 Agent。"""
import asyncio
import hashlib
import ipaddress
import json
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import bcrypt
import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import Field, model_validator
from psycopg.types.json import Jsonb
from starlette.concurrency import run_in_threadpool

from agent.runtime import app as runtime
from agent.runtime.analysis import AnalysisError, Spec, canonical, metric_ids
from agent.runtime.analysis_api import context as analysis_context
from agent.runtime.catalog import StrictModel, catalog
from .database import Database
from .policy import Policy
from .repository import ACTIVE, Repository, binding, identity, permitted, public_conversation, public_task

UUID = r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$"
DUMMY_HASH = bcrypt.hashpw(b"invalid-login-placeholder", bcrypt.gensalt(rounds=12))


class Login(StrictModel):
    tenant: str = Field(min_length=1, max_length=31)
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class Input(runtime.QueryInput):
    @model_validator(mode="after")
    def valid_input(self):
        if not self.question.strip() or (self.metric and self.metric not in catalog().metrics) or (self.group_by and self.group_by not in catalog().dimensions):
            raise ValueError("问题、指标或维度无效")
        return self


class AnalysisInput(StrictModel):
    question: str = Field(min_length=1, max_length=2000)
    request_id: str = Field(pattern=UUID)
    spec: Spec | None = None


class Version(StrictModel):
    expected_version: int = Field(ge=0, strict=True)


class Answer(Version):
    request_id: str = Field(pattern=UUID)
    answer: str = Field(min_length=1, max_length=1000)


class Turn(Input, Version):
    request_id: str = Field(pattern=UUID)


class UserInput(StrictModel):
    username: str = Field(min_length=2, max_length=100)
    password: str = Field(min_length=12, max_length=72)
    role: Literal["admin", "data", "ops"]
    allowed_metrics: list[str]

    @model_validator(mode="after")
    def valid_user(self):
        if len(self.password.encode()) > 72 or not set(self.allowed_metrics) <= set(catalog().metrics):
            raise ValueError("密码字节数或指标权限无效")
        return self


class Permissions(StrictModel):
    enabled: bool = Field(strict=True)
    allowed_metrics: list[str]


def same_origin(value, expected):
    try:
        a, b = urlsplit(value), urlsplit(expected)
        return bool(a.netloc) and not a.username and not a.password and not a.path and not a.query and not a.fragment and a.scheme == b.scheme and a.netloc == b.netloc
    except ValueError:
        return False


def client_address(request, trusted_proxies):
    peer = request.client.host if request.client else "unknown"
    try:
        address = ipaddress.ip_address(peer)
        trusted = lambda ip: any(ip in network for network in trusted_proxies)
        if not trusted(address):
            return peer
        chain = request.headers.get("x-forwarded-for", "")
        if not chain or len(chain) > 1024:
            return peer
        # 从连接的直接对端向左查找，攻击者伪造的前缀不能覆盖真实来源。
        for item in reversed(chain.split(",")):
            address = ipaddress.ip_address(item.strip())
            if not trusted(address):
                return str(address)
        return str(address)
    except ValueError:
        return peer


def create_app(db=None, policy=None, *, origin=None, secure_cookie=None, trusted_proxies=None):
    import os
    origin = origin or os.getenv("PUBLIC_ORIGIN", "http://localhost:8088")
    secure_cookie = secure_cookie if secure_cookie is not None else os.getenv("COOKIE_SECURE") == "true"
    trusted_proxies = [ipaddress.ip_network(value.strip(), strict=False) for value in
        (trusted_proxies if trusted_proxies is not None else os.getenv("TRUSTED_PROXY_CIDRS", "").split(",")) if value.strip()]

    @asynccontextmanager
    async def lifespan(app):
        owned = db is None
        app.state.repo = Repository(db or Database())
        app.state.policy = policy if db is not None else Policy.from_env()
        try:
            yield
        finally:
            if owned:
                app.state.repo.db.close()

    app = FastAPI(title="Olist Agent 平台", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def boundary(request, call_next):
        if request.method not in {"GET", "HEAD"} and not same_origin(request.headers.get("origin", ""), origin):
            return JSONResponse({"error": "请求来源不受信任"}, status_code=403)
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > 16384):
            return JSONResponse({"error": "请求体过大"}, status_code=413)
        # Content-Length 不存在或伪造也受流式读取上限约束。
        size = 0
        receive = request._receive
        async def limited_receive():
            nonlocal size
            value = await receive()
            size += len(value.get("body", b""))
            if size > 16384:
                raise HTTPException(413, "请求体过大")
            return value
        request._receive = limited_receive
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"error": "请求格式或参数不正确"}, status_code=400)

    @app.exception_handler(AnalysisError)
    async def analysis_error(request, exc):
        return JSONResponse({"error": exc.message}, status_code=400)

    @app.exception_handler(psycopg.Error)
    async def database_error(request, exc):
        return JSONResponse({"error": "数据库暂时不可用"}, status_code=503)

    def repo(request: Request):
        return request.app.state.repo

    def current_user(request: Request, repository=Depends(repo)):
        token = request.cookies.get("session", "")
        with repository.db.transaction() as conn:
            user = conn.execute(f"SELECT u.id,u.tenant_id,u.username,u.role,u.allowed_metrics FROM sessions s JOIN users u ON u.id=s.user_id AND u.tenant_id=s.tenant_id WHERE s.token_hash=%s AND s.expires_at>now() AND u.enabled", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not user:
            raise HTTPException(401, "请先登录或会话已失效")
        return user

    def admin(user):
        if user["role"] != "admin":
            raise HTTPException(403, "需要管理员权限")

    @app.get("/livez")
    def live():
        return {"status": "ok"}

    @app.get("/healthz")
    def health(repository=Depends(repo)):
        with repository.db.transaction() as conn:
            conn.execute("SELECT 1 FROM platform.alembic_version")
        return {"status": "ok", "runtime": "python-langgraph"}

    @app.post("/api/login")
    def login(body: Login, request: Request, repository=Depends(repo)):
        # 只信任显式代理链；多 API 实例共享数据库限流窗口。
        ip_hash = hashlib.sha256(client_address(request, trusted_proxies).encode()).hexdigest()
        with repository.db.transaction() as conn:
            count = conn.execute("INSERT INTO login_windows(identity,started_at,attempts) VALUES(%s,now(),1) ON CONFLICT(identity) DO UPDATE SET started_at=CASE WHEN login_windows.started_at<now()-interval '1 minute' THEN now() ELSE login_windows.started_at END,attempts=CASE WHEN login_windows.started_at<now()-interval '1 minute' THEN 1 ELSE login_windows.attempts+1 END RETURNING attempts", (ip_hash,)).fetchone()["attempts"]
        if count > 10:
            raise HTTPException(429, "登录尝试过多，请稍后重试")
        with repository.db.transaction() as conn:
            user = conn.execute("SELECT * FROM users WHERE tenant_id=%s AND username=%s AND enabled", (body.tenant, body.username)).fetchone()
            password = body.password.encode()
            valid = len(password) <= 72 and bcrypt.checkpw(password, bytes(user["password_hash"]) if user else DUMMY_HASH)
            if not user or not valid:
                raise HTTPException(401, "租户、账号或密码不正确")
            token = secrets.token_hex(24)
            conn.execute("INSERT INTO sessions VALUES(%s,%s,%s,now()+interval '12 hours')", (hashlib.sha256(token.encode()).hexdigest(), user["tenant_id"], user["id"]))
            value = {k: user[k] for k in ("id", "tenant_id", "username", "role", "allowed_metrics")}
        response = JSONResponse(value)
        response.set_cookie("session", token, max_age=43200, httponly=True, secure=secure_cookie, samesite="strict")
        return response

    @app.post("/api/logout")
    def logout(request: Request, user=Depends(current_user), repository=Depends(repo)):
        with repository.db.transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=%s", (hashlib.sha256(request.cookies["session"].encode()).hexdigest(),))
        response = JSONResponse({"ok": True})
        response.delete_cookie("session", httponly=True, secure=secure_cookie, samesite="strict")
        return response

    @app.get("/api/me")
    def me(user=Depends(current_user)):
        return user

    @app.get("/api/metrics")
    def metrics(user=Depends(current_user)):
        return [{"id": k, "name": v.name, "version": v.version} for k, v in catalog().metrics.items() if k in user["allowed_metrics"]]

    @app.get("/api/tasks")
    def tasks(user=Depends(current_user), repository=Depends(repo)):
        values = []
        with repository.db.transaction() as conn:
            for row in conn.execute("SELECT id FROM tasks WHERE tenant_id=%s ORDER BY created_at DESC LIMIT 100", (user["tenant_id"],)).fetchall():
                try:
                    values.append(public_task(repository.task(conn, user, row["id"])))
                except HTTPException:
                    continue
        return values

    @app.post("/api/tasks", status_code=202)
    def create_task(body: Input, user=Depends(current_user), repository=Depends(repo)):
        if body.metric and body.metric not in user["allowed_metrics"]:
            raise HTTPException(403, "没有该指标权限")
        with repository.db.transaction() as conn:
            repository.capacity(conn, user)
            run_id = repository.insert_task(conn, user, body.model_dump())
            return public_task(repository.task(conn, user, run_id))

    @app.get("/api/tasks/{run_id}")
    def task(run_id: str, user=Depends(current_user), repository=Depends(repo)):
        with repository.db.transaction() as conn:
            return public_task(repository.task(conn, user, run_id))

    @app.post("/api/tasks/{run_id}/stop")
    def stop(run_id: str, user=Depends(current_user), repository=Depends(repo)):
        with repository.db.transaction() as conn:
            value = repository.task(conn, user, run_id, lock=True)
            if value["status"] in ACTIVE:
                conn.execute("UPDATE tasks SET status='canceled',error='任务已停止',execution_version=execution_version+1,lease_until=NULL WHERE id=%s", (run_id,))
                conn.execute("UPDATE task_outbox SET delivered_at=now() WHERE task_id=%s", (run_id,))
                conn.execute("UPDATE task_attempts SET state='canceled',ended_at=now() WHERE task_id=%s AND state='running'", (run_id,))
                conn.execute("UPDATE analysis_steps SET status='canceled',ended_at=now() WHERE run_id=%s AND status='running'", (run_id,))
                conn.execute("UPDATE analysis_budget_ledger SET status='unknown' WHERE run_id=%s AND status='reserved'", (run_id,))
                if value["conversation_id"]:
                    conn.execute("UPDATE conversations SET active_task_id=NULL,version=version+1,updated_at=now() WHERE id=%s AND active_task_id=%s", (value["conversation_id"], run_id))
                if value["kind"] == "analysis":
                    conn.execute("UPDATE analysis_runs SET state=jsonb_set(jsonb_set(state,'{phase}','\"canceled\"'),'{message}','\"任务已停止\"'),version=version+1 WHERE id=%s", (run_id,))
                repository.event(conn, run_id, "canceled", {"message": "任务已停止"})
        return {"ok": True}

    @app.get("/api/tasks/{run_id}/events")
    async def events(run_id: str, request: Request, user=Depends(current_user), repository=Depends(repo)):
        try:
            after = int(request.headers.get("last-event-id", "0"))
            if after < 0:
                raise ValueError()
        except ValueError:
            raise HTTPException(400, "事件序号不正确")
        def fetch():
            latest = current_user(request, repository)
            with repository.db.transaction() as conn:
                value = repository.task(conn, latest, run_id)
                rows = conn.execute("SELECT seq,kind,data FROM task_events WHERE task_id=%s AND seq>%s ORDER BY seq LIMIT 100", (run_id, after)).fetchall()
                return value, rows
        await run_in_threadpool(fetch)  # 首次鉴权失败返回 HTTP 状态，不能先发送 200。
        async def stream():
            nonlocal after
            heartbeat_at = time.monotonic()
            while not await request.is_disconnected():
                try:
                    value, rows = await run_in_threadpool(fetch)
                except (HTTPException, psycopg.Error):
                    return
                for row in rows:
                    yield f"id: {row['seq']}\nevent: {row['kind']}\ndata: {json.dumps(row['data'],ensure_ascii=False)}\n\n"
                    after = row["seq"]
                if value["status"] not in {"pending", "querying"} and len(rows) < 100:
                    return
                if time.monotonic() - heartbeat_at >= 10:
                    yield ": heartbeat\n\n"
                    heartbeat_at = time.monotonic()
                await asyncio.sleep(0.5)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.get("/api/analysis-capabilities")
    def capabilities(request: Request, user=Depends(current_user)):
        enabled = False
        if request.app.state.policy:
            try:
                enabled = analysis_context(identity(user))["model_ready"] and binding(user, True)["model"] == request.app.state.policy.model
            except HTTPException:
                pass
        return {"enabled": enabled, "runtime": "python-langgraph", "model_mode": os.getenv("ANALYSIS_MODEL_MODE", "provider"), "message": "需冻结数据版本、模型和预算价格配置"}

    @app.post("/api/analysis-runs", status_code=202)
    def create_analysis(body: AnalysisInput, request: Request, user=Depends(current_user), repository=Depends(repo)):
        policy = request.app.state.policy
        if not policy or not capabilities(request, user)["enabled"]:
            raise HTTPException(503, "经营分析尚未启用或模型配置不完整")
        if not body.question.strip():
            raise HTTPException(400, "请输入有效问题")
        if body.spec:
            body.spec.check_anchor(date.fromisoformat(binding(user, True)["analysis_date"]))
            if not set(metric_ids(body.spec)) <= set(user["allowed_metrics"]):
                raise HTTPException(403, "没有任务所需全部指标权限")
        with repository.db.transaction() as conn:
            return repository.create_analysis(conn, user, body.request_id, body.question, body.spec.model_dump(mode="json") if body.spec else None, policy)

    @app.get("/api/analysis-runs")
    def analyses(user=Depends(current_user), repository=Depends(repo)):
        values = []
        with repository.db.transaction() as conn:
            for row in conn.execute("SELECT r.id FROM analysis_runs r WHERE tenant_id=%s AND user_id=%s ORDER BY updated_at DESC LIMIT 100", (user["tenant_id"], user["id"])).fetchall():
                try:
                    run = repository.analysis(conn, user, row["id"])
                    values.append({k: run[k] for k in ("id", "question", "status", "version")})
                except HTTPException:
                    continue
        return values

    @app.get("/api/analysis-runs/{run_id}")
    def analysis(run_id: str, user=Depends(current_user), repository=Depends(repo)):
        with repository.db.transaction() as conn:
            return repository.analysis(conn, user, run_id)

    @app.post("/api/analysis-runs/{run_id}/answers")
    def answer(run_id: str, body: Answer, user=Depends(current_user), repository=Depends(repo)):
        if not body.answer.strip():
            raise HTTPException(400, "请输入补充内容")
        digest = hashlib.sha256(body.answer.encode()).hexdigest()
        with repository.db.transaction() as conn:
            value = repository.task(conn, user, run_id, lock=True)
            run = repository.analysis(conn, user, run_id)
            old = conn.execute("SELECT body_hash FROM analysis_answers WHERE run_id=%s AND request_id=%s", (run_id, body.request_id)).fetchone()
            if old:
                if old["body_hash"] != digest:
                    raise HTTPException(409, "重复请求的补充内容不同")
                return run
            if value["status"] != "needs_clarification" or run["version"] != body.expected_version or len(run["state"]["answers"]) >= 2 or run["state"].get("feedback", {}).get("evidence_expired"):
                raise HTTPException(409, "任务已更新、补充已过期或轮数耗尽")
            if (time.time()-value["updated_at"].timestamp()) > 86400:
                raise HTTPException(409, "补充等待已超过 24 小时")
            repository.capacity(conn, user)
            conn.execute("UPDATE analysis_runs SET resume_input=%s,version=version+1,updated_at=now() WHERE id=%s", (Jsonb({"answer": body.answer, "wait_id": run["state"]["wait_id"]}), run_id))
            conn.execute("INSERT INTO analysis_answers VALUES(%s,%s,%s,%s)", (run_id, body.request_id, digest, run["version"]+1))
            conn.execute("UPDATE tasks SET status='pending',ready_at=now(),updated_at=now() WHERE id=%s", (run_id,))
            repository.event(conn, run_id, "accepted", {"status": "pending"})
            return repository.analysis(conn, user, run_id)

    @app.post("/api/analysis-runs/{run_id}/resume")
    def resume(run_id: str, user=Depends(current_user), repository=Depends(repo)):
        with repository.db.transaction() as conn:
            value = repository.task(conn, user, run_id)
            if value["status"] not in {"pending", "querying"}:
                raise HTTPException(409, "当前任务不能恢复；未知模型调用不能重发")
            return repository.analysis(conn, user, run_id)  # 租约恢复由 Worker 负责，API 不改图。

    @app.post("/api/conversations", status_code=201)
    def new_conversation(body: StrictModel, user=Depends(current_user), repository=Depends(repo)):
        meta, conversation_id = binding(user), secrets.token_hex(24)
        with repository.db.transaction() as conn:
            conn.execute("INSERT INTO conversations(id,tenant_id,user_id,role,binding,analysis_date,catalog_hash) VALUES(%s,%s,%s,%s,%s,%s,%s)", (conversation_id, user["tenant_id"], user["id"], user["role"], meta["binding"], meta["analysis_date"], meta["catalog_hash"]))
            return public_conversation(repository.conversation(conn, user, conversation_id))

    @app.get("/api/conversations")
    def conversations(page: int = Query(default=0, ge=0, le=100000), user=Depends(current_user), repository=Depends(repo)):
        visible = []
        with repository.db.transaction() as conn:
            values = conn.execute("SELECT id FROM conversations WHERE tenant_id=%s AND user_id=%s ORDER BY updated_at DESC,id LIMIT 20 OFFSET %s", (user["tenant_id"], user["id"], page*20)).fetchall()
            for row in values:
                try:
                    c = public_conversation(repository.conversation(conn, user, row["id"]))
                    first = conn.execute("SELECT input->>'question' question FROM tasks WHERE conversation_id=%s ORDER BY conversation_turn LIMIT 1", (row["id"],)).fetchone()
                    c["title"] = first["question"] if first else "新的问数"
                    visible.append(c)
                except HTTPException:
                    continue
        return {"conversations": visible, "page": page, "has_more": len(values) == 20}

    @app.get("/api/conversations/{conversation_id}")
    def conversation(conversation_id: str, user=Depends(current_user), repository=Depends(repo)):
        with repository.db.transaction() as conn:
            c = repository.conversation(conn, user, conversation_id)
            values = []
            for row in conn.execute("SELECT id,conversation_turn FROM tasks WHERE conversation_id=%s ORDER BY conversation_turn", (conversation_id,)).fetchall():
                try:
                    values.append(public_task(repository.task(conn, user, row["id"])))
                except HTTPException:
                    values.append({**row, "status": "unavailable"})
            return {"conversation": public_conversation(c), "tasks": values}

    @app.post("/api/conversations/{conversation_id}/turns", status_code=202)
    def turn(conversation_id: str, body: Turn, user=Depends(current_user), repository=Depends(repo)):
        input_value = body.model_dump(exclude={"request_id", "expected_version"})
        if not permitted(user, input_value):
            raise HTTPException(403, "没有该指标权限")
        with repository.db.transaction() as conn:
            c = repository.conversation(conn, user, conversation_id, lock=True)
            old = conn.execute("SELECT id,input FROM tasks WHERE conversation_id=%s AND request_id=%s", (conversation_id, body.request_id)).fetchone()
            if old:
                if old["input"] != input_value:
                    raise HTTPException(409, "重复请求对应不同问题")
                return public_task(repository.task(conn, user, old["id"]))
            if c["version"] != body.expected_version or c["active_task_id"] or c["turn_count"] >= 30:
                raise HTTPException(409, "会话已更新、正在执行或已达 30 轮")
            repository.capacity(conn, user)
            version = c["version"]+1
            task_id = repository.insert_task(conn, user, input_value, conversation_id=conversation_id, conversation_turn=c["turn_count"]+1,
                request_id=body.request_id, admission_version=version, context={"binding": c["binding"], "version": version, "plan": c["plan"], "pending": c["pending"]})
            conn.execute("UPDATE conversations SET version=%s,turn_count=turn_count+1,active_task_id=%s,updated_at=now() WHERE id=%s", (version, task_id, conversation_id))
            return public_task(repository.task(conn, user, task_id))

    @app.post("/api/conversations/{conversation_id}/discard-pending")
    def discard(conversation_id: str, body: Version, user=Depends(current_user), repository=Depends(repo)):
        with repository.db.transaction() as conn:
            c = repository.conversation(conn, user, conversation_id, lock=True)
            if c["version"] != body.expected_version or c["active_task_id"]:
                raise HTTPException(409, "会话已更新或正在执行")
            conn.execute("UPDATE conversations SET pending=NULL,version=version+1,updated_at=now() WHERE id=%s", (conversation_id,))
            return public_conversation(repository.conversation(conn, user, conversation_id))

    @app.get("/api/users")
    def users(user=Depends(current_user), repository=Depends(repo)):
        admin(user)
        with repository.db.transaction() as conn:
            rows = conn.execute("SELECT id,tenant_id,username,role,allowed_metrics,enabled FROM users WHERE tenant_id=%s ORDER BY username LIMIT 200", (user["tenant_id"],)).fetchall()
            return [{"enabled": row.pop("enabled"), "user": row} for row in rows]

    @app.post("/api/users", status_code=201)
    def add_user(body: UserInput, user=Depends(current_user), repository=Depends(repo)):
        admin(user)
        value = dict(id=secrets.token_hex(24), tenant_id=user["tenant_id"], username=body.username, role=body.role, allowed_metrics=body.allowed_metrics)
        try:
            with repository.db.transaction() as conn:
                conn.execute("INSERT INTO users(id,tenant_id,username,role,allowed_metrics,password_hash) VALUES(%s,%s,%s,%s,%s,%s)", (*value.values(), bcrypt.hashpw(body.password.encode(), bcrypt.gensalt(rounds=12))))
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, "账号已存在")
        return value

    @app.patch("/api/users/{user_id}")
    def update_user(user_id: str, body: Permissions, user=Depends(current_user), repository=Depends(repo)):
        admin(user)
        if user_id == user["id"] or not set(body.allowed_metrics) <= set(catalog().metrics):
            raise HTTPException(400, "不能修改自身或使用未知指标")
        with repository.db.transaction() as conn:
            tag = conn.execute("UPDATE users SET enabled=%s,allowed_metrics=%s WHERE tenant_id=%s AND id=%s", (body.enabled, body.allowed_metrics, user["tenant_id"], user_id))
            if not tag.rowcount:
                raise HTTPException(404, "账号不存在")
        return {"ok": True}

    @app.get("/api/operations/summary")
    def operations(request: Request, days: int = 7, user=Depends(current_user), repository=Depends(repo)):
        admin(user)
        if days not in {1, 7, 30} or set(request.query_params) - {"days"} or len(request.query_params.getlist("days")) > 1:
            raise HTTPException(400, "统计窗口只能为 1、7、30 天")
        statement = Path(__file__).with_name("operations.sql").read_text(encoding="utf-8")
        with repository.db.transaction() as conn:
            return conn.execute(statement, {"tenant": user["tenant_id"], "days": days}).fetchone()["summary"]

    return app


app = create_app()
