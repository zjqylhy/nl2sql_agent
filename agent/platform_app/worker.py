"""独立 Worker：领取持久任务并执行整张图，等待或终态才释放名额。"""
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import psycopg
from fastapi import HTTPException
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent.runtime import app as runtime, telemetry
from agent.runtime.analysis import AnalysisError
from .checkpoints import FencedPostgresSaver, LeaseLost
from .graph import build_graph
from .ledger import BudgetExceeded


def preserve_partial(conn,repo,task,run,spec,message):
    """预算或流程失败后只用已有证据收尾，不再调用模型或业务查询。"""
    if not spec or spec.get('task_type') != 'analysis':
        return None
    from agent.runtime.exploration import partial_report
    from agent.runtime.task_contracts import TaskContract
    user = repo.execution(conn,task)
    current = conn.execute('SELECT contract_version FROM tasks WHERE id=%s',(task['id'],)).fetchone()['contract_version']
    items = [r['body'] for r in conn.execute('SELECT body FROM analysis_artifacts WHERE run_id=%s AND contract_version=%s ORDER BY created_at,result_ref',(task['id'],current))]
    result = partial_report(TaskContract.model_validate(spec),items,run['binding'],runtime.sources()[user['tenant_id']+':'+user['role']],message)
    if result:
        conn.execute('INSERT INTO analysis_artifacts(tenant_id,run_id,result_ref,body,contract_version) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
            (task['tenant_id'],task['id'],result['result_ref'],Jsonb(result),current))
    return result['data'] if result else None


@contextmanager
def heartbeat(repo, task):
    stopped = threading.Event()
    def renew():
        while not stopped.wait(10):
            try:
                with repo.db.transaction() as conn:
                    result = conn.execute("UPDATE tasks SET lease_until=now()+interval '60 seconds' WHERE id=%s AND execution_version=%s AND status='querying' AND lease_until>now()", (task["id"], task["execution_version"]))
                    if not result.rowcount:
                        return
            except Exception:
                return  # 不能续租时让围栏阻止后续动作，不假设仍有执行权。
    thread = threading.Thread(target=renew, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1)


def execute_query(repo, task):
    with repo.db.transaction() as conn:
        user = repo.execution(conn, task)
        # 首轮回执维持旧契约；显式补充按版本分开，不能复用上一轮澄清结果。
        version = task["query_version"]
        table = "query_followup_receipts" if version else "query_receipts"
        where = "task_id=%s AND version=%s" if version else "task_id=%s"
        keys = (task["id"], version) if version else (task["id"],)
        receipt = conn.execute(f"SELECT * FROM {table} WHERE {where} FOR UPDATE", keys).fetchone()
        if receipt and receipt["status"] == "running":
            if (receipt['output'] or {}).get('program_state'):
                # 初始模型回执已转为有效完整计划，后续只恢复程序步骤。
                output = None
            else:
                # 初始解析也可能调用模型，不能因 Worker 崩溃自动重发。
                if task.get('push_thread_id'):
                    conn.execute("UPDATE push_model_calls SET status='unknown' WHERE task_id=%s AND status='running'",(task['id'],))
                repo.finish(conn, task, "failed", message="上次问数调用结果未知，请新建任务")
                return
        elif receipt:
            output = receipt["output"]
        else:
            from .task_intent import reserve_initial
            reservation = reserve_initial(conn,task)
            if version:
                conn.execute("INSERT INTO query_followup_receipts(task_id,version,status,output) VALUES(%s,%s,'running',%s)", (*keys,Jsonb({'intent_reservation':reservation}) if reservation else None))
            else:
                conn.execute("INSERT INTO query_receipts(task_id,status,output) VALUES(%s,'running',%s)", (*keys,Jsonb({'intent_reservation':reservation}) if reservation else None))
            output = None
    if output is None:
        request = runtime.QueryRequest(tenant_id=user["tenant_id"], role=user["role"], allowed_metrics=user["allowed_metrics"],
            input=task["input"], execution={"task_id": task["id"], "execution_version": task["execution_version"]}, context=task["conversation_context"],
            initial_max_output_tokens=(task.get('intent_policy') or {}).get('max_output_tokens',2048),
            initial_model=(task.get('intent_policy') or {}).get('model'),
            **({"push_dialogue": task["query_dialogue"]} if task["query_dialogue"] else {}))
        try:
            with telemetry.capture(request.execution) as trace:
                if task.get('push_turn_context'):
                    from .push_thread_runtime import run_tree_query
                    output = run_tree_query(repo,task,request)
                else:
                    source = runtime.sources().get(user['tenant_id']+':'+user['role'])
                    if source and source.domain == 'push':
                        from .query_execution import QueryExecution
                        from agent.runtime.push_query import run_push_query
                        output = run_push_query(request,source,hooks=QueryExecution(repo,task))
                    else:
                        output = runtime.run_query(request)
        finally:
            with repo.db.transaction() as conn:
                conn.execute("UPDATE task_attempts SET telemetry=%s,error_code=%s WHERE task_id=%s AND execution_version=%s", (Jsonb(trace), trace.get("error_code", ""), task["id"], task["execution_version"]))
        output["telemetry"] = trace
        if output['status'] == 'analysis_ready':
            from .task_intent import promote_analysis
            promote_analysis(repo,task,output)
            with repo.db.transaction() as conn:
                conn.execute(f"UPDATE {table} SET status='completed',output=%s WHERE {where}",(Jsonb({'status':'routed','task_type':'analysis'}),*keys))
                # 类型在识别后才确定，交回队列才能按分析名额重新领取，不能绕过并发上限。
                repo.finish(conn,task,'pending',{'status':'pending','task_type':'analysis'})
            return
        with repo.db.transaction() as conn:
            user = repo.execution(conn, task)
            from .repository import permitted
            if not permitted(user, output):
                raise HTTPException(403, "结果指标权限已撤销")
            conn.execute(f"UPDATE {table} SET status='completed',output=%s WHERE {where}", (Jsonb(output), *keys))
    with repo.db.transaction() as conn:
        repo.finish(conn, task, output["status"], output)


