"""统一入口在初始解析后分流；明确取数不增加一次单独的意图模型调用。"""
import hashlib
from uuid import uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from agent.runtime.analysis import canonical
from agent.runtime.task_contracts import TaskContract, validate_contract
from agent.runtime import app as runtime
from .policy import Policy
from .repository import binding, new_state
from .task_contracts import save_contract


def reserve_initial(conn,task):
    """初始识别也占同一模型预算；预留在外部请求之前保存。"""
    if not task.get('intent_policy') or task['input'].get('push_plan') or task['input'].get('query_contract'):
        return None
    policy = Policy.model_validate(task['intent_policy'])
    tokens = cost = calls = 0
    for row in conn.execute('SELECT telemetry FROM task_attempts WHERE task_id=%s',(task['id'],)):
        trace = row['telemetry'] or {}
        if not trace.get('model_calls'):
            continue
        a,b = trace.get('input_tokens'),trace.get('output_tokens')
        known = type(a) is int and type(b) is int and 0<=a<=6000 and 0<=b<=policy.max_output_tokens
        tokens += a+b if known else policy.reserved_tokens
        cost += policy.cost(a,b) if known else policy.reserved_cost
        calls += trace['model_calls']
    if calls>=10 or tokens+policy.reserved_tokens>policy.max_tokens or cost+policy.reserved_cost>policy.max_cost:
        raise HTTPException(422,'剩余模型预算不足以开始意图识别或条件补充')
    return {'reserved_tokens':policy.reserved_tokens,'reserved_cost':policy.reserved_cost,'model':policy.model}


def promote_analysis(repo, task, output):
    with repo.db.transaction() as conn:
        user = repo.execution(conn,task)
        current = conn.execute('SELECT * FROM tasks WHERE id=%s FOR UPDATE',(task['id'],)).fetchone()
        if not current['intent_policy']:
            raise HTTPException(503,'此问题需要开放分析，当前未启用模型分析与预算配置')
        policy = Policy.model_validate(current['intent_policy'])
        meta = binding(user,True)
        if meta['model'] != policy.model:
            raise HTTPException(409,'模型配置已变化，请新建任务')
        contract = TaskContract.model_validate(output['contract'])
        validate_contract(contract,runtime.sources()[user['tenant_id']+':'+user['role']],user['allowed_metrics'])
        state = new_state(contract.model_dump(mode='json'))
        state['answers'] = [x['answer'] for x in current['query_dialogue']]
        traces = conn.execute('SELECT execution_version,telemetry,call_ms FROM task_attempts WHERE task_id=%s ORDER BY execution_version',(task['id'],)).fetchall()
        records = []
        for row in traces:
            trace = row['telemetry'] or {}
            if not trace.get('model_calls'):
                continue
            a,b = trace.get('input_tokens'),trace.get('output_tokens')
            known = type(a) is int and type(b) is int and 0<=a<=6000 and 0<=b<=policy.max_output_tokens
            records.append((row,trace,a+b if known else None,policy.cost(a,b) if known else None))
            state['model_calls'] += trace['model_calls']
            state['tokens'] += a+b if known else policy.reserved_tokens
            state['cost'] += policy.cost(a,b) if known else policy.reserved_cost
            state['active_ms'] += row['call_ms'] or int(sum(trace.get('stages_ms',{}).values()))
        if state['model_calls']>=10 or state['tokens']>policy.max_tokens or state['cost']>policy.max_cost:
            raise HTTPException(422,'初始解析已用尽分析预算，不能继续调查')
        state['sequence'] = len(records)
        conn.execute("UPDATE tasks SET kind='analysis' WHERE id=%s",(task['id'],))
        conn.execute('INSERT INTO analysis_runs(id,tenant_id,user_id,role,binding,metadata,policy,state,required_metrics,request_id,request_hash) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (task['id'],task['tenant_id'],task['user_id'],user['role'],meta['binding'],Jsonb(meta),Jsonb(policy.model_dump()),Jsonb(state),contract.required_metrics(),str(uuid4()),hashlib.sha256(canonical([task['input']['question'],contract.model_dump(mode='json')]).encode()).hexdigest()))
        for sequence,(row,trace,tokens,cost) in enumerate(records,1):
            step = hashlib.sha256(f"{task['id']}:intent:{row['execution_version']}".encode()).hexdigest()[:48]
            conn.execute("INSERT INTO analysis_steps(id,tenant_id,run_id,sequence,action_key,phase,execution_version,input,output,status,ended_at) VALUES(%s,%s,%s,%s,%s,'decide',%s,%s,%s,'completed',now())",
                (step,task['tenant_id'],task['id'],sequence,step,row['execution_version'],Jsonb({'protocol':'initial-intent-and-plan'}),Jsonb({'model_sent':True,'status':'completed','decision_type':'analysis'})))
            conn.execute("INSERT INTO analysis_step_attempts(step_id,execution_version,status) VALUES(%s,%s,'completed')",(step,row['execution_version']))
            conn.execute('INSERT INTO analysis_budget_ledger(step_id,run_id,reserved_tokens,reserved_cost,actual_tokens,actual_cost,status) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                (step,task['id'],policy.reserved_tokens,policy.reserved_cost,tokens,cost,'settled' if tokens is not None else 'unknown'))
        # 身份和预算分流在同一事务内完成，恢复后从分析流程继续，不再解析意图。
        task['kind'] = 'analysis'
        save_contract(conn,repo,task,contract)
        repo.event(conn,task['id'],'step',{'task_type':'analysis','message':'识别为开放任务分析'})
