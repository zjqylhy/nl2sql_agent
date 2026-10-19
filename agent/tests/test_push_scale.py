"""大样本路径用小规模验证流式协议，真实数量与性能另做专项运行。"""
from datetime import datetime,timedelta
from collections import Counter
import gzip
import json
import shutil

import psycopg
import pytest
from fastapi import HTTPException

from agent.tools.generate_push import generate
from agent.tools.import_push_postgres import provision,read_bundle
from agent.tools.push_bundle import BundleReader
from agent.tests.test_python_platform import platform


@pytest.fixture(scope='module')
def scaled_bundle(tmp_path_factory):
    root=tmp_path_factory.mktemp('scale_parent')/'bundle'
    generate(root,devices=90,deliveries=1000)
    return root


def test_compressed_records_hash_matches_uncompressed(scaled_bundle,tmp_path):
    for path in (scaled_bundle/'data').glob('*.jsonl.gz'):
        with gzip.open(path,'rb') as source,(tmp_path/path.stem).open('wb') as target:
            shutil.copyfileobj(source,target,length=65536)
    rows,compressed=read_bundle(scaled_bundle/'data')
    plain_rows,plain=read_bundle(tmp_path)
    assert compressed==plain and rows==plain_rows
    assert len(rows['devices'])==90 and len(rows['push_deliveries'])==1000
    assert len({r['device_id'] for r in rows['devices']})==90


def test_generation_reproducible_and_cannot_overwrite(scaled_bundle,tmp_path):
    target=tmp_path/'reproduction'
    generate(target,devices=90,deliveries=1000)
    original=json.loads((scaled_bundle/'validation.json').read_text())
    reproduced=json.loads((target/'validation.json').read_text())
    assert original['files']==reproduced['files']
    assert original['passed'] and reproduced['passed']
    with pytest.raises(ValueError,match='不能覆盖'):
        generate(target,devices=90,deliveries=1000)


def test_reader_rejects_ambiguous_or_oversized_input(tmp_path):
    (tmp_path/'apps.jsonl').write_text('{}\n')
    with gzip.open(tmp_path/'apps.jsonl.gz','wb') as stream:
        stream.write(b'{}\n')
    with pytest.raises(ValueError,match='只能存在一份'):
        list(BundleReader(tmp_path).records('apps'))
    (tmp_path/'apps.jsonl.gz').unlink()
    (tmp_path/'apps.jsonl').write_text('x'*65537)
    with pytest.raises(ValueError,match='64 KiB'):
        list(BundleReader(tmp_path).records('apps'))


def test_actual_postgres_row_1001_is_rejected(platform):
    from datetime import date
    from psycopg import sql
    from agent.runtime.push import DEFINITIONS,PushPlan,execute_push
    _,repo,source=platform
    with psycopg.connect(repo.db.dsn,autocommit=True) as conn:
        conn.execute('CREATE SCHEMA analytics_group_boundary')
        conn.execute('''CREATE VIEW analytics_group_boundary.metric_push_deliveries WITH(security_barrier=true) AS
          SELECT (at AT TIME ZONE 'Asia/Shanghai')::date sent_day,at+interval '12 hours' sent_at,'tpl_'||n::text template_id
          FROM generate_series('2026-04-01T00:00:00+08:00'::timestamptz,
            '2026-09-30T00:00:00+08:00'::timestamptz,interval '1 day') at
          CROSS JOIN generate_series(1,8) n''')
        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA analytics_group_boundary TO {}').format(sql.Identifier(source.user)))
        conn.execute(sql.SQL('GRANT SELECT ON analytics_group_boundary.metric_push_deliveries TO {}').format(sql.Identifier(source.user)))
        assert conn.execute('SELECT count(*) FROM analytics_group_boundary.metric_push_deliveries').fetchone()[0]>1000
    boundary=source.model_copy(update={'domain':'push','schema_name':'analytics_group_boundary',
        'analysis_date':date(2026,10,14),'data_start':datetime.fromisoformat('2026-04-01T00:00:00+08:00'),
        'data_cutoff':datetime.fromisoformat('2026-10-15T00:00:00+08:00')})
    p=PushPlan(metrics=['sent'],dimensions=['day','template_id'],time_range={
        'start':'2026-04-01T00:00:00+08:00','end_exclusive':'2026-10-01T00:00:00+08:00'})
    with pytest.raises(HTTPException) as error:
        execute_push(boundary,p,list(DEFINITIONS),{})
    assert error.value.status_code==422 and '1000' in error.value.detail