def execute_analysis(repo, task):
    with repo.db.transaction() as conn:
        repo.execution(conn, task)
        run = conn.execute("SELECT * FROM analysis_runs WHERE id=%s", (task["id"],)).fetchone()
    dsn = os.getenv("CHECKPOINT_DATABASE_URL", repo.db.dsn)
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row, connect_timeout=5,
            options="-c search_path=agent_checkpoint,pg_catalog -c statement_timeout=5000") as connection:
        saver = FencedPostgresSaver(connection, task["id"], task["execution_version"])
        graph = build_graph(repo, task, saver)
        config = {"configurable": {"thread_id": task["id"]}, "recursion_limit": 180}
        checkpoint = graph.get_state(config)
        value = None
        if not checkpoint.values:
            value = dict(question=task["input"]["question"], spec=run["state"].get("spec"), answers=run['state'].get('answers',[]), feedback={}, sequence=run['state'].get('sequence',0), refs=[])
        elif checkpoint.interrupts:
            waiting = checkpoint.interrupts[0].value
            response = run["resume_input"]
            if waiting["kind"] == "backoff":
                value = Command(resume=True)
            elif response and response["wait_id"] == waiting["wait_id"]:
                value = Command(resume=response["answer"])
        result = graph.invoke(value, config, durability="sync")
        checkpoint = graph.get_state(config)
        with repo.db.transaction() as conn:
            repo.execution(conn, task)
            current = conn.execute("SELECT * FROM analysis_runs WHERE id=%s FOR UPDATE", (task["id"],)).fetchone()
            state = current["state"]
            for key in ("spec", "answers", "feedback", "repairs", "model_retries", "message", 'pending_scope'):
                if key in result:
                    state[key] = result[key]
            if checkpoint.interrupts:
                wait = checkpoint.interrupts[0].value
                state["wait_id"] = wait["wait_id"]
                state["phase"] = "waiting" if wait["kind"] == "clarification" else "backoff"
                status = "needs_clarification" if wait["kind"] == "clarification" else "pending"
                state["message"] = wait.get("message", "只读查询临时失败，等待有限重试")
                seconds = wait.get("seconds", 0)
                conn.execute("UPDATE tasks SET ready_at=now()+(%s * interval '1 second') WHERE id=%s", (seconds, task["id"]))
            else:
                status = result.get("status", "failed")
                if status not in {"completed", "failed"}:
                    status = "failed"
                state["phase"] = status
                if status == "completed":
                    state["message"] = ""
            conn.execute("UPDATE analysis_runs SET state=%s,resume_input=NULL,version=version+1,updated_at=now() WHERE id=%s", (Jsonb(state), task["id"]))
            report = conn.execute("SELECT body FROM analysis_artifacts WHERE run_id=%s AND contract_version=(SELECT contract_version FROM tasks WHERE id=%s) AND body->>'kind'='report' ORDER BY created_at DESC LIMIT 1", (task["id"],task['id'])).fetchone()
            data = report['body']['data'] if report else preserve_partial(conn,repo,task,current,state.get('spec'),state.get('message') or '调查停止，仍有未完成事项') if status=='failed' else None
            repo.finish(conn, task, status, data or {"partial": True}, state.get("message", ""))


