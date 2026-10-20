"""人工续跑追加到原任务；原证据、失败回执和累计费用不覆盖。"""
import hashlib

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from agent.runtime import app as runtime
from agent.runtime.analysis import canonical
from .policy import Policy
from .task_budget import model_call_limit, usage_snapshot, other_budget_reason, receipt_key
from .task_recovery import analysis_recovery, recoverable


def retry_info(conn, user, task, repo=None):
    info=dict(available=False,execution_version=task['execution_version'],current_round=task.get('resume_round',0))
    source=runtime.sources().get(user['tenant_id']+':'+user['role'])
    if (task['status']!='failed' or task['user_id']!=user['id'] or not source or source.domain!='push'
            or task['conversation_id'] or task.get('push_thread_id')):
        return info
    from .repository import binding
    run=None
    try:
        if task['kind']=='analysis':
            run=conn.execute('SELECT * FROM analysis_runs WHERE id=%s',(task['id'],)).fetchone()
            recovery=analysis_recovery(repo,conn,user,task,run,source)
            policy=Policy.model_validate(run['policy'])
        else:
            if task.get('query_binding')!=binding(user)['binding'] or not task.get('intent_policy'):
                raise HTTPException(409,'数据绑定或模型预算不可用')
            table,where,keys=receipt_key(task)
            row=conn.execute(f'SELECT * FROM {table} WHERE {where}',keys).fetchone()
            trace=conn.execute('SELECT telemetry FROM task_attempts WHERE task_id=%s AND execution_version=%s',
                               (task['id'],task['execution_version'])).fetchone()
            trace=(trace or {}).get('telemetry') or {}
            code=trace.get('error_code','')
            if not code and trace.get('model_calls') and not trace.get('sql_calls') and not task['contract_version']:
                code='model_plan_fields'
            if not code and row and row['status']=='running' and (row.get('model_usage') or {}).get('model_calls'):
                code='unknown_execution'
            if (not recoverable(code) or trace.get('sql_calls') or task['contract_version'] or (row and (row['output'] or {}).get('program_state'))
                    or task['input'].get('push_plan') or task['input'].get('query_contract')):
                raise HTTPException(409,'仅尚未接纳计划的模型解析失败可重试识别')
            if not row or not row.get('model_usage') and not trace.get('model_calls'):
                raise HTTPException(409,'缺少可靠的原调用预留和回执，不能续跑')
            recovery=dict(mode='initial_parse',error_code=code,contract_version=task['contract_version'],recovery_version=1)
            policy=Policy.model_validate(task['intent_policy'])
        usage=usage_snapshot(conn,task,run)
        limit=model_call_limit(conn,task['id'])
        reason=other_budget_reason(usage,policy)
        exhausted=usage['model_calls']>=limit
        return {**info,'available':not bool(reason),'message':reason,'reason_code':'other_budget' if reason else '',
                'error_code':recovery['error_code'],'requires_call_extension':exhausted,
                'max_additional_model_calls':10 if exhausted and not reason else 0,
                'remaining_budget':dict(model_calls=max(0,limit-usage['model_calls']),tokens=max(0,policy.max_tokens-usage['tokens']),
                    cost=max(0,policy.max_cost-usage['cost']),sql_calls=max(0,6-usage['sql_calls'])),
                'usage':usage,'recovery':recovery}
    except HTTPException as exc:
        return {**info,'message':str(exc.detail),'reason_code':'not_recoverable'}


def create_retry(repo, conn, user, task_id, request_id, expected_version, policy, additional_model_calls=0):
    conn.execute('SELECT pg_advisory_xact_lock(710091)')
    task=repo.task(conn,user,task_id,lock=True)
    if task['user_id']!=user['id']:
        raise HTTPException(403,'只能继续自己的任务')
    digest=hashlib.sha256(canonical([expected_version,additional_model_calls]).encode()).hexdigest()
    old=conn.execute('SELECT * FROM task_resume_rounds WHERE task_id=%s AND request_id=%s',(task_id,request_id)).fetchone()
    if old:
        if old['request_hash']!=digest:
            raise HTTPException(409,'相同请求标识对应不同的续跑内容')
        return task
    info=retry_info(conn,user,task,repo)
    if task['execution_version']!=expected_version or not info['available']:
        raise HTTPException(409,info.get('message') or '任务已变化或不可续跑，请刷新任务')
    if policy is None:
        raise HTTPException(503,'模型与预算尚未配置')
    if bool(additional_model_calls)!=info['requires_call_extension']:
        raise HTTPException(409,'模型次数已耗尽，请明确确认追加次数' if not additional_model_calls else '原次数仍有剩余，不能提前追加，请刷新任务')
    repo.capacity(conn,user)
    round_no=task.get('resume_round',0)+1
    conn.execute('INSERT INTO task_resume_rounds(task_id,round_no,request_id,request_hash,source_execution_version,created_by,recovery,additional_model_calls,usage_before,previous_failure) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        (task_id,round_no,request_id,digest,expected_version,user['id'],Jsonb(info['recovery']),additional_model_calls,Jsonb(info['usage']),Jsonb({'message':task['error'],'result':task['result']})))
    conn.execute("UPDATE tasks SET status='pending',resume_round=%s,execution_version=execution_version+1,lease_until=NULL,ready_at=now(),error='',result=NULL,updated_at=now() WHERE id=%s",(round_no,task_id))
    conn.execute('UPDATE task_outbox SET delivered_at=NULL WHERE task_id=%s',(task_id,))
    repo.event(conn,task_id,'resumed',{'round_no':round_no,'additional_model_calls':additional_model_calls,'request_id':request_id})
    return repo.task(conn,user,task_id)
