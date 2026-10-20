"""所有续跑共享调用次数和费用；解析预留独立于可能被覆盖的业务输出。"""
from .policy import Policy


def model_call_limit(conn, task_id):
    return 10 + conn.execute('SELECT coalesce(sum(additional_model_calls),0) n FROM task_resume_rounds WHERE task_id=%s',
                             (task_id,)).fetchone()['n']


def receipt_key(task):
    followup = bool(task['query_version'])
    table = 'query_followup_receipts' if followup else 'query_receipts'
    where = 'task_id=%s AND resume_round=%s' + (' AND version=%s' if followup else '')
    keys = (task['id'],task.get('resume_round',0)) + ((task['query_version'],) if followup else ())
    return table, where, keys


def initial_records(conn, task):
    policy = Policy.model_validate(task['intent_policy'])
    records = {}
    for row in conn.execute('SELECT model_usage,resume_round FROM query_receipts WHERE task_id=%s UNION ALL SELECT model_usage,resume_round FROM query_followup_receipts WHERE task_id=%s',
                            (task['id'],task['id'])):
        usage = row['model_usage']
        if usage:
            records[usage['execution_version']] = {**usage,'resume_round':row['resume_round']}
    # 升级前回执没有独立预留列，只接纳已有执行用量；缺失情况由恢复检查拒绝。
    for row in conn.execute('SELECT execution_version,telemetry,call_ms FROM task_attempts WHERE task_id=%s', (task['id'],)):
        trace = row['telemetry'] or {}
        version = row['execution_version']
        if version not in records and trace.get('model_calls') and trace.get('parser') != 'analysis':
            a,b = trace.get('input_tokens'),trace.get('output_tokens')
            known = trace.get('total_tokens') is not None and all(type(v) is int and 0<=v<=bound for v,bound in zip((a,b),(policy.max_input_bytes,policy.max_output_tokens)))
            records[version] = dict(execution_version=version,resume_round=0,model_calls=trace['model_calls'],
                tokens=a+b if known else policy.reserved_tokens, cost=policy.cost(a,b) if known else policy.reserved_cost,
                known=known,active_ms=row['call_ms'] or int(sum(trace.get('stages_ms',{}).values())))
    return sorted(records.values(),key=lambda x:x['execution_version'])


def usage_snapshot(conn, task, run=None):
    if run is not None:
        state = run['state']
        usage = conn.execute("SELECT coalesce(sum(actual_tokens),0) known_tokens,coalesce(sum(actual_cost),0) known_cost_micros,count(*) FILTER(WHERE status IN ('reserved','unknown')) unknown_calls FROM analysis_budget_ledger WHERE run_id=%s",(task['id'],)).fetchone()
        return {**{k:state.get(k,0) for k in ('model_calls','tokens','cost','sql_calls','actions','sequence','active_ms')},
                **{k:int(v) for k,v in usage.items()}}
    records = initial_records(conn,task) if task.get('intent_policy') else []
    return dict(model_calls=sum(r['model_calls'] for r in records),tokens=sum(r['tokens'] for r in records),
        cost=sum(r['cost'] for r in records),active_ms=sum(r.get('active_ms',0) for r in records),
        sql_calls=0,actions=0,sequence=len(records),known_tokens=sum(r['tokens'] for r in records if r['known']),
        known_cost_micros=sum(r['cost'] for r in records if r['known']),unknown_calls=sum(r['model_calls'] for r in records if not r['known']))


def other_budget_reason(usage, policy):
    if usage['tokens']+policy.reserved_tokens>policy.max_tokens:
        return '累计Token预算不足以预留下一次模型调用'
    if usage['cost']+policy.reserved_cost>policy.max_cost:
        return '累计费用预算不足以预留下一次模型调用'
    if usage['active_ms']+policy.model_timeout_seconds*1000>180000:
        return '剩余活跃执行时长不足以安全调用模型'
    if usage['sequence']>=40 or usage['actions']>=12:
        return '累计步骤或工具动作预算已耗尽'
    return ''