def test_verification_distinguishes_deadline_from_database_failure():
    from agent.tools.verify_push_scale import classify_group_rejection
    timeout=HTTPException(503,'query failed')
    timeout.__cause__=psycopg.errors.QueryCanceled('canceling statement due to statement timeout')
    assert classify_group_rejection(timeout)=={'reason':'query_timeout','row_count_checked':False}
    failure=HTTPException(503,'query failed')
    failure.__cause__=psycopg.OperationalError('connection failed')
    with pytest.raises(HTTPException):
        classify_group_rejection(failure)


def test_stream_import_summary_matches_independent_raw_counts(platform,scaled_bundle,tmp_path):
    _,repo,source=platform
    report=tmp_path/'import.json'
    with psycopg.connect(repo.db.dsn,autocommit=True) as conn:
        revision=provision(conn,scaled_bundle/'data','analytics_scaled',source.user,source.password,
            cutoff=datetime.fromisoformat('2026-10-15T00:00:00+08:00'),create_role=False,report=report)
        with conn.cursor() as cur:
            cur.execute('''SELECT count(*),count(*) FILTER(WHERE accepted_at IS NOT NULL),
                count(*) FILTER(WHERE delivered_at<=sent_at+interval '24 hours'),
                count(*) FILTER(WHERE clicked_at<=sent_at+interval '24 hours') FROM analytics_scaled.metric_push_deliveries''')
            actual=cur.fetchone()
            expected=json.loads((scaled_bundle/'data/expected_large.json').read_text())['totals']
            assert actual==tuple(expected.values())
            cur.execute('SELECT count(*) FROM analytics_scaled.device_states')
            assert cur.fetchone()[0]>=90
            # 优化视图必须保留全部列的精确明细，而不只是核对几个聚合数。
            cur.execute('''WITH original AS (
                SELECT d.*,r.delivered_at,r.clicked_at,r.expired_at,
                  (d.sent_at AT TIME ZONE 'Asia/Shanghai')::date AS sent_day
                FROM analytics_scaled.push_deliveries d
                LEFT JOIN analytics_scaled.push_receipt_summary r USING(delivery_id)
              ), difference AS (
                (SELECT * FROM original EXCEPT ALL SELECT * FROM analytics_scaled.metric_push_deliveries)
                UNION ALL
                (SELECT * FROM analytics_scaled.metric_push_deliveries EXCEPT ALL SELECT * FROM original)
              ) SELECT count(*) FROM difference''')
            assert cur.fetchone()[0]==0
            from agent.runtime.push import PushPlan,compile_push
            daily=PushPlan(metrics=['sent'],dimensions=['day'],time_range={
                'start':'2026-04-01T00:00:00+08:00','end_exclusive':'2026-10-01T00:00:00+08:00'})
            statement,params=compile_push(daily)
            cur.execute('SET search_path=analytics_scaled,pg_catalog')
            cur.execute(statement.replace('%','%%').replace('?','%s'),params)
            actual_daily={str(day):count for day,count in cur.fetchall()}
            expected_daily=Counter(r['sent_at'][:10] for r in BundleReader(scaled_bundle/'data').records('push_deliveries'))
            assert actual_daily==expected_daily
    result=json.loads(report.read_text())
    assert result['revision']==revision and result['counts']['push_deliveries']==1000
    assert result['visibility_maintained'] and not result['pending_caller_commit']
    assert result['peak_memory_bytes']>0
    with psycopg.connect(dbname=source.database,host=source.host,port=source.port,user=source.user,password=source.password) as conn:
        assert conn.execute('SELECT count(*) FROM analytics_scaled.metric_push_deliveries').fetchone()[0]==1000
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute('SELECT * FROM analytics_scaled.push_receipt_summary LIMIT 1')
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute('SELECT * FROM analytics_scaled.push_delivery_metric_facts LIMIT 1')