def execute(repo, task):
    started = time.monotonic()
    try:
        with heartbeat(repo, task):
            (execute_analysis if task["kind"] == "analysis" else execute_query)(repo, task)
    except LeaseLost:
        pass  # 取消或接管已生效；迟到执行者不能再改终态。
    except (HTTPException, BudgetExceeded, AnalysisError) as exc:
        message = exc.detail if isinstance(exc, HTTPException) else str(exc)
        try:
            with repo.db.transaction() as conn:
                partial = None
                if task["kind"] == "analysis":
                    row = conn.execute("SELECT * FROM analysis_runs WHERE id=%s", (task["id"],)).fetchone()
                    state = {**row["state"], "phase": "failed", "message": message}
                    if isinstance(exc,BudgetExceeded):
                        partial = preserve_partial(conn,repo,task,row,state.get('spec'),message)
                    conn.execute("UPDATE analysis_runs SET state=%s,version=version+1 WHERE id=%s", (Jsonb(state), task["id"]))
                repo.finish(conn, task, "failed", result=partial, message=message, authorize=isinstance(exc,BudgetExceeded))
        except (LeaseLost, HTTPException):
            pass
    except Exception as exc:
        # 包括检查点/回执写失败：保留租约和账本，超时后按未知操作恢复。
        logging.error("worker interrupted: task=%s error=%s", task["id"], type(exc).__name__)
    finally:
        with repo.db.transaction() as conn:
            if task["kind"] == "analysis":
                rows = conn.execute("SELECT s.phase,s.input,s.output,s.status,a.cached,l.actual_tokens,l.status budget_status,s.started_at,s.ended_at FROM analysis_steps s JOIN analysis_step_attempts a ON a.step_id=s.id LEFT JOIN analysis_budget_ledger l ON l.step_id=s.id WHERE s.run_id=%s AND s.execution_version=%s ORDER BY s.sequence", (task["id"], task["execution_version"])).fetchall()
                model_steps = [s for s in rows if s["phase"] == "decide" and (s["output"] or {}).get("model_sent") is not False]
                unknown = any(s["budget_status"] in {"reserved", "unknown"} for s in model_steps)
                known = sum(s["actual_tokens"] or 0 for s in rows)
                stages = {}
                for row in rows:
                    if row["ended_at"]:
                        name = "analysis_"+row["phase"]
                        stages[name] = stages.get(name, 0)+(row["ended_at"]-row["started_at"]).total_seconds()*1000
                trace = dict(version=1, task_id=task["id"], execution_version=task["execution_version"], parser="analysis",
                    model_calls=len(model_steps), total_tokens=None if unknown else known, known_tokens=known,
                    sql_calls=sum(r["phase"] == "tool" and r["input"].get("action") == "query_metrics" and not r["cached"] for r in rows),
                    stages_ms=stages, error_code=next(((r["output"] or {}).get("error_code", "") for r in reversed(rows) if (r["output"] or {}).get("error_code")), ""))
                conn.execute("UPDATE task_attempts SET telemetry=%s,error_code=%s WHERE task_id=%s AND execution_version=%s", (Jsonb(trace), trace["error_code"], task["id"], task["execution_version"]))
            conn.execute("UPDATE task_attempts SET call_ms=%s WHERE task_id=%s AND execution_version=%s", (int((time.monotonic()-started)*1000), task["id"], task["execution_version"]))


def run(repo, policy, stop=None):
    stop = stop or threading.Event()
    futures = set()
    last_cleanup = 0
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="agent-worker") as executor:
        while not stop.wait(0.25):
            if time.monotonic()-last_cleanup >= 60:
                from .retention import prune
                try:
                    prune(repo)
                except Exception:
                    logging.error("retention temporarily unavailable")
                last_cleanup = time.monotonic()
            futures = {f for f in futures if not f.done()}
            if len(futures) >= 8:
                continue
            task = repo.claim(policy is not None)
            if task:
                futures.add(executor.submit(execute, repo, task))
