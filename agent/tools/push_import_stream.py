"""流式 COPY 后在新快照内校验关联与归并回执；发布前失败整体回滚。"""
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import time

from psycopg import sql

from .push_bundle import BundleReader, peak_memory_bytes


def validate_record(name, record, cutoff, time_fields):
    for field in time_fields:
        if record.get(field) is not None:
            try:
                at=datetime.fromisoformat(record[field])
            except (ValueError,TypeError) as exc:
                raise ValueError(f'{name}.{field}：不是有效时间') from exc
            if at.tzinfo is None or at>cutoff:
                raise ValueError(f'{name}.{field}：事件或状态时间无时区或超出快照')
    if name in {'device_state_history','device_channel_history'}:
        if record.get('valid_to') is not None and datetime.fromisoformat(record['valid_from'])>=datetime.fromisoformat(record['valid_to']):
            raise ValueError('历史时间区间无效')
    if name=='push_deliveries':
        if any(not isinstance(record.get(k),str) or not record[k].strip() or len(record[k])>limit
               for k,limit in [('template_id',100),('title',200),('body',4000)]):
            raise ValueError('发送记录必须包含合法的模板 ID、标题与正文')
        if record.get('accepted_at') and datetime.fromisoformat(record['accepted_at'])<datetime.fromisoformat(record['sent_at']):
            raise ValueError('受理时间早于发送')


def check_database(cur):
    def check(query,message):
        cur.execute('SELECT EXISTS('+query+')')
        if cur.fetchone()[0]:
            raise ValueError(message)
    check('SELECT 1 FROM devices d LEFT JOIN apps a USING(app_id) WHERE a.app_id IS NULL','应用外键不存在')
    for table in ['device_state_history','device_channel_history','device_activity','push_deliveries','push_events']:
        check(f'SELECT 1 FROM {table} r LEFT JOIN devices d USING(app_id,device_id) WHERE d.device_id IS NULL','应用设备外键不存在：'+table)
    for table in ['push_attempts','push_events']:
        check(f'SELECT 1 FROM {table} r LEFT JOIN push_deliveries d USING(delivery_id) WHERE d.delivery_id IS NULL','推送外键不存在：'+table)
    check('''SELECT 1 FROM push_events r JOIN push_deliveries d USING(delivery_id)
      WHERE r.app_id<>d.app_id OR r.device_id<>d.device_id OR r.event_type NOT IN ('delivered','clicked','expired')
      OR d.accepted_at IS NULL OR r.occurred_at<d.accepted_at OR r.received_at<r.occurred_at''','回执范围、类型或时间不一致')
    check('''SELECT 1 FROM push_events GROUP BY event_id
      HAVING count(DISTINCT (delivery_id,app_id,device_id,event_type,occurred_at))>1''','同一回执去重键内容冲突')
    check('''SELECT 1 FROM push_attempts a JOIN push_deliveries d USING(delivery_id)
      WHERE a.attempt_index<1 OR a.sent_at<d.sent_at OR a.outcome NOT IN ('accepted','transient_failure','permanent_failure')''','发送尝试序号、时间或类型不合法')
    check('''SELECT 1 FROM device_activity a JOIN devices d USING(app_id,device_id)
      WHERE a.occurred_at<d.registered_at OR a.event_type<>'app_foreground' ''','活跃事件早于注册或类型不合法')
    check('''SELECT 1 FROM push_deliveries p JOIN devices d USING(app_id,device_id)
      WHERE p.sent_at<d.registered_at OR p.manufacturer<>d.manufacturer''','发送设备属性或时间不合法')
    for table,partition in [('device_state_history','app_id,device_id'),('device_channel_history','route_id')]:
        check(f'''SELECT 1 FROM (SELECT valid_from,lag(valid_to) OVER w prev_end,row_number() OVER w n
          FROM {table} WINDOW w AS(PARTITION BY {partition} ORDER BY valid_from)) q
          WHERE n>1 AND (prev_end IS NULL OR prev_end>valid_from)''','设备权限或通道状态区间重叠')
    check("SELECT 1 FROM device_state_history WHERE notification_permission NOT IN ('granted','denied','unknown')",'设备权限状态不合法')
    check("SELECT 1 FROM device_channel_history WHERE token_status NOT IN ('valid','invalid','unknown')",'通道状态不合法')
    check('''SELECT 1 FROM push_deliveries d WHERE NOT EXISTS(SELECT 1 FROM device_channel_history r
      WHERE r.route_id=d.route_id AND r.app_id=d.app_id AND r.device_id=d.device_id AND r.channel=d.channel
      AND r.valid_from<=d.sent_at AND (r.valid_to IS NULL OR d.sent_at<r.valid_to))''','发送引用的通道或状态区间不存在')


