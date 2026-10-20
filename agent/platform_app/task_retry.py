"""手动重试创建独立任务；原回执、未知消耗及证据始终保留。"""
from fastapi import HTTPException
from psycopg.types.json import Jsonb
from uuid import uuid4

from agent.runtime import app as runtime
from agent.runtime.task_contracts import TaskContract, validate_contract, with_capabilities


def retry_info(conn, user, task):
    info = {'available': False, 'execution_version': task['execution_version']}
    source = runtime.sources().get(user['tenant_id']+':'+user['role'])
    if (task['status'] != 'failed' or task['user_id'] != user['id'] or not source
            or source.domain != 'push' or task['conversation_id'] or task.get('push_thread_id')):
        return info
    from .repository import binding
    if task['kind'] == 'analysis':
        run = conn.execute('SELECT binding,state FROM analysis_runs WHERE id=%s', (task['id'],)).fetchone()
        if run['binding'] != binding(user,True)['binding'] or (run['state'].get('spec') and run['state']['spec'].get('task_type') != 'analysis'):
            return info
        step = conn.execute("SELECT phase,output FROM analysis_steps WHERE run_id=%s ORDER BY sequence DESC LIMIT 1", (task['id'],)).fetchone()
        code = (step['output'] or {}).get('error_code', '') if step and step['phase'] == 'decide' else ''
        available = code.startswith('model_') or code == 'unknown_execution' or (
            code in {'invalid_arguments','missing_conditions','missing_evidence'} and
            bool((step['output'] or {}).get('model_sent')))
    else:
        if task.get('query_binding') != binding(user)['binding']:
            return info
        row = conn.execute('SELECT telemetry FROM task_attempts WHERE task_id=%s AND execution_version=%s',
                           (task['id'], task['execution_version'])).fetchone()
        trace = row['telemetry'] or {} if row else {}
        code = trace.get('error_code', '')
        # 旧版本未单独标记计划校验错误；已经调用模型但未接纳计划/执行 SQL，同样可手动重新解析。
        if not code and trace.get('model_calls') and not trace.get('sql_calls') and not task['contract_version']:
            code = 'model_plan_fields'
        available = code.startswith('model_') and not task['input'].get('push_plan') and not task['input'].get('query_contract')
    return {**info, 'available': available, 'error_code': code}


def create_retry(repo, conn, user, task_id, request_id, expected_version, policy):
    # 与任务接纳和补充保持同一锁顺序；同一次点击的网络重发只创建一个新任务。
    conn.execute('SELECT pg_advisory_xact_lock(710091)')
    task = repo.task(conn, user, task_id, lock=True)
    if task['user_id'] != user['id']:
        raise HTTPException(403, '只能重试自己的任务')
    old = conn.execute("SELECT data->>'task_id' task_id FROM task_events WHERE task_id=%s AND kind='manual_retry' AND data->>'request_id'=%s",
                       (task_id, request_id)).fetchone()
    if old:
        return repo.task(conn, user, old['task_id'])
    if task['execution_version'] != expected_version or not retry_info(conn, user, task)['available']:
        raise HTTPException(409, '任务已变化或不是可手动重试的模型失败，请刷新任务')
    if policy is None:
        raise HTTPException(503, '模型与预算尚未配置，暂不能重试')
    if task['kind'] == 'analysis':
        previous = conn.execute('SELECT state FROM analysis_runs WHERE id=%s', (task_id,)).fetchone()['state']
        saved = conn.execute('SELECT body FROM task_contracts WHERE task_id=%s AND version=%s',
                             (task_id, task['contract_version'])).fetchone()
        spec = None
        if saved:
            contract = with_capabilities(TaskContract.model_validate(saved['body']), user['allowed_metrics'])
            validate_contract(contract, runtime.sources()[user['tenant_id']+':'+user['role']], user['allowed_metrics'])
            spec = contract.model_dump(mode='json')
        # 重试幂等由原任务事件负责；内部新编号不与既有分析创建请求冲突。
        value = repo.create_analysis(conn, user, str(uuid4()), task['input']['question'], spec, policy)
        new_id = value['id']
        conn.execute("UPDATE analysis_runs SET state=jsonb_set(state,'{answers}',%s) WHERE id=%s",
                     (Jsonb(previous.get('answers', [])), new_id))
    else:
        repo.capacity(conn, user)
        new_id = repo.insert_task(conn, user, task['input'])
        conn.execute('UPDATE tasks SET intent_policy=%s,query_dialogue=%s,query_version=%s WHERE id=%s',
                     (Jsonb(policy.model_dump()), Jsonb(task['query_dialogue']), task['query_version'], new_id))
    repo.event(conn, task_id, 'manual_retry', {'request_id': request_id, 'task_id': new_id})
    return repo.task(conn, user, new_id)
