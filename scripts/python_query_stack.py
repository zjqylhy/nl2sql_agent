"""单步质量评测专用双租户 PG 夹具；启动真实 Python API/Worker。"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile

import bcrypt
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from agent.platform_app.database import migrate
from agent.runtime.catalog import catalog
from scripts.python_analysis_stack import uri_for

ROOT = Path(__file__).resolve().parents[1]
HIDDEN = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


@contextmanager
def stack(rows, agent_root, max_calls, *, live=False):
    from scripts.evaluate_agent import wait_health
    admin_dsn = os.environ['TEST_DATABASE_URL']
    prefix = secrets.token_hex(8)
    password, key = [secrets.token_hex(24) for _ in range(2)]
    name = 'test_query_' + prefix
    roles, processes, created = [], [], False
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
            created = True
        dsn = uri_for(admin_dsn, name)
        migrate(dsn)
        sources = {}
        params = conninfo_to_dict(admin_dsn)
        with psycopg.connect(dsn) as conn:
            conn.execute('CREATE SCHEMA analytics_fixture')
            conn.execute('CREATE TABLE analytics_fixture.facts(purchased_at timestamp,customer_state text,order_status text,item_amount numeric,delivery_days numeric,review_score numeric)')
            with conn.cursor() as cursor:
                cursor.executemany('INSERT INTO analytics_fixture.facts VALUES(%s,%s,%s,%s,%s,%s)', [(r['purchased_at'], r['state'], r['status'], r['amount'], r['delivery'], r['review']) for r in rows])
            for marker in ['ea', 'eb']:
                tenant, role, schema = marker+'_'+prefix, 'rq_'+marker+'_'+prefix, 'view_'+marker
                conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
                restriction = sql.SQL(" WHERE customer_state='SP'") if marker == 'eb' else sql.SQL('')
                conn.execute(sql.SQL('CREATE VIEW {}.metric_orders AS SELECT * FROM analytics_fixture.facts{}').format(sql.Identifier(schema), restriction))
                conn.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(role), sql.Literal(password)))
                roles.append(role)
                conn.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema), sql.Identifier(role)))
                conn.execute(sql.SQL('GRANT SELECT ON {}.metric_orders TO {}').format(sql.Identifier(schema), sql.Identifier(role)))
                conn.execute('INSERT INTO platform.tenants VALUES(%s,%s)', (tenant, tenant))
                for user_role in (['admin', 'ops'] if marker == 'ea' else ['admin']):
                    allowed = ['gmv'] if user_role == 'ops' else list(catalog().metrics)
                    conn.execute('INSERT INTO platform.users(id,tenant_id,username,password_hash,role,allowed_metrics) VALUES(%s,%s,%s,%s,%s,%s)', (secrets.token_hex(24), tenant, user_role, bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)), user_role, allowed))
                    sources[tenant+':'+user_role] = dict(driver='postgresql', host=params.get('host', '127.0.0.1'), port=int(params.get('port', 5432)), database=name, schema_name=schema, user=role, password=password, analysis_date='2018-08-31', dataset_revision=hashlib.sha256((ROOT/'evals/fixture.csv').read_bytes()).hexdigest())
            conn.execute('CREATE TABLE platform.evaluation_budget(id integer PRIMARY KEY,issued integer NOT NULL,maximum integer NOT NULL)')
            conn.execute('INSERT INTO platform.evaluation_budget VALUES(1,0,%s)', (max_calls,))
        with socket.socket() as available:
            available.bind(('127.0.0.1', 0))
            port = available.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        env = {**os.environ, 'DATABASE_URL': dsn, 'CHECKPOINT_DATABASE_URL': dsn, 'PYTHONPATH': str(ROOT),
            'EVAL_AGENT_ROOT': str(agent_root.resolve()), 'TENANT_DATASOURCES': json.dumps(sources),
            'PUBLIC_ORIGIN': base, 'COOKIE_SECURE': 'false', 'SERVICE_KEY': key, 'ANALYSIS_ENABLED': 'false'}
        if not live:
            env.pop('LLM_API_KEY', None)
        with tempfile.TemporaryFile() as log:
            for command in [('api', '--port', str(port)), ('worker',)]:
                process = subprocess.Popen([sys.executable, '-m', 'scripts.query_eval_runtime', *command], cwd=ROOT, env=env, stdout=log, stderr=log, **HIDDEN)
                processes.append(process)
                if command[0] == 'api':
                    wait_health(base, process)
            yield base, prefix, password, key
    finally:
        # 先终止所有发送者，再清理本次随机库和只读角色。
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if created:
            with psycopg.connect(admin_dsn, autocommit=True) as conn:
                conn.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
                for role in roles:
                    conn.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(role)))