def create_device_states(cur, cutoff):
    # 一次推导所有状态变化边界；只在注册或状态变化时生成区间，不展开成每日快照。
    cur.execute('''CREATE TABLE device_states AS
      WITH boundaries AS (
        SELECT app_id,device_id,registered_at at FROM devices
        UNION SELECT app_id,device_id,valid_from FROM device_state_history
        UNION SELECT app_id,device_id,valid_to FROM device_state_history WHERE valid_to IS NOT NULL
        UNION SELECT app_id,device_id,valid_from FROM device_channel_history
        UNION SELECT app_id,device_id,valid_to FROM device_channel_history WHERE valid_to IS NOT NULL
      ), periods AS (
        SELECT app_id,device_id,at,lead(at) OVER(PARTITION BY app_id,device_id ORDER BY at) until
        FROM boundaries WHERE at<=%s
      ) SELECT b.app_id,b.device_id,b.at valid_from,b.until valid_to,
        CASE WHEN p.notification_permission='denied' OR
            count(r.route_id)>0 AND bool_and(r.token_status='invalid') THEN 'invalid'
          WHEN p.notification_permission='granted' AND bool_or(r.token_status='valid') THEN 'valid'
          ELSE 'unknown' END push_status
        FROM periods b
        LEFT JOIN device_state_history p ON p.app_id=b.app_id AND p.device_id=b.device_id
          AND p.valid_from<=b.at AND (p.valid_to IS NULL OR b.at<p.valid_to)
        LEFT JOIN device_channel_history r ON r.app_id=b.app_id AND r.device_id=b.device_id
          AND r.valid_from<=b.at AND (r.valid_to IS NULL OR b.at<r.valid_to)
        GROUP BY b.app_id,b.device_id,b.at,b.until,p.notification_permission''',(cutoff,))
    cur.execute('ALTER TABLE device_states ADD PRIMARY KEY(app_id,device_id,valid_from)')


def create_metric_facts(cur):
    # 指标明细不复制标题正文；避免每次汇总都扫描并关联整张回执表。
    cur.execute('''CREATE TABLE push_delivery_metric_facts AS
      SELECT d.delivery_id,d.app_id,d.manufacturer,d.channel,d.template_id,d.failure_reason,
        d.sent_at,d.accepted_at,r.delivered_at,r.clicked_at,r.expired_at,
        (d.sent_at AT TIME ZONE 'Asia/Shanghai')::date AS sent_day
      FROM push_deliveries d LEFT JOIN push_receipt_summary r USING(delivery_id)
      ORDER BY d.sent_at,d.app_id''')
    cur.execute('ALTER TABLE push_delivery_metric_facts ADD PRIMARY KEY(delivery_id)')
    # 低内存实例可只读索引完成统计，减少全期查询的数据页读取。
    cur.execute('''CREATE INDEX ON push_delivery_metric_facts(sent_at,app_id)
      INCLUDE(delivery_id,manufacturer,channel,template_id,failure_reason,accepted_at,
        delivered_at,clicked_at,expired_at,sent_day)''')


