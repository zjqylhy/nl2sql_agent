"""明确取数的逐步回执；模型未知不重发，已保存的完整结果按版本复用。"""
from datetime import datetime, timezone
import hashlib

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from agent.runtime import app as runtime
from agent.runtime.analysis import canonical
from agent.runtime.query_program import ProgramError
from agent.runtime.task_contracts import TaskContract
from .task_contracts import save_contract


class QueryExecution:
    def __init__(self, repo, task):
        self.repo, self.task = repo, task
        self.table = 'query_followup_receipts' if task['query_version'] else 'query_receipts'
        self.where = 'task_id=%s AND version=%s' if task['query_version'] else 'task_id=%s'
        self.keys = (task['id'],task['query_version']) if task['query_version'] else (task['id'],)

    def read(self, conn):
        receipt = conn.execute(f'SELECT output FROM {self.table} WHERE {self.where} FOR UPDATE', self.keys).fetchone()
        return (receipt['output'] or {}).get('program_state', {}) if receipt else {}

    def write(self, conn, state):
        conn.execute(f'UPDATE {self.table} SET output=%s WHERE {self.where}', (Jsonb({'program_state':state}),*self.keys))

    def restore(self):
        with self.repo.db.transaction() as conn:
            self.repo.execution(conn, self.task)
            state = self.read(conn)
            if not state:
                return None
            record = conn.execute('SELECT body FROM task_contracts WHERE task_id=%s AND version=%s',
                                  (self.task['id'],state['contract_version'])).fetchone()
            return TaskContract.model_validate(record['body'])

    def accept(self, contract, validated):
        with self.repo.db.transaction() as conn:
            self.repo.execution(conn, self.task)
            state = self.read(conn)
            if state:
                if state['plan_hash'] != validated.plan_hash:
                    raise ProgramError('plan_hash', '已保存计划不可在执行中替换')
                return
            version = save_contract(conn,self.repo,self.task,contract,validated.descriptions)
            self.write(conn,dict(plan_hash=validated.plan_hash,contract_version=version,steps={},sql_calls=0,
                                 started_at=datetime.now(timezone.utc).isoformat()))

    def authorize(self, contract, validated):
        with self.repo.db.transaction() as conn:
            user = self.repo.execution(conn,self.task)
            state = self.read(conn)
            current = conn.execute('SELECT contract_version FROM tasks WHERE id=%s',(self.task['id'],)).fetchone()
            if state['plan_hash'] != validated.plan_hash or state['contract_version'] != current['contract_version']:
                raise ProgramError('contract_version', '约定或执行计划已改变')
            source = runtime.sources().get(user['tenant_id']+':'+user['role'])
            if source is None:
                raise HTTPException(403,'数据源权限已撤销')
            if (datetime.now(timezone.utc)-datetime.fromisoformat(state['started_at'])).total_seconds() > 180:
                raise ProgramError('budget','明确取数执行时间已耗尽')
            return user['allowed_metrics']

    def load(self, key, plan_hash):
        with self.repo.db.transaction() as conn:
            self.repo.execution(conn,self.task)
            state = self.read(conn)
            step = state['steps'].get(key)
            if not step:
                return None
            if step['status'] != 'completed':
                raise ProgramError(key,'上次步骤未取得可靠回执，停止依赖链，不重发模型或拼接新旧结果')
            record = conn.execute('SELECT body FROM task_evidence WHERE task_id=%s AND contract_version=%s AND result_ref=%s',
                (self.task['id'],state['contract_version'],step['result_ref'])).fetchone()
            if not record or hashlib.sha256(canonical(record['body']).encode()).hexdigest() != step['result_ref']:
                raise ProgramError(key,'完整结果丢失或内容校验不一致')
            return record['body']

    def begin(self, key, plan_hash, sql):
        with self.repo.db.transaction() as conn:
            self.repo.execution(conn,self.task)
            state = self.read(conn)
            if sql and state['sql_calls'] >= 6:
                raise ProgramError(key,'SQL 调用额度已耗尽')
            state['sql_calls'] += int(sql)
            state['steps'][key] = {'status':'running'}
            self.write(conn,state)

    def save(self, key, plan_hash, data):
        ref = hashlib.sha256(canonical(data).encode()).hexdigest()
        with self.repo.db.transaction() as conn:
            user = self.repo.execution(conn,self.task)
            from .repository import permitted
            if not permitted(user,data):
                raise HTTPException(403,'步骤结果指标权限已撤销')
            state = self.read(conn)
            conn.execute('INSERT INTO task_evidence(task_id,contract_version,result_ref,body) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                         (self.task['id'],state['contract_version'],ref,Jsonb(data)))
            state['steps'][key] = {'status':'completed','result_ref':ref}
            self.write(conn,state)
            self.repo.event(conn,self.task['id'],'step',{'step_id':key,'status':'completed','result_ref':ref})

    def fail(self, key, plan_hash, message):
        with self.repo.db.transaction() as conn:
            self.repo.execution(conn,self.task)
            state = self.read(conn)
            if state:
                state['steps'][key] = {**state['steps'].get(key,{}),'status':'failed','message':message[:300]}
                self.write(conn,state)
