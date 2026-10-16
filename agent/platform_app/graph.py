"""单 Agent 的受限 ReAct 图；图控制循环，平台事务强制授权、回执和预算。"""
import hashlib
from typing import TypedDict

from langgraph.graph import START, END, StateGraph
from langgraph.types import interrupt

# 共享查询模块先完成初始化，再加载保留的内部工具适配器。
from agent.runtime import app as runtime
from agent.runtime.analysis import canonical
from agent.runtime.analysis_api import Snapshot, ToolRequest, checked, decide, decision_prompt, fixed_decide, tool
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


def build_graph(repo, task, saver):
    ledger = Ledger(repo, task)

    def snapshot(state):
        with repo.db.transaction() as conn:
            user = repo.execution(conn, task)
            run = conn.execute("SELECT * FROM analysis_runs WHERE id=%s", (task["id"],)).fetchone()
            artifacts = [r["body"] for r in conn.execute("SELECT body FROM analysis_artifacts WHERE run_id=%s ORDER BY created_at,result_ref", (task["id"],))]
        value = dict(**identity(user).model_dump(), binding=run["binding"], question=state["question"],
            spec=state.get("spec"), artifacts=artifacts, answers=state.get("answers", []), feedback=state.get("feedback", {}),
            max_output_tokens=run["policy"].get("max_output_tokens", 1024),
            model_timeout_seconds=run["policy"].get("model_timeout_seconds", 20))
        checked(Snapshot.model_validate(value))
        return value, run

    def prepare(state):
        seq = state.get("sequence", 0) + 1
        return {"sequence": seq, "op_id": hashlib.sha256(f"{task['id']}:{seq}".encode()).hexdigest()[:48]}

    def decision(state):
        request, run = snapshot(state)
        fixed = run["policy"]["strategy"] == "fixed" and state.get("spec") is not None
        phase = "fixed" if fixed else "decide"
        payload = {"spec": request["spec"], "answers": request["answers"], "feedback": request["feedback"], "artifact_refs": state.get("refs", [])}
        if not fixed:
            from datetime import date
            prompt, question = decision_prompt(Snapshot.model_validate(request), date.fromisoformat(run["metadata"]["analysis_date"]))
            payload.update(prompt=prompt, question=question, model=run["metadata"]["model"],
                max_output_tokens=request["max_output_tokens"], model_timeout_seconds=request["model_timeout_seconds"])
        output = ledger.call(state["op_id"], phase, payload, state,
            lambda: (fixed_decide if fixed else decide)(Snapshot.model_validate(request)))
        return {"output": output}

    def interpret(state):
        output = state["output"]
        if output.get("status") != "completed":
            return failure(state, output)
        value = output["decision"]
        with repo.db.transaction() as conn:
            strategy = conn.execute("SELECT policy->>'strategy' strategy FROM analysis_runs WHERE id=%s", (task["id"],)).fetchone()["strategy"]
        if strategy == "fixed" and value["spec"] is not None:
            # C 组从第一个工具起即按固定依赖执行，不采纳模型附带的工具选择。
            from agent.runtime.analysis import Spec
            from agent.runtime.analysis_fixed import next_action
            request, _ = snapshot({**state, "spec": value["spec"]})
            value = next_action(Spec.model_validate(value["spec"]), request["artifacts"])
        return {"spec": value["spec"], "decision": value["action"], "status": "tool", "retry_sql": False}

    def execute_tool(state):
        request, _ = snapshot(state)
        output = ledger.call(state["op_id"], "tool", state["decision"], state,
            lambda: tool(ToolRequest.model_validate({**request, "action": state["decision"]})))
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
            return {"status": "waiting", "message": output["message"]}
        if output.get("status") != "completed":
            if output.get("error_code") in {"sql_timeout", "sql_connection", "sql_unknown"} and state.get("sql_retries", 0) == 0:
                return {"sql_retries": 1, "retry_sql": True, "status": "backoff", "feedback": output}
            return failure(state, output)
        art = output["artifact"]
        refs = state.get("refs", [])
        no_progress = state.get("no_progress", 0) + 1 if art["result_ref"] in refs else 0
        if no_progress >= 2:
            return {"status": "failed", "message": "重复调用未产生新证据，已停止"}
        return {"refs": list(dict.fromkeys([*refs, art["result_ref"]])), "no_progress": no_progress,
            "status": "completed" if art["kind"] == "report" else "decide", "retry_sql": False,
            "feedback": {"status": "completed", "result_ref": art["result_ref"], "outcome": art["data"].get("outcome")}}

    def wait_for_answer(state):
        # 此节点只执行 interrupt 和纯状态变换，恢复从开头执行也没有外部副作用。
        answer = interrupt({"kind": "clarification", "message": state["message"], "wait_id": state["op_id"]})
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
    for name, fn in {"prepare_decision": prepare, "decide": decision, "interpret": interpret,
                     "prepare_tool": prepare, "tool": execute_tool, "observe": observe,
                     "wait_for_answer": wait_for_answer, "backoff": backoff, "model_backoff": model_backoff}.items():
        graph.add_node(name, fn)
    graph.add_edge(START, "prepare_decision")
    graph.add_edge("prepare_decision", "decide")
    graph.add_edge("decide", "interpret")
    graph.add_conditional_edges("interpret", lambda s: s["status"], {"tool": "prepare_tool", "decide": "prepare_decision", "model_backoff": "model_backoff", "failed": END})
    graph.add_edge("prepare_tool", "tool")
    graph.add_edge("tool", "observe")
    graph.add_conditional_edges("observe", lambda s: s["status"], {"decide": "prepare_decision", "waiting": "wait_for_answer", "backoff": "backoff", "failed": END, "completed": END})
    graph.add_edge("wait_for_answer", "prepare_decision")
    graph.add_edge("backoff", "prepare_tool")
    graph.add_edge("model_backoff", "prepare_decision")
    return graph.compile(checkpointer=saver)
