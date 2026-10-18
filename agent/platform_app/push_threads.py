"""不可变消息树；游标与分支头分离，旧位置继续时自动分叉。"""
import hashlib
import secrets
from decimal import Decimal

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from agent.runtime.analysis import canonical
from agent.runtime.push import PushPlan, required_metrics
from .repository import binding, permitted, public_task


def uid():
    return secrets.token_hex(24)


def answer_text(result):
    plan = result['plan']
    lines = [f"统计期间：{plan['time_range']['start']} 至 {plan['time_range']['end_exclusive']}（结束不含）。",
             f"结果包含 {len(result['rows'])} 个分组。"]
    for metric in result['metrics']:
        value = result.get('totals', {}).get(metric['id'], {}).get('value')
        display = f'{Decimal(value):,.2f}'.rstrip('0').rstrip('.') if value is not None else '无样本'
        lines.append(f"{metric['name']}：{display} {metric['unit']}。")
    if result.get('presentation', {}).get('detail', 'brief') == 'detailed':
        lines += [m['name']+'：'+m['definition'] for m in result['metrics']]
        lines += result.get('limitations', [])
    return '\n'.join(lines)


class Threads:
    def __init__(self, repo):
        self.repo = repo

    def authorize(self, conn, user, thread_id, lock=False):
        value = conn.execute('SELECT * FROM push_threads WHERE id=%s AND tenant_id=%s AND user_id=%s'+(' FOR UPDATE' if lock else ''),
                             (thread_id, user['tenant_id'], user['id'])).fetchone()
        if not value or value['role'] != user['role'] or value['binding'] != binding(user)['binding']:
            raise HTTPException(404, '会话不存在或授权/数据绑定已变化')
        plans = [r['plan'] for r in conn.execute('SELECT plan FROM push_revisions WHERE thread_id=%s', (thread_id,))]
        if not permitted(user, plans) or any(not set(required_metrics(PushPlan.model_validate(p))) <= set(user['allowed_metrics']) for p in plans):
            raise HTTPException(404, '会话指标权限已变化')
        return value

    def path(self, conn, thread_id, cursor):
        return conn.execute('''WITH RECURSIVE p AS (
            SELECT * FROM push_messages WHERE thread_id=%s AND id=%s
            UNION ALL SELECT m.* FROM push_messages m JOIN p ON m.id=p.parent_message_id WHERE m.thread_id=%s
        ) SELECT * FROM p ORDER BY seq''', (thread_id, cursor, thread_id)).fetchall()

    def append(self, conn, thread, role, kind, content, task_id):
        node = uid()
        conn.execute('INSERT INTO push_messages(id,thread_id,parent_message_id,role,kind,content,task_id) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                     (node, thread['id'], thread['cursor_id'], role, kind, Jsonb(content), task_id))
        thread['cursor_id'] = node
        return node

    def publish(self, conn, thread):
        conn.execute('UPDATE push_branches SET head_message_id=%s WHERE id=%s AND thread_id=%s',
                     (thread['cursor_id'], thread['active_branch_id'], thread['id']))
        conn.execute('UPDATE push_threads SET cursor_id=%s,active_branch_id=%s,active_task_id=%s,version=version+1 WHERE id=%s',
                     (thread['cursor_id'], thread['active_branch_id'], thread['active_task_id'], thread['id']))

    def get(self, conn, user, thread_id):
        value = self.authorize(conn, user, thread_id)
        nodes = conn.execute('SELECT m.*,r.plan,r.presentation FROM push_messages m LEFT JOIN push_revisions r ON r.message_id=m.id WHERE m.thread_id=%s ORDER BY seq', (thread_id,)).fetchall()
        path = self.path(conn, thread_id, value['cursor_id'])
        revisions = {n['id']: n for n in nodes if n.get('plan')}
        current = next((revisions[n['id']] for n in reversed(path) if n['id'] in revisions), None)
        result = None
        if current:
            row = conn.execute('SELECT t.result FROM push_revisions r JOIN tasks t ON t.id=r.result_task_id WHERE r.message_id=%s', (current['id'],)).fetchone()
            result = {**row['result'], 'presentation': current['presentation'], 'answer_text': current['content']['text'],
                      'context_compacted':current['content'].get('context_compacted',False)}
        task = None
        if value['active_task_id']:
            task = public_task(self.repo.task(conn, user, value['active_task_id']))
        return {**{k: value[k] for k in ('id','version','active_branch_id','cursor_id','active_task_id')},
                'messages': nodes, 'path_ids': [n['id'] for n in path], 'result': result, 'task': task,
                'branches': conn.execute('SELECT * FROM push_branches WHERE thread_id=%s ORDER BY id', (thread_id,)).fetchall()}

    def adopt(self, conn, user, task_id):
        conn.execute('SELECT pg_advisory_xact_lock(710091)')
        task = self.repo.task(conn, user, task_id, lock=True)
        if task['user_id'] != user['id'] or task['kind'] != 'query' or task['status'] != 'completed' or not (task['result'] or {}).get('domain') == 'push':
            raise HTTPException(409, '请选择自己已完成的推送查询')
        if task['push_thread_id']:
            return self.get(conn, user, task['push_thread_id'])
        thread = dict(id=uid(), cursor_id=None, active_branch_id=uid(), active_task_id=None)
        conn.execute('INSERT INTO push_threads(id,tenant_id,user_id,role,binding,root_task_id) VALUES(%s,%s,%s,%s,%s,%s)',
                     (thread['id'],user['tenant_id'],user['id'],user['role'],binding(user)['binding'],task_id))
        self.append(conn, thread, 'user', 'text', {'text':task['input']['question']}, task_id)
        for turn in task['query_dialogue']:
            self.append(conn, thread, 'assistant', 'clarification', {'text':turn['question']}, task_id)
            self.append(conn, thread, 'user', 'text', {'text':turn['answer']}, task_id)
        self.completed(conn, thread, task, task['result'], None)
        conn.execute('INSERT INTO push_branches(id,thread_id,head_message_id,label) VALUES(%s,%s,%s,%s)',
                     (thread['active_branch_id'],thread['id'],thread['cursor_id'],'主分支'))
        self.publish(conn, thread)
        conn.execute('UPDATE tasks SET push_thread_id=%s WHERE id=%s', (thread['id'],task_id))
        return self.get(conn, user, thread['id'])

    def seek(self, conn, user, thread_id, expected_version, node_id):
        conn.execute('SELECT pg_advisory_xact_lock(710091)')
        thread = self.authorize(conn, user, thread_id, True)
        if thread['version'] != expected_version or thread['active_task_id']:
            raise HTTPException(409, '会话已变化；执行或等待补充时请先停止任务')
        node = conn.execute("SELECT id FROM push_messages WHERE thread_id=%s AND id=%s AND role='assistant' AND kind IN ('answer','failed','canceled')", (thread_id,node_id)).fetchone()
        if not node:
            raise HTTPException(404, '请选择本会话已完成或已停止的回答位置')
        # 切到其他分支头时选中该分支；回到历史中间节点仍保留原分支头。
        branch = conn.execute('SELECT id FROM push_branches WHERE thread_id=%s AND head_message_id=%s ORDER BY id LIMIT 1', (thread_id,node_id)).fetchone()
        conn.execute('UPDATE push_threads SET cursor_id=%s,active_branch_id=%s,version=version+1 WHERE id=%s',
                     (node_id, branch['id'] if branch else thread['active_branch_id'],thread_id))
        return self.get(conn,user,thread_id)

    def turn(self, conn, user, thread_id, body):
        conn.execute('SELECT pg_advisory_xact_lock(710091)')
        thread = self.authorize(conn,user,thread_id,True)
        digest = hashlib.sha256(canonical([body.question,body.expected_version]).encode()).hexdigest()
        old = conn.execute('SELECT * FROM push_requests WHERE thread_id=%s AND request_id=%s', (thread_id,body.request_id)).fetchone()
        if old:
            if old['body_hash'] != digest:
                raise HTTPException(409,'重复请求的内容或版本不同')
            return self.get(conn,user,thread_id)
        if thread['version'] != body.expected_version or thread['active_task_id']:
            raise HTTPException(409,'会话已变化或仍有任务，请刷新')
        count = conn.execute("SELECT count(*) n FROM push_messages WHERE thread_id=%s AND role='user'", (thread_id,)).fetchone()['n']
        if count >= 30:
            raise HTTPException(409,'会话已达30轮，请新建会话')
        self.repo.capacity(conn,user)
        path = self.path(conn,thread_id,thread['cursor_id'])
        base = conn.execute('SELECT * FROM push_revisions WHERE message_id=ANY(%s) ORDER BY array_position(%s,message_id) DESC LIMIT 1',
                            ([n['id'] for n in path],[n['id'] for n in path])).fetchone()
        branch = conn.execute('SELECT * FROM push_branches WHERE id=%s', (thread['active_branch_id'],)).fetchone()
        if branch['head_message_id'] != thread['cursor_id']:
            branch_id = uid()
            conn.execute('INSERT INTO push_branches(id,thread_id,fork_message_id,head_message_id,label) VALUES(%s,%s,%s,%s,%s)',
                         (branch_id,thread_id,thread['cursor_id'],thread['cursor_id'],'分支 '+str(count+1)))
            thread['active_branch_id'] = branch_id
        task_id = self.repo.insert_task(conn,user,{'question':body.question})
        context = {'base_revision_id':base['message_id'], 'branch_id':thread['active_branch_id']}
        conn.execute('UPDATE tasks SET push_thread_id=%s,push_turn_context=%s WHERE id=%s', (thread_id,Jsonb(context),task_id))
        self.append(conn,thread,'user','text',{'text':body.question},task_id)
        thread['active_task_id'] = task_id
        self.publish(conn,thread)
        conn.execute('INSERT INTO push_requests VALUES(%s,%s,%s,%s)', (thread_id,body.request_id,digest,task_id))
        return self.get(conn,user,thread_id)

    def completed(self, conn, thread, task, result, parent):
        reuse = result.get('result_task_id', task['id'])
        call_id = self.append(conn,thread,'assistant','tool_request',{'name':'reuse_result' if reuse != task['id'] else 'query_metrics','arguments':{'plan':result['plan']}},task['id'])
        self.append(conn,thread,'tool','tool_result',{'tool_call_id':call_id,'result_task_id':reuse},task['id'])
        node = self.append(conn,thread,'assistant','answer',{'text':answer_text(result),'result_task_id':reuse,'context_compacted':result.get('context_compacted',False)},task['id'])
        conn.execute('INSERT INTO push_revisions VALUES(%s,%s,%s,%s,%s,%s)',
                     (node,thread['id'],parent,Jsonb(result['plan']),Jsonb(result.get('presentation',{'layout':'both','detail':'brief'})),reuse))

    def finish(self, conn, task, status, result, message):
        if not task.get('push_thread_id') or not task.get('push_turn_context'):
            return
        thread = conn.execute('SELECT * FROM push_threads WHERE id=%s FOR UPDATE', (task['push_thread_id'],)).fetchone()
        if thread['active_task_id'] != task['id']:
            raise HTTPException(409,'会话执行位置已变化')
        if status == 'completed':
            self.completed(conn,thread,task,result,result.get('base_revision_id',task['push_turn_context']['base_revision_id']))
        else:
            self.append(conn,thread,'assistant','clarification' if status == 'needs_clarification' else status,
                        {'text':(result or {}).get('message') or message},task['id'])
        if status != 'needs_clarification':
            thread['active_task_id'] = None
        self.publish(conn,thread)

    def answer(self, conn, user, task, answer):
        if not task.get('push_thread_id'):
            return
        thread = self.authorize(conn,user,task['push_thread_id'],True)
        if thread['active_task_id'] != task['id']:
            raise HTTPException(409,'会话已变化')
        self.append(conn,thread,'user','text',{'text':answer},task['id'])
        self.publish(conn,thread)
