"""外部操作先预留再发送，回执和证据同事务保存；图重放只读已完成回执。"""
import hashlib
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from agent.runtime.analysis import canonical, metric_ids, Spec
from agent.runtime.task_contracts import load_spec
from .policy import Policy


class BudgetExceeded(RuntimeError):
    def __init__(self,message,code='other_budget'):
        super().__init__(message)
        self.code=code


class Ledger:
    def __init__(self, repo, task):
        self.repo, self.task = repo, task

    def begin(self, op_id, phase, payload, snapshot):
        with self.repo.db.transaction() as conn:
            self.repo.execution(conn, self.task)
            run = conn.execute("SELECT * FROM analysis_runs WHERE id=%s FOR UPDATE", (self.task["id"],)).fetchone()
            state, policy = run["state"], Policy.model_validate(run["policy"])
            old = conn.execute("SELECT * FROM analysis_steps WHERE id=%s", (op_id,)).fetchone()
            if old:
                if old["output"] is not None:
                    return old["output"]
                if phase == "decide":
                    result = {"status": "error", "error_code": "unknown_execution", "model_sent": True, "message": "上次模型调用结果未知，停止且保留费用预留"}
                    conn.execute("UPDATE analysis_steps SET status='unknown',output=%s,ended_at=now() WHERE id=%s", (Jsonb(result), op_id))
                    conn.execute("UPDATE analysis_budget_ledger SET status='unknown' WHERE step_id=%s", (op_id,))
                    return result
                # 只读 SQL 上界 10s，任务租约 60s；新 epoch 接管时旧查询已超过执行上界。
                if payload.get("action") == "query_metrics":
                    retry_id = hashlib.sha256((op_id + ":readonly-retry").encode()).hexdigest()[:48]
                    retried = conn.execute("SELECT output FROM analysis_steps WHERE id=%s", (retry_id,)).fetchone()
                    if retried and retried["output"] is not None:
                        return retried["output"]
                    if retried or state["sql_retries"] >= 1 or state["sql_calls"] >= 6 or state["active_ms"]+35000 >= 180000 or state["sequence"] >= 40:
                        raise BudgetExceeded("只读查询结果未知，重试额度已耗尽")
                    if old["started_at"].timestamp() + 15 > datetime.now(timezone.utc).timestamp():
                        raise BudgetExceeded("旧只读查询执行上界尚未结束")
                    conn.execute("UPDATE analysis_steps SET status='unknown',ended_at=now() WHERE id=%s", (op_id,))
                    state["sql_retries"] += 1
                    state["sql_calls"] += 1
                    state["active_ms"] += 35000
                    self._insert(conn, retry_id, phase, payload, run, state, policy, snapshot, reserve=False)
                    return {"_retry_id": retry_id}
                # 纯计算重放无外部副作用，不重复扣预算。
                conn.execute("UPDATE analysis_steps SET execution_version=%s,started_at=now() WHERE id=%s", (self.task["execution_version"], op_id))
                return None
            if state["active_ms"] >= 180000 or state["sequence"] >= 40:
                raise BudgetExceeded("分析活跃时长或步骤预算已耗尽")
            call_bound = policy.model_timeout_seconds * 1000 if phase == "decide" else 20000
            if (phase == "decide" or payload.get("action") == "query_metrics") and state["active_ms"]+call_bound > 180000:
                raise BudgetExceeded("剩余活跃时长不足以安全发起外部调用")
            key = hashlib.sha256(canonical([run["binding"], snapshot.get("spec"), payload]).encode()).hexdigest()
            cached = conn.execute("SELECT output FROM analysis_steps WHERE run_id=%s AND action_key=%s AND status='completed' ORDER BY sequence DESC LIMIT 1", (run["id"], key)).fetchone() if phase == "tool" else None
            if phase == "decide":
                from .task_budget import model_call_limit
                if state['model_calls']>=model_call_limit(conn,self.task['id']):
                    raise BudgetExceeded('模型调用次数已耗尽，可明确追加次数后从此处继续','model_calls_budget')
                if state["tokens"] + policy.reserved_tokens > policy.max_tokens or state["cost"] + policy.reserved_cost > policy.max_cost:
                    raise BudgetExceeded("模型调用、Token 或费用预算已耗尽")
                state["model_calls"] += 1
                state["tokens"] += policy.reserved_tokens
                state["cost"] += policy.reserved_cost
            elif phase == "tool":
                if state["actions"] >= 12:
                    raise BudgetExceeded("工具动作预算已耗尽")
                state["actions"] += 1
                if payload.get("action") == "query_metrics" and not cached:
                    if snapshot.get("retry_sql"):
                        if state["sql_retries"] >= 1:
                            raise BudgetExceeded("全局 SQL 重试额度已耗尽")
                        state["sql_retries"] += 1
                    if state["sql_calls"] >= 6:
                        raise BudgetExceeded("SQL 调用预算已耗尽")
                    state["sql_calls"] += 1
            if phase == "tool":
                if cached:
                    self._insert(conn, op_id, phase, payload, run, state, policy, snapshot, key=key)
                    conn.execute("UPDATE analysis_steps SET status='completed',output=%s,ended_at=now() WHERE id=%s", (Jsonb(cached["output"]), op_id))
                    conn.execute("UPDATE analysis_step_attempts SET cached=true,status='completed' WHERE step_id=%s", (op_id,))
                    return cached["output"]
            self._insert(conn, op_id, phase, payload, run, state, policy, snapshot, key=key)
            return None

    def _insert(self, conn, op_id, phase, payload, run, state, policy, snapshot, *, reserve=True, key=None):
        state["sequence"] += 1
        state["phase"] = phase
        # 仅展示投影；下一节点和权威 spec 保存在图检查点。
        state["spec"] = snapshot.get("spec")
        state["answers"] = snapshot.get("answers", [])
        state["feedback"] = snapshot.get("feedback", {})
        state['repairs'] = snapshot.get('repairs',0)
        state['model_retries'] = snapshot.get('model_retries',0)
        state.pop('recovery_failure',None)
        if state["spec"]:
            run["required_metrics"] = sorted(set(run["required_metrics"]) | set(metric_ids(load_spec(state["spec"]))))
            if state['spec'].get('task_type') == 'analysis':
                from agent.runtime.task_contracts import TaskContract
                from .task_contracts import save_contract
                state['contract_version'] = save_contract(conn,self.repo,self.task,TaskContract.model_validate(state['spec']))
        conn.execute("INSERT INTO analysis_steps(id,tenant_id,run_id,sequence,action_key,phase,execution_version,input,status,resume_round) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'running',%s)", (op_id, run["tenant_id"], run["id"], state["sequence"], key or op_id, phase, self.task["execution_version"], Jsonb(payload),self.task.get('resume_round',0)))
        conn.execute("INSERT INTO analysis_step_attempts(step_id,execution_version) VALUES(%s,%s)", (op_id, self.task["execution_version"]))
        if phase == "decide" and reserve:
            conn.execute("INSERT INTO analysis_budget_ledger(step_id,run_id,reserved_tokens,reserved_cost) VALUES(%s,%s,%s,%s)", (op_id, run["id"], policy.reserved_tokens, policy.reserved_cost))
        conn.execute("UPDATE analysis_runs SET state=%s,required_metrics=%s,version=version+1,updated_at=now() WHERE id=%s", (Jsonb(state), run["required_metrics"], run["id"]))
        self.repo.event(conn, run["id"], "step", {"sequence": state["sequence"], "phase": phase, "status": "running"})

    def complete(self, op_id, output):
        with self.repo.db.transaction() as conn:
            user = self.repo.execution(conn, self.task)
            run = conn.execute("SELECT * FROM analysis_runs WHERE id=%s FOR UPDATE", (self.task["id"],)).fetchone()
            step = conn.execute("SELECT * FROM analysis_steps WHERE id=%s FOR UPDATE", (op_id,)).fetchone()
            if step["output"] is not None:
                return step["output"]
            state, policy = run["state"], Policy.model_validate(run["policy"])
            state["active_ms"] += max(0, int((datetime.now(timezone.utc) - step["started_at"]).total_seconds() * 1000))
            if state["active_ms"] > 180000:
                output = {**output, "status": "error", "error_code": "active_budget", "message": "活跃执行时长预算已耗尽"}
                output.pop("artifact", None)
            if step["phase"] == "decide":
                usage = output.get("usage") or {}
                tokens = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
                known = all(type(v) is int and 0 <= v <= limit for v, limit in zip(tokens, [policy.max_input_bytes, policy.max_output_tokens]))
                if output.get("model_sent") is False:
                    tokens, known = [0, 0], True
                if known:
                    actual_cost = policy.cost(*tokens)
                    state["tokens"] += sum(tokens) - policy.reserved_tokens
                    state["cost"] += actual_cost - policy.reserved_cost
                    conn.execute("UPDATE analysis_budget_ledger SET actual_tokens=%s,actual_cost=%s,status='settled' WHERE step_id=%s", (sum(tokens), actual_cost, op_id))
                else:
                    conn.execute("UPDATE analysis_budget_ledger SET status='unknown' WHERE step_id=%s", (op_id,))
                    if usage:
                        output = {"status": "error", "error_code": "model_usage", "message": "供应商用量超出预留或格式异常，已停止", "model_sent": True}
            art = output.get("artifact")
            if art:
                if not set(art["metrics_used"]) <= set(user["allowed_metrics"]):
                    raise BudgetExceeded("指标权限已变化")
                run["required_metrics"] = sorted(set(run["required_metrics"]) | set(art["metrics_used"]))
                contract_version = conn.execute('SELECT contract_version FROM tasks WHERE id=%s',(run['id'],)).fetchone()['contract_version']
                conn.execute("INSERT INTO analysis_artifacts(tenant_id,run_id,result_ref,body,contract_version,resume_round) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", (run["tenant_id"], run["id"], art["result_ref"], Jsonb(art),contract_version,self.task.get('resume_round',0)))
                from agent.runtime.task_contracts import TaskContract
                if state.get('spec') and state['spec'].get('task_type') == 'analysis':
                    from .task_contracts import save_contract
                    from agent.runtime.exploration import investigated
                    version = save_contract(conn,self.repo,self.task,TaskContract.model_validate(state['spec']))
                    actual = [r['body'] for r in conn.execute('SELECT body FROM analysis_artifacts WHERE run_id=%s AND contract_version=%s ORDER BY created_at,result_ref',(run['id'],version))]
                    state['progress'] = {'contract_version':version,'investigated':investigated(actual)}
            status = "completed" if output.get("status") in {"completed", "needs_clarification"} else "unknown" if output.get("error_code") in {"unknown_execution", "sql_unknown"} else "failed"
            conn.execute("UPDATE analysis_steps SET output=%s,status=%s,ended_at=now() WHERE id=%s", (Jsonb(output), status, op_id))
            conn.execute("UPDATE analysis_step_attempts SET status=%s WHERE step_id=%s", (status, op_id))
            conn.execute("UPDATE analysis_runs SET state=%s,required_metrics=%s,version=version+1,updated_at=now() WHERE id=%s", (Jsonb(state), run["required_metrics"], run["id"]))
            self.repo.event(conn, run["id"], "step", {"sequence": state["sequence"], "phase": step["phase"], "status": status})
            return output

    def call(self, op_id, phase, payload, snapshot, fn):
        cached = self.begin(op_id, phase, payload, snapshot)
        if cached and "_retry_id" not in cached:
            return cached
        if cached:
            op_id = cached["_retry_id"]
        # 抛出的非预期异常不伪装成确定回执：保留 running，恢复时保守分类。
        return self.complete(op_id, fn())