def test_incomplete_or_changed_manifest_never_publishes(platform,scaled_bundle,tmp_path):
    _,repo,source=platform
    target=tmp_path/'copy'
    shutil.copytree(scaled_bundle,target)
    manifest=json.loads((target/'validation.json').read_text())
    manifest['passed']=False
    (target/'validation.json').write_text(json.dumps(manifest))
    with psycopg.connect(repo.db.dsn,autocommit=True) as conn:
        with pytest.raises(ValueError,match='尚未通过'):
            provision(conn,target/'data','analytics_incomplete',source.user,source.password,
                cutoff=datetime.fromisoformat('2026-10-15T00:00:00+08:00'),create_role=False)
        manifest['passed']=True
        manifest['files']['push_events']['sha256']='0'*64
        (target/'validation.json').write_text(json.dumps(manifest))
        with pytest.raises(ValueError,match='数量或摘要不一致'):
            provision(conn,target/'data','analytics_changed',source.user,source.password,
                cutoff=datetime.fromisoformat('2026-10-15T00:00:00+08:00'),create_role=False)
        assert conn.execute("SELECT count(*) FROM pg_namespace WHERE nspname IN ('analytics_incomplete','analytics_changed')").fetchone()[0]==0


@pytest.mark.parametrize('problem',['orphan','conflict','overlap','timezone','title'])
def test_stream_checks_reject_invalid_external_bundle_and_rollback(platform,scaled_bundle,tmp_path,problem):
    _,repo,source=platform
    root=tmp_path/'external'
    shutil.copytree(scaled_bundle,root)
    # 外部输入也必须过完整校验；移除生成器清单以真正检验数据规则，而不只检验文件摘要。
    (root/'validation.json').unlink()
    table={'overlap':'device_state_history','title':'push_deliveries'}.get(problem,'push_events')
    path=root/'data'/(table+'.jsonl.gz')
    with gzip.open(path,'rt',encoding='utf-8') as stream:
        rows=[json.loads(line) for line in stream]
    if problem=='orphan':
        rows[0]['delivery_id']='dlv_999999999'
    elif problem=='conflict':
        rows.append({**rows[0],'receipt_id':'conflicting_receipt',
            'occurred_at':(datetime.fromisoformat(rows[0]['occurred_at'])+timedelta(seconds=1)).isoformat()})
    elif problem=='overlap':
        rows.append({**rows[0],'valid_from':(datetime.fromisoformat(rows[0]['valid_from'])+timedelta(seconds=1)).isoformat()})
    elif problem=='timezone':
        rows[0]['received_at']=rows[0]['received_at'][:-6]
    else:
        rows[0]['title']='x'*201
    with gzip.open(path,'wt',encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps(row)+'\n')
    with psycopg.connect(repo.db.dsn,autocommit=True) as conn:
        with pytest.raises(ValueError):
            provision(conn,root/'data','analytics_invalid',source.user,source.password,
                cutoff=datetime.fromisoformat('2026-10-15T00:00:00+08:00'),create_role=False)
        assert conn.execute("SELECT count(*) FROM pg_namespace WHERE nspname='analytics_invalid'").fetchone()[0]==0


def test_nested_import_never_commits_callers_transaction(platform,scaled_bundle,tmp_path):
    _,repo,source=platform
    report=tmp_path/'pending.json'
    with psycopg.connect(repo.db.dsn,autocommit=True) as conn:
        with pytest.raises(RuntimeError,match='caller rollback'):
            with conn.transaction():
                provision(conn,scaled_bundle/'data','analytics_nested',source.user,source.password,
                    cutoff=datetime.fromisoformat('2026-10-15T00:00:00+08:00'),create_role=False,report=report)
                pending=json.loads(report.read_text(encoding='utf-8'))
                assert pending['validation_passed'] and pending['pending_caller_commit']
                assert not pending['passed'] and not pending['visibility_maintained']
                assert conn.execute('SELECT count(*) FROM analytics_nested.devices').fetchone()[0]==90
                raise RuntimeError('caller rollback')
        assert conn.execute("SELECT count(*) FROM pg_namespace WHERE nspname='analytics_nested'").fetchone()[0]==0