def provision_stream(conn, folder, schema, readonly_user, password, *, cutoff, is_synthetic=True,
                     app_ids=(), create_role=True, report=None, progress=False):
    from .import_push_postgres import FIELDS
    if cutoff.tzinfo is None or any(not re.fullmatch(r'[a-z][a-z0-9_]{1,62}',n) for n in (schema,readonly_user)) or len(password)<16:
        raise ValueError('schema、账号、密码长度或快照时区不符合要求')
    folder=Path(folder)
    manifest_path=folder.parent/'validation.json'
    manifest=json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else None
    if manifest and manifest.get('passed') is not True:
        raise ValueError('样本构建尚未通过检查，不能导入')
    reader=BundleReader(folder)
    started=time.monotonic()
    phases=[]
    def phase(name,**values):
        entry={'phase':name,'elapsed_seconds':round(time.monotonic()-started,3),**values}
        phases.append(entry)
        if progress:
            print(json.dumps(entry,ensure_ascii=False),flush=True)
    with conn.transaction(),conn.cursor() as cur:
        cur.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        cur.execute(sql.SQL('SET LOCAL search_path={},pg_catalog').format(sql.Identifier(schema)))
        # 仅维护事务使用，避免哈希归并及建索引因默认小工作区反复落盘。
        cur.execute("SET LOCAL work_mem='64MB'")
        cur.execute("SET LOCAL maintenance_work_mem='256MB'")
        for name,ddl in FIELDS.items():
            cur.execute(sql.SQL('CREATE TABLE {} ({})').format(sql.Identifier(name),sql.SQL(ddl)))
            columns=re.findall(r'(?:^|,)([a-z_]+) (?:text|timestamptz|integer)',ddl)
            time_fields=re.findall(r'(?:^|,)([a-z_]+) timestamptz',ddl)
            count=0
            with cur.copy(sql.SQL('COPY {} ({}) FROM STDIN').format(sql.Identifier(name),sql.SQL(',').join(map(sql.Identifier,columns)))) as copy:
                for record in reader.records(name):
                    validate_record(name,record,cutoff,time_fields)
                    copy.write_row([record.get(c) for c in columns])
                    count+=1
                    if count%1000000==0:
                        phase('copy_progress',table=name,rows=count)
                        if shutil.disk_usage(folder).free<8*1024**3:
                            raise ValueError('工作盘剩余空间不足 8 GiB，停止并回滚导入')
            phase('copied',table=name,rows=count)
        if not reader.counts['devices'] or not reader.counts['push_deliveries']:
            raise ValueError('样本不能缺少设备或发送记录')
        if manifest and manifest.get('format_version')==2:
            if reader.counts!={n:manifest['files'][n]['rows'] for n in FIELDS} or reader.hashes!={n:manifest['files'][n]['sha256'] for n in FIELDS}:
                raise ValueError('导入内容与已验证样本的数量或摘要不一致')
        if app_ids:
            cur.execute('SELECT app_id FROM apps')
            if not set(app_ids)<={r[0] for r in cur.fetchall()}:
                raise ValueError('授权应用不存在')
        for name in FIELDS:
            cur.execute(sql.SQL('ANALYZE {}').format(sql.Identifier(name)))
        cur.execute('CREATE INDEX ON device_channel_history(app_id,device_id,valid_from)')
        cur.execute('CREATE INDEX ON push_events(delivery_id,event_type,occurred_at)')
        phase('validating')
        check_database(cur)
        phase('validated')
        create_device_states(cur,cutoff)
        phase('device_states_created')
        cur.execute('''CREATE TABLE push_receipt_summary AS SELECT delivery_id,
          min(occurred_at) FILTER(WHERE event_type='delivered') delivered_at,
          min(occurred_at) FILTER(WHERE event_type='clicked') clicked_at,
          min(occurred_at) FILTER(WHERE event_type='expired') expired_at
          FROM push_events GROUP BY delivery_id''')
        cur.execute('ALTER TABLE push_receipt_summary ADD PRIMARY KEY(delivery_id) INCLUDE(delivered_at,clicked_at,expired_at)')
        create_metric_facts(cur)
        phase('metric_facts_created')
        cur.execute('CREATE INDEX ON device_activity(occurred_at,app_id,device_id)')
        cur.execute('CREATE INDEX ON device_states(app_id,device_id,valid_from,valid_to)')
        restriction=sql.SQL(' WHERE app_id IN ({})').format(sql.SQL(',').join(map(sql.Literal,app_ids))) if app_ids else sql.SQL('')
        for name,base in [('metric_devices','devices'),('metric_device_states','device_states'),('metric_device_activity','device_activity')]:
            cur.execute(sql.SQL('CREATE VIEW {} WITH(security_barrier=true) AS SELECT * FROM {}{}').format(sql.Identifier(name),sql.Identifier(base),restriction))
        cur.execute(sql.SQL('''CREATE VIEW metric_push_deliveries WITH(security_barrier=true) AS
          SELECT f.delivery_id,d.message_id,f.template_id,d.title,d.body,f.app_id,d.device_id,
            f.manufacturer,d.route_id,f.channel,f.sent_at,f.accepted_at,f.failure_reason,
            f.delivered_at,f.clicked_at,f.expired_at,f.sent_day
          FROM (SELECT * FROM push_delivery_metric_facts{}) f
          LEFT JOIN push_deliveries d USING(delivery_id)''').format(restriction))
        revision=reader.digest.hexdigest()
        cur.execute('CREATE TABLE dataset_release(revision text PRIMARY KEY,data_cutoff timestamptz NOT NULL,is_synthetic boolean NOT NULL)')
        cur.execute('INSERT INTO dataset_release VALUES(%s,%s,%s)',(revision,cutoff,is_synthetic))
        if create_role:
            cur.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT').format(sql.Identifier(readonly_user),sql.Literal(password)))
        cur.execute(sql.SQL('REVOKE ALL ON SCHEMA {} FROM PUBLIC').format(sql.Identifier(schema)))
        cur.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(readonly_user)))
        for name in ['metric_devices','metric_device_states','metric_device_activity','metric_push_deliveries']:
            cur.execute(sql.SQL('GRANT SELECT ON {} TO {}').format(sql.Identifier(name),sql.Identifier(readonly_user)))
        cur.execute(sql.SQL('ALTER ROLE {} SET default_transaction_read_only=on').format(sql.Identifier(readonly_user)))
        for name in [*FIELDS,'device_states','push_receipt_summary','push_delivery_metric_facts']:
            cur.execute(sql.SQL('ANALYZE {}').format(sql.Identifier(name)))
        cur.execute('SELECT coalesce(sum(pg_total_relation_size(c.oid)),0) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relkind IN (\'r\',\'m\')',(schema,))
        database_bytes=int(cur.fetchone()[0])
        phase('ready_to_publish',database_bytes=database_bytes)
    # 不可变快照在提交后标记可见性，索引查询才不必逐行回读宽表。
    # 调用者若包在外部事务内，维护需在其提交后单独执行，不能擅自提交调用者事务。
    from psycopg.pq import TransactionStatus
    vacuumed=conn.info.transaction_status==TransactionStatus.IDLE
    if vacuumed:
        previous_autocommit=conn.autocommit
        conn.autocommit=True
        try:
            with conn.cursor() as cur:
                for name in ['push_deliveries','push_receipt_summary','push_delivery_metric_facts','devices','device_states','device_activity']:
                    cur.execute(sql.SQL('VACUUM (ANALYZE) {}.{}').format(sql.Identifier(schema),sql.Identifier(name)))
        finally:
            conn.autocommit=previous_autocommit
        phase('visibility_maintained')
    result={'passed':vacuumed,'validation_passed':True,'pending_caller_commit':not vacuumed,
            'schema':schema,'revision':revision,'counts':reader.counts,'logical_sha256':reader.hashes,
            'database_bytes':database_bytes,'elapsed_seconds':round(time.monotonic()-started,3),
            'peak_memory_bytes':peak_memory_bytes(),'visibility_maintained':vacuumed,'phases':phases}
    if report:
        Path(report).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    phase('published' if vacuumed else 'awaiting_caller_commit',revision=revision,peak_memory_bytes=result['peak_memory_bytes'])
    return revision
