"""平台状态唯一写入口。图拥有下一节点，账本拥有外部调用回执和预算。"""
import hashlib
import secrets
from functools import lru_cache
from pathlib import Path
from datetime import datetime, timezone

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from agent.runtime.analysis import canonical, metric_ids, Spec
from agent.runtime.task_contracts import load_spec
from agent.runtime.catalog import catalog
from agent.runtime.multiturn import source_metadata
from agent.runtime import app as runtime
from agent.runtime.analysis_api import Identity, metadata
from .checkpoints import LeaseLost

ACTIVE = {"pending", "querying", "needs_clarification"}
USER_COLUMNS = "id,tenant_id,username,role,allowed_metrics"
GRAPH_VERSION = "stategraph-1"


@lru_cache
def code_digest():
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for folder in (root/"runtime", root/"platform_app"):
        for path in sorted(folder.rglob("*.py")):
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    digest.update((root/"runtime/catalog.json").read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def identity(user):
    return Identity(tenant_id=user["tenant_id"], role=user["role"], allowed_metrics=user["allowed_metrics"])


def binding(user, analysis=False):
    source = runtime.sources().get(user["tenant_id"] + ":" + user["role"])
    if source is None or source.driver != "postgresql":
        raise HTTPException(403, "未配置 PostgreSQL 授权数据源")
    return {**metadata(identity(user))[1], "graph_version": GRAPH_VERSION, "code_digest": code_digest()} if analysis else source_metadata(source, user["tenant_id"], user["role"])


def metrics_in(value):
    """覆盖结果、计划和待确认操作；指标撤权后不能继续读历史证据。"""
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"metrics", "metrics_used", "required_metrics", "allowed_metrics"} and isinstance(item, list):
                found.update(v.get("id") if isinstance(v, dict) else v for v in item)
            elif key in {'metric','rank_metric'} and isinstance(item, str) and item:
                found.add(item)
            elif key == "op" and item == "set_metrics":
                found.update(value.get("value", []))
            else:
                found.update(metrics_in(item))
    elif isinstance(value, list):
        for item in value:
            found.update(metrics_in(item))
    return found


def permitted(user, value):
    from agent.runtime.push import DEFINITIONS
    return metrics_in(value) <= set(user["allowed_metrics"]) & (set(catalog().metrics) | set(DEFINITIONS))


def new_state(spec=None):
    return dict(phase="decide", spec=spec, feedback={}, answers=[], sequence=0, model_calls=0, actions=0,
                sql_calls=0, repairs=0, sql_retries=0, model_retries=0, clarifications=0, active_ms=0, tokens=0, cost=0, message="")


def query_clarification(task):
    result = task.get("result") or {}
    available = (task["kind"] == "query" and not task["conversation_id"]
                 and not task["input"].get("push_plan") and result.get("usage", {}).get("parser") == "llm")
    reason = ""
    if task.get("query_version", 0) >= 2:
        reason = "本次查询已补充 2 次，请修改原问题后新建查询。"
    elif (datetime.now(timezone.utc)-task["updated_at"]).total_seconds() > 86400:
        reason = "等待补充已超过 24 小时，请新建查询。"
    return dict(version=task.get("query_version", 0), turns=task.get("query_dialogue", []),
                can_answer=available and task["status"] == "needs_clarification" and not reason, reason=reason)


def public_task(task):
    value = {key: value for key, value in task.items() if key not in {"conversation_context", "admission_version", "lease_until", "next_seq", "ready_at", "query_dialogue", "query_version", "push_turn_context"}}
    if task["kind"] == "query" and not task["conversation_id"]:
        value["clarification"] = query_clarification(task)
    return value


def public_conversation(value):
    return {key: item for key, item in value.items() if key not in {"tenant_id", "user_id", "role"}}


