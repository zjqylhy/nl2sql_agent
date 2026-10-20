"""在服务器原地核实旧任务并补齐恢复摘要；默认只检查，不调用模型或业务SQL。"""
import argparse
import json
import os

import psycopg
from fastapi import HTTPException
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent.platform_app.database import Database
from agent.platform_app.graph import State
from agent.platform_app.repository import Repository
from agent.platform_app.task_recovery import analysis_recovery
from agent.runtime import app as runtime


def prepare(repo,task_id,*,apply=False):
    # 与接纳续跑相同的锁顺序；存档只读，失败任务的旧绑定和费用不改写。
    with repo.db.transaction() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(710091)')
        task=conn.execute('SELECT * FROM tasks WHERE id=%s FOR UPDATE',(task_id,)).fetchone()
        if not task or task['status']!='failed' or task['kind']!='analysis':
            raise HTTPException(409,'仅可检查已失败的独立分析任务')
        user=repo.user(conn,task['user_id'],task['tenant_id'])
        repo.task(conn,user,task_id)
        run=conn.execute('SELECT * FROM analysis_runs WHERE id=%s FOR UPDATE',(task_id,)).fetchone()
        dsn=os.getenv('CHECKPOINT_DATABASE_URL',repo.db.dsn)
        with psycopg.connect(dsn,autocommit=True,row_factory=dict_row,connect_timeout=5,
                            options='-c search_path=agent_checkpoint,pg_catalog -c statement_timeout=5000') as native:
            saved=PostgresSaver(native).get_tuple({'configurable':{'thread_id':task_id}})
        if not saved:
            raise HTTPException(409,'原生流程存档已丢失，不能迁移')
        run['recovery_checkpoint']={'checkpoint_id':saved.checkpoint['id'],
                                   'state':{k:v for k,v in saved.checkpoint['channel_values'].items() if k in State.__annotations__}}
        recovery=analysis_recovery(repo,conn,user,task,run,runtime.sources()[user['tenant_id']+':'+user['role']])
        if apply:
            conn.execute('UPDATE analysis_runs SET recovery_checkpoint=%s WHERE id=%s',(Jsonb(run['recovery_checkpoint']),task_id))
        # 只返回技术校验结果；问题、证据和存档不离开服务器。
        return dict(valid=True,applied=apply,evidence_count=len(recovery['valid_result_refs']),
                    source_digest=run['metadata']['code_digest'],compatibility=recovery['compatibility'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-id',required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    db=Database()
    try:
        print(json.dumps(prepare(Repository(db),args.task_id,apply=args.apply),ensure_ascii=False))
    finally:
        db.close()


if __name__=='__main__':
    main()
