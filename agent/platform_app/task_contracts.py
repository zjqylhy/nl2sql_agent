"""约定的唯一写入口；保存有效版本时核对任务身份和执行围栏。"""
import hashlib

from psycopg.types.json import Jsonb

from agent.runtime.analysis import canonical


def save_contract(conn, repo, task, contract, validation=None, confirmation_id=None):
    repo.execution(conn, task)
    current = conn.execute('SELECT contract_version FROM tasks WHERE id=%s FOR UPDATE', (task['id'],)).fetchone()
    body = contract.model_dump(mode='json')
    plan_hash = hashlib.sha256(canonical(body).encode()).hexdigest()
    version = current['contract_version']
    if version:
        old = conn.execute('SELECT * FROM task_contracts WHERE task_id=%s AND version=%s', (task['id'],version)).fetchone()
        if old['body'] == body:
            return version
    version += 1
    conn.execute("INSERT INTO task_contracts(task_id,tenant_id,user_id,version,status,body,plan_hash,validation,confirmation_request_id) VALUES(%s,%s,%s,%s,'confirmed',%s,%s,%s,%s)",
        (task['id'],task['tenant_id'],task['user_id'],version,Jsonb(body),plan_hash,Jsonb(validation or {}),confirmation_id or f'{version}:{plan_hash}'))
    conn.execute('UPDATE tasks SET contract_version=%s WHERE id=%s', (version,task['id']))
    return version