class Repository:
    def __init__(self, db):
        self.db = db

    def user(self, conn, user_id, tenant):
        value = conn.execute(f"SELECT {USER_COLUMNS} FROM users WHERE id=%s AND tenant_id=%s AND enabled FOR SHARE", (user_id, tenant)).fetchone()
        if not value:
            raise HTTPException(403, "账号已停用")
        return value

    def event(self, conn, run_id, kind, data):
        seq = conn.execute("UPDATE tasks SET next_seq=next_seq+1,updated_at=now() WHERE id=%s RETURNING next_seq", (run_id,)).fetchone()["next_seq"]
        conn.execute("INSERT INTO task_events(task_id,seq,kind,data) VALUES(%s,%s,%s,%s)", (run_id, seq, kind, Jsonb(data)))

    def task(self, conn, user, run_id, *, lock=False, check_binding=True):
        task = conn.execute("SELECT * FROM tasks WHERE id=%s AND tenant_id=%s" + (" FOR UPDATE" if lock else ""), (run_id, user["tenant_id"])).fetchone()
        if not task:
            raise HTTPException(404, "任务不存在或无权访问")
        private = task["kind"] == "analysis" or bool(task["conversation_id"]) or bool(task.get('push_thread_id'))
        if task["user_id"] != user["id"] and (private or user["role"] not in {"admin", "data"}):
            raise HTTPException(404, "任务不存在或无权访问")
        if not permitted(user, [task["input"], task["result"] if task["kind"] == "query" else None, task["conversation_context"]]):
            raise HTTPException(404, "任务不存在或指标权限已变化")
        if task["kind"] == "analysis":
            run = conn.execute("SELECT * FROM analysis_runs WHERE id=%s", (run_id,)).fetchone()
            meta = binding(user, True) if check_binding else run["metadata"]
            changed_code = any(run["metadata"].get(k) != meta.get(k) for k in ("graph_version", "code_digest"))
            historical=False
            if task['status'] not in ACTIVE and run['metadata'].get('runtime_version')=='push-1':
                from .historical_results import legacy_push_binding
                historical=run['binding']==legacy_push_binding(user,analysis_model=run['metadata'].get('model',''))
            if run["role"] != user["role"] or not set(run["required_metrics"]) <= set(user["allowed_metrics"]) or (check_binding and (run["binding"] != meta["binding"] and not historical or task["status"] in ACTIVE and changed_code)):
                raise HTTPException(404, "任务授权或数据绑定已变化")
        elif task["conversation_id"]:
            self.conversation(conn, user, task["conversation_id"])
        elif task.get("query_binding"):
            historical=False
            if task['status'] not in ACTIVE and (task.get('result') or {}).get('catalog_version')=='push-1':
                from .historical_results import legacy_push_binding
                historical=task['query_binding']==legacy_push_binding(user)
            if task['query_role']!=user['role'] or task['query_binding']!=binding(user)['binding'] and not historical:
                raise HTTPException(404, "任务角色或数据绑定已变化")
        if task.get('push_thread_id'):
            from .push_threads import Threads
            Threads(self).authorize(conn,user,task['push_thread_id'])
        from .task_retry import retry_info
        task['retry'] = retry_info(conn,user,task)
        return task

    def conversation(self, conn, user, conversation_id, *, lock=False):
        value = conn.execute("SELECT * FROM conversations WHERE id=%s AND tenant_id=%s AND user_id=%s" + (" FOR UPDATE" if lock else ""), (conversation_id, user["tenant_id"], user["id"])).fetchone()
        if not value or value["role"] != user["role"] or not permitted(user, [value["plan"], value["pending"]]) or value["binding"] != binding(user)["binding"]:
            raise HTTPException(404, "会话不存在、权限或数据基准已变化")
        return value

    def capacity(self, conn, user):
        conn.execute("SELECT pg_advisory_xact_lock(710091)")
        counts = conn.execute("SELECT count(*) FILTER(WHERE status='pending') pending,count(*) FILTER(WHERE user_id=%s AND status IN ('pending','querying')) own FROM tasks", (user["id"],)).fetchone()
        if counts["pending"] >= 100 or counts["own"]:
            raise HTTPException(429, "队列已满或已有执行中的任务")

    def insert_task(self, conn, user, input_value, **extra):
        run_id = secrets.token_hex(24)
        conn.execute("INSERT INTO tasks(id,tenant_id,user_id,input,kind,conversation_id,conversation_turn,request_id,conversation_context,admission_version) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (run_id, user["tenant_id"], user["id"], Jsonb(input_value), extra.get("kind", "query"), extra.get("conversation_id"), extra.get("conversation_turn"), extra.get("request_id"), Jsonb(extra["context"]) if "context" in extra else None, extra.get("admission_version")))
        conn.execute("INSERT INTO task_outbox(task_id) VALUES(%s)", (run_id,))
        if extra.get("kind", "query") == "query":
            conn.execute("UPDATE tasks SET query_role=%s,query_binding=%s WHERE id=%s", (user["role"], binding(user)["binding"], run_id))
        self.event(conn, run_id, "accepted", {"task_id": run_id})
        return run_id

    def create_analysis(self, conn, user, request_id, question, spec, policy):
        digest = hashlib.sha256(canonical([question, spec]).encode()).hexdigest()
        conn.execute("SELECT pg_advisory_xact_lock(710091)")
        old = conn.execute("SELECT id,request_hash FROM analysis_runs WHERE tenant_id=%s AND user_id=%s AND request_id=%s", (user["tenant_id"], user["id"], request_id)).fetchone()
        if old:
            if old["request_hash"] != digest:
                raise HTTPException(409, "相同请求标识对应不同内容")
            return self.analysis(conn, user, old["id"])
        self.capacity(conn, user)
        meta = binding(user, True)
        run_id = self.insert_task(conn, user, {"question": question}, kind="analysis")
        required = metric_ids(load_spec(spec)) if spec else []
        conn.execute("INSERT INTO analysis_runs(id,tenant_id,user_id,role,binding,metadata,policy,state,required_metrics,request_id,request_hash) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (run_id, user["tenant_id"], user["id"], user["role"], meta["binding"], Jsonb(meta), Jsonb(policy.model_dump()), Jsonb(new_state(spec)), required, request_id, digest))
        return self.analysis(conn, user, run_id)

    def analysis(self, conn, user, run_id):
        task = self.task(conn, user, run_id)
        run = conn.execute("SELECT * FROM analysis_runs WHERE id=%s", (run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "分析任务不存在")
        artifacts = [r["body"] for r in conn.execute("SELECT body FROM analysis_artifacts WHERE run_id=%s AND contract_version=%s ORDER BY created_at,result_ref", (run_id,task.get('contract_version',0)))]
        steps = conn.execute("SELECT s.id,s.sequence,s.phase,s.input->>'action' action,s.input->'arguments' arguments,s.status,s.started_at,s.ended_at,s.output->>'error_code' error_code,a.cached FROM analysis_steps s JOIN analysis_step_attempts a ON a.step_id=s.id WHERE run_id=%s ORDER BY sequence", (run_id,)).fetchall()
        usage = conn.execute("SELECT coalesce(sum(actual_tokens),0) known_tokens,coalesce(sum(actual_cost),0) known_cost_micros,count(*) FILTER(WHERE status IN ('reserved','unknown')) unknown_calls FROM analysis_budget_ledger WHERE run_id=%s", (run_id,)).fetchone()
        return dict(id=run_id, question=task["input"]["question"], status=task["status"], version=run["version"], metadata=run["metadata"], policy=run["policy"], state=run["state"], metrics_used=run["required_metrics"], artifacts=artifacts, steps=steps, usage=run["state"].get("archived_usage", usage), retry=task['retry'])

    def execution(self, conn, task, *, authorize=True):
        current = conn.execute("SELECT * FROM tasks WHERE id=%s FOR UPDATE", (task["id"],)).fetchone()
        if current["status"] != "querying" or current["execution_version"] != task["execution_version"] or current["lease_until"] <= datetime.now(timezone.utc):
            raise LeaseLost("任务已停止或租约失效")
        if not authorize:
            return None
        user = self.user(conn, task["user_id"], task["tenant_id"])
        if authorize:
            self.task(conn, user, task["id"])
        return user

    def claim(self, analysis_enabled=True):
        with self.db.transaction() as conn:
            # 容量锁串行保护跨进程名额；行锁 SKIP LOCKED 领取具体任务。
            conn.execute("SELECT pg_advisory_xact_lock(710091)")
            if conn.execute("SELECT count(*) n FROM tasks WHERE status='querying' AND lease_until>now()").fetchone()["n"] >= 50:
                return None
            task = conn.execute("""SELECT t.* FROM tasks t JOIN task_outbox o ON o.task_id=t.id
                WHERE o.delivered_at IS NULL AND t.ready_at<=now()
                AND (t.status='pending' OR (t.status='querying' AND t.lease_until<now()))
                AND (t.kind='query' OR (%s AND (SELECT count(*) FROM tasks a WHERE a.kind='analysis' AND a.id<>t.id AND a.status='querying' AND a.lease_until>now())<2))
                AND NOT EXISTS(SELECT 1 FROM tasks a WHERE a.user_id=t.user_id AND a.id<>t.id AND a.status='querying' AND a.lease_until>now())
                ORDER BY t.created_at LIMIT 1 FOR UPDATE OF t SKIP LOCKED""", (analysis_enabled,)).fetchone()
            if not task:
                return None
            conn.execute("UPDATE task_attempts SET state='lease_expired',ended_at=now(),error_code='lease_expired' WHERE task_id=%s AND state='running'", (task["id"],))
            task = conn.execute("UPDATE tasks SET status='querying',attempts=attempts+1,execution_version=execution_version+1,lease_until=now()+interval '60 seconds',started_at=coalesce(started_at,now()) WHERE id=%s RETURNING *", (task["id"],)).fetchone()
            conn.execute("INSERT INTO task_attempts(tenant_id,task_id,execution_version,queue_ms) VALUES(%s,%s,%s,%s)", (task["tenant_id"], task["id"], task["execution_version"], max(0, (datetime.now(timezone.utc)-task["created_at"]).total_seconds()*1000)))
            return task

    def finish(self, conn, task, status, result=None, message="", *, authorize=True):
        user = self.execution(conn, task, authorize=authorize)
        if authorize and task["kind"] == "query" and not permitted(user, result):
            raise HTTPException(403, "结果指标权限已撤销")
        if task["conversation_id"]:
            c = conn.execute("SELECT * FROM conversations WHERE id=%s FOR UPDATE", (task["conversation_id"],)).fetchone()
            if c["active_task_id"] != task["id"] or c["version"] != task["admission_version"]:
                raise LeaseLost("会话已发生变化")
            if status in {"completed", "needs_clarification"}:
                if not result or result.get("context_binding") != c["binding"]:
                    raise ValueError("会话结果绑定不一致")
                if status == "completed":
                    c["plan"], c["pending"] = result["plan"], None
                else:
                    c["pending"] = result.get("multiturn", {}).get("pending", c["pending"])
            conn.execute("UPDATE conversations SET plan=%s,pending=%s,active_task_id=NULL,version=version+1,updated_at=now() WHERE id=%s", (Jsonb(c["plan"]), Jsonb(c["pending"]), c["id"]))
        if task.get('push_thread_id'):
            from .push_threads import Threads
            Threads(self).finish(conn,task,status,result,message)
        conn.execute("UPDATE tasks SET status=%s,result=%s,error=%s,lease_until=NULL,updated_at=now() WHERE id=%s", (status, Jsonb(result), message, task["id"]))
        conn.execute("UPDATE task_attempts SET state=%s,ended_at=now() WHERE task_id=%s AND execution_version=%s", (status if status != "pending" else "completed", task["id"], task["execution_version"]))
        if status not in ACTIVE or (status == "needs_clarification" and task["kind"] == "query"):
            conn.execute("UPDATE task_outbox SET delivered_at=now() WHERE task_id=%s", (task["id"],))
        self.event(conn, task["id"], {"completed": "done", "failed": "error", "needs_clarification": "clarification"}.get(status, "step"), {"status": status, "message": message})
