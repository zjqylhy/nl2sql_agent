"""单 Agent 的受限 ReAct 图；图控制循环，平台事务强制授权、回执和预算。"""
import hashlib
from typing import TypedDict

from langgraph.graph import START, END, StateGraph
from langgraph.types import interrupt

# 共享查询模块先完成初始化，再加载保留的内部工具适配器。
from agent.runtime import app as runtime
from agent.runtime.analysis_api import Snapshot, ToolRequest, checked, decide, decision_prompt, fixed_decide, parse_spec, spec_prompt, tool
from .ledger import Ledger
from .repository import identity


class State(TypedDict, total=False):
    question: str
    spec: dict | None
    answers: list[str]
    feedback: dict
    sequence: int
    op_id: str
    decision: dict
    output: dict
    refs: list[str]
    status: str
    message: str
    repairs: int
    sql_retries: int
    retry_sql: bool
    model_retries: int
    no_progress: int
    observed_rows: list[str]
    pending_scope: dict | None


def build_graph(repo, task, saver):
    ledger = Ledger(repo, task)

    def snapshot(state):
        with repo.db.transaction() as conn:
            user = repo.execution(conn, task)
            run = conn.execute("SELECT * FROM analysis_runs WHERE id=%s", (task["id"],)).fetchone()
            current = conn.execute('SELECT contract_version FROM tasks WHERE id=%s',(task['id'],)).fetchone()['contract_version']
            contract = conn.execute('SELECT body FROM task_contracts WHERE task_id=%s AND version=%s',(task['id'],current)).fetchone() if current else None
            # 新约定尚待本轮账本保存时不读取旧版本证据。
            changed = contract and contract['body'] != state.get('spec')
            artifacts = [] if changed else [r["body"] for r in conn.execute("SELECT body FROM analysis_artifacts WHERE run_id=%s AND contract_version=%s ORDER BY created_at,result_ref", (task["id"],current))]
        value = Snapshot(**identity(user).model_dump(), binding=run["binding"], question=state["question"],
            spec=state.get("spec"), artifacts=artifacts, answers=state.get("answers", []), feedback=state.get("feedback", {}),
            max_output_tokens=run["policy"].get("max_output_tokens", 1024),
            max_input_bytes=run['policy'].get('max_input_bytes',6000),
            model_timeout_seconds=run["policy"].get("model_timeout_seconds", 20))
        value.budget = {name:max(0,limit-run['state'].get(name,0)) for name,limit in [('model_calls',10),('actions',12),('sql_calls',6),('active_ms',180000)]}
        checked(value)
        return value, run

    def prepare(state):
        seq = state.get("sequence", 0) + 1
        return {"sequence": seq, "op_id": hashlib.sha256(f"{task['id']}:{seq}".encode()).hexdigest()[:48]}

    def model_interval(state):
        with repo.db.transaction() as conn:
            run = conn.execute("SELECT policy,state,metadata FROM analysis_runs WHERE id=%s", (task["id"],)).fetchone()
        seconds = run["policy"].get("model_interval_seconds", 0)
        needs_model = run['metadata'].get('domain')=='push' or run['policy']['strategy'] != 'fixed' or state.get('spec') is None
        if seconds and needs_model and run["state"]["model_calls"]:
            # 等待不占 Worker 线程，不预占尚未发送的模型费用，也不会重放已完成调用。
            interrupt({"kind": "backoff", "wait_id": state["op_id"], "seconds": seconds, "message": "等待已配置的模型调用间隔"})
        return {}

    def decision(state):
        request, run = snapshot(state)
        # 开放分析必须由观测驱动；旧订单域的固定策略仍按原协议运行。
        open_push = run['metadata'].get('domain') == 'push' or (state.get('spec') or {}).get('task_type') == 'analysis'
        fixed = not open_push and run["policy"]["strategy"] == "fixed" and state.get("spec") is not None
        spec_only = not open_push and run['policy']['strategy'] == 'fixed' and not fixed
        phase = "fixed" if fixed else "decide"
        payload = {"spec": request.spec.model_dump(mode="json") if request.spec else None,
                   "answers": request.answers, "feedback": request.feedback, "artifact_refs": state.get("refs", [])}
        if not fixed:
            from datetime import date
            prompt, question = (spec_prompt if spec_only else decision_prompt)(request, date.fromisoformat(run["metadata"]["analysis_date"]))
            payload.update(prompt=prompt, question=question, model=run["metadata"]["model"],
                decision_protocol='spec-only-v1' if spec_only else 'adaptive-short-refs-v2',
                max_output_tokens=request.max_output_tokens, model_timeout_seconds=request.model_timeout_seconds)
        output = ledger.call(state["op_id"], phase, payload, state,
            lambda: (fixed_decide if fixed else parse_spec if spec_only else decide)(request))
        return {"output": output}

    def interpret(state):
        output = state["output"]
        if output.get("status") != "completed":
            return failure(state, output)
        value = output["decision"]
        return {"spec": value["spec"], "decision": value["action"], "status": "tool", "retry_sql": False}

    def execute_tool(state):
        request, _ = snapshot(state)
        output = ledger.call(state["op_id"], "tool", state["decision"], state,
            lambda: tool(ToolRequest(**request.model_dump(), action=state["decision"])))
        return {"output": output}

    def failure(state, output):
        code = output.get("error_code", "internal")
        if code == "model_rate_limit":
            if state.get("model_retries", 0) == 0:
                return {"model_retries": 1, "feedback": output, "status": "model_backoff"}
            return {"status": "failed", "message": "模型网关仍拒绝请求，有限重试已耗尽", "feedback": output}
        if code in {"invalid_arguments", "model_format", "missing_conditions", "missing_evidence"} and state.get("repairs", 0) == 0:
            return {"repairs": 1, "feedback": output, "status": "decide", "retry_sql": False}
        return {"status": "failed", "message": output.get("message", "分析未完成"), "feedback": output}

    def observe(state):
        output = state["output"]
        if output.get("status") == "needs_clarification":
            if len(state.get("answers", [])) >= 2:
                return {"status": "failed", "message": "补充轮数已耗尽"}
            return {"status": "waiting", "message": output["message"], 'pending_scope':output.get('scope_proposal')}
        if output.get("status") != "completed":
            if output.get("error_code") in {"sql_timeout", "sql_connection", "sql_unknown"} and state.get("sql_retries", 0) == 0:
                return {"sql_retries": 1, "retry_sql": True, "status": "backoff", "feedback": output}
            return failure(state, output)
        art = output["artifact"]
        refs = state.get("refs", [])
        observed_rows=set(state.get('observed_rows',[]))
        repeated=art['result_ref'] in refs
        if art['kind']=='observation':
            rows={f"{art['data']['source_ref']}:{r['source_row']}" for r in art['data']['rows']}
            # 页大小或排序参数改变但返回相同行，不算调查取得新进展。
            repeated=not bool(rows-observed_rows)
            observed_rows.update(rows)
        no_progress = state.get("no_progress", 0) + 1 if repeated else 0
        if no_progress >= 2:
            message = '重复调用未产生新证据，已停止'
            return {'status':'failed','message':message,'feedback':{'status':'error','error_code':'no_progress','message':message}}
        report_status = 'failed' if art['data'].get('completeness') == 'partial' else 'completed'
        feedback={"status": "completed", "result_ref": art["result_ref"], "outcome": art["data"].get("outcome")}
        if repeated:
            feedback['message']='本次没有读到新行；请换调查方向或明确未回答事项后提交报告，不要重复读取相同行。'
        return {"refs": list(dict.fromkeys([*refs, art["result_ref"]])), "no_progress": no_progress,'observed_rows':sorted(observed_rows),
            "status": report_status if art["kind"] == "report" else "decide", "retry_sql": False,
            "feedback": feedback}

    def wait_for_answer(state):
        # 此节点只执行 interrupt 和纯状态变换，恢复从开头执行也没有外部副作用。
        answer = interrupt({"kind": "clarification", "message": state["message"], "wait_id": state["op_id"], 'scope_proposal':state.get('pending_scope')})
        if state.get('pending_scope'):
            if not isinstance(answer,dict) or type(answer.get('approved')) is not bool:
                return {'status':'failed','message':'范围变更需要明确确认，不能用自由文本代替'}
            approved = answer['approved']
            return {'spec':state['pending_scope'] if approved else state['spec'],'pending_scope':None,
                'refs':[] if approved else state.get('refs',[]),'no_progress':0,'observed_rows':[] if approved else state.get('observed_rows',[]),
                'answers':[*state.get('answers',[]),'已确认具体范围变更' if approved else '拒绝扩大范围，请在原约定内完成'],
                'status':'decide','feedback':{},'message':''}
        return {"answers": [*state.get("answers", []), answer], "status": "decide", "feedback": {}, "message": ""}

    def backoff(state):
        interrupt({"kind": "backoff", "wait_id": state["op_id"], "seconds": 15 if state["feedback"].get("error_code") == "sql_unknown" else 1})
        return {"status": "tool"}

    def model_backoff(state):
        # 在已知未执行的拒绝回执之后等待；恢复新建操作，绝不重发未知调用。
        interrupt({"kind": "backoff", "wait_id": state["op_id"],
            "seconds": state["feedback"]["retry_after_seconds"], "message": state["feedback"]["message"]})
        return {"status": "decide"}

    graph = StateGraph(State)
    for name, fn in {"prepare_decision": prepare, "model_interval": model_interval, "decide": decision, "interpret": interpret,
                     "prepare_tool": prepare, "tool": execute_tool, "observe": observe,
                     "wait_for_answer": wait_for_answer, "backoff": backoff, "model_backoff": model_backoff}.items():
        graph.add_node(name, fn)
    graph.add_edge(START, "prepare_decision")
    graph.add_edge("prepare_decision", "model_interval")
    graph.add_edge("model_interval", "decide")
    graph.add_edge("decide", "interpret")
    graph.add_conditional_edges("interpret", lambda s: s["status"], {"tool": "prepare_tool", "decide": "prepare_decision", "model_backoff": "model_backoff", "failed": END})
    graph.add_edge("prepare_tool", "tool")
    graph.add_edge("tool", "observe")
    graph.add_conditional_edges("observe", lambda s: s["status"], {"decide": "prepare_decision", "waiting": "wait_for_answer", "backoff": "backoff", "failed": END, "completed": END})
    graph.add_conditional_edges('wait_for_answer',lambda s:s['status'],{'decide':'prepare_decision','failed':END})
    graph.add_edge("backoff", "prepare_tool")
    graph.add_edge("model_backoff", "prepare_decision")
    return graph.compile(checkpointer=saver)
