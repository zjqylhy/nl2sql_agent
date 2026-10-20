"""在新 schema 发布推送快照；原始表不授权，设备状态先归并为互斥时间区间。"""
import argparse
import getpass
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import psycopg

FIELDS = {
    'apps': 'app_id text PRIMARY KEY,app_name text NOT NULL',
    'devices': 'app_id text NOT NULL,device_id text NOT NULL,manufacturer text NOT NULL,os text NOT NULL,registered_at timestamptz NOT NULL,PRIMARY KEY(app_id,device_id)',
    'device_state_history': 'app_id text NOT NULL,device_id text NOT NULL,valid_from timestamptz NOT NULL,valid_to timestamptz,notification_permission text NOT NULL,PRIMARY KEY(app_id,device_id,valid_from)',
    'device_channel_history': 'route_id text NOT NULL,app_id text NOT NULL,device_id text NOT NULL,channel text NOT NULL,token_status text NOT NULL,valid_from timestamptz NOT NULL,valid_to timestamptz,PRIMARY KEY(route_id,valid_from)',
    'device_activity': 'activity_id text PRIMARY KEY,app_id text NOT NULL,device_id text NOT NULL,event_type text NOT NULL,occurred_at timestamptz NOT NULL',
    'push_deliveries': 'delivery_id text PRIMARY KEY,message_id text NOT NULL,template_id text NOT NULL,title text NOT NULL,body text NOT NULL,app_id text NOT NULL,device_id text NOT NULL,manufacturer text NOT NULL,route_id text NOT NULL,channel text NOT NULL,sent_at timestamptz NOT NULL,accepted_at timestamptz,failure_reason text',
    'push_attempts': 'attempt_id text PRIMARY KEY,delivery_id text NOT NULL,attempt_index integer NOT NULL,sent_at timestamptz NOT NULL,outcome text NOT NULL',
    'push_events': 'receipt_id text PRIMARY KEY,event_id text NOT NULL,delivery_id text NOT NULL,app_id text NOT NULL,device_id text NOT NULL,event_type text NOT NULL,occurred_at timestamptz NOT NULL,received_at timestamptz NOT NULL',
}


def read_bundle(folder):
    """仅供小样本诊断与回归；正式导入始终逐行读取。"""
    from .push_bundle import BundleReader
    reader, rows = BundleReader(folder), {}
    for name in FIELDS:
        rows[name] = []
        for row in reader.records(name):
            if len(rows[name])>=100000:
                raise ValueError('read_bundle 仅供小样本诊断，请使用流式导入')
            rows[name].append(row)
    return rows, reader.digest.hexdigest()


def device_intervals(rows, cutoff):
    permissions, routes = defaultdict(list), defaultdict(list)
    for r in rows['device_state_history']:
        permissions[r['app_id'], r['device_id']].append(r)
    for r in rows['device_channel_history']:
        routes[r['app_id'], r['device_id']].append(r)
    result = []
    def contains(row, t):
        return datetime.fromisoformat(row['valid_from']) <= t and (row['valid_to'] is None or t < datetime.fromisoformat(row['valid_to']))
    for device in rows['devices']:
        key = device['app_id'], device['device_id']
        boundaries = {datetime.fromisoformat(device['registered_at'])}
        for r in permissions[key] + routes[key]:
            boundaries.add(datetime.fromisoformat(r['valid_from']))
            if r['valid_to']:
                boundaries.add(datetime.fromisoformat(r['valid_to']))
        boundaries = sorted(t for t in boundaries if t <= cutoff)
        for i, at in enumerate(boundaries):
            p = [r['notification_permission'] for r in permissions[key] if contains(r, at)]
            tokens = [r['token_status'] for r in routes[key] if contains(r, at)]
            route_ids = [r['route_id'] for r in routes[key] if contains(r, at)]
            if len(p) > 1 or len(set(route_ids)) != len(route_ids):
                raise ValueError('设备权限或通道状态区间重叠')
            permission = p[0] if p else 'unknown'
            status = 'invalid' if permission == 'denied' or tokens and all(t == 'invalid' for t in tokens) else 'valid' if permission == 'granted' and 'valid' in tokens else 'unknown'
            result.append((*key, at, boundaries[i+1] if i+1 < len(boundaries) else None, status))
    return result


def validate_bundle(rows, cutoff):
    keys = {(d['app_id'], d['device_id']) for d in rows['devices']}
    if len(keys) != len(rows['devices']):
        raise ValueError('应用设备主键重复')
    apps = {a['app_id'] for a in rows['apps']}
    deliveries = {d['delivery_id']: d for d in rows['push_deliveries']}
    events = {}
    for name, records in rows.items():
        for r in records:
            if r.get('app_id') and r['app_id'] not in apps:
                raise ValueError('应用外键不存在')
            if r.get('device_id') and (r['app_id'], r['device_id']) not in keys:
                raise ValueError('应用设备外键不存在')
            if r.get('delivery_id') and name != 'push_deliveries' and r['delivery_id'] not in deliveries:
                raise ValueError('推送外键不存在')
            for field in ['registered_at', 'valid_from', 'valid_to', 'occurred_at', 'received_at', 'sent_at', 'accepted_at']:
                if r.get(field):
                    t = datetime.fromisoformat(r[field])
                    if t.tzinfo is None or t > cutoff:
                        raise ValueError('事件或状态时间无时区或超出快照')
            if name in {'device_state_history', 'device_channel_history'} and r['valid_to'] and datetime.fromisoformat(r['valid_from']) >= datetime.fromisoformat(r['valid_to']):
                raise ValueError('历史时间区间无效')
            if name == 'push_events':
                d = deliveries[r['delivery_id']]
                signature = (r['delivery_id'], r['app_id'], r['device_id'], r['event_type'], r['occurred_at'])
                if r['event_id'] in events and events[r['event_id']] != signature:
                    raise ValueError('同一回执去重键内容冲突')
                events[r['event_id']] = signature
                if (r['app_id'], r['device_id']) != (d['app_id'], d['device_id']) or r['event_type'] not in {'delivered', 'clicked', 'expired'}:
                    raise ValueError('回执范围或类型不一致')
                if not d['accepted_at'] or not datetime.fromisoformat(d['accepted_at']) <= datetime.fromisoformat(r['occurred_at']) <= datetime.fromisoformat(r['received_at']):
                    raise ValueError('回执时间无效')
            if name == 'push_deliveries' and any(not isinstance(r.get(k), str) or not r[k].strip() or len(r[k]) > limit
                    for k, limit in [('template_id', 100), ('title', 200), ('body', 4000)]):
                raise ValueError('发送记录必须包含合法的模板 ID、标题与正文')
    if not rows['devices'] or not rows['push_deliveries']:
        raise ValueError('样本不能缺少设备或发送记录')
    return device_intervals(rows, cutoff)


def provision(conn, folder, schema, readonly_user, password, *, cutoff, is_synthetic=True, app_ids=(), create_role=True, report=None, progress=False):
    from .push_import_stream import provision_stream
    return provision_stream(conn,folder,schema,readonly_user,password,cutoff=cutoff,is_synthetic=is_synthetic,
                            app_ids=app_ids,create_role=create_role,report=report,progress=progress)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--schema', required=True)
    parser.add_argument('--readonly-user', required=True)
    parser.add_argument('--cutoff', default='2026-10-15T00:00:00+08:00')
    parser.add_argument('--app-ids', nargs='*', default=[])
    parser.add_argument('--report', type=Path, help='保存不含凭据的导入验证报告')
    parser.add_argument('--real-data', action='store_true', help='只用于显式导入真实数据，不改变已有快照')
    args = parser.parse_args()
    password = os.getenv('PUSH_READONLY_PASSWORD') or getpass.getpass('只读账号密码: ')
    with psycopg.connect(os.environ['IMPORT_DATABASE_URL'], autocommit=True) as conn:
        print(provision(conn, args.data_dir, args.schema, args.readonly_user, password,
                        cutoff=datetime.fromisoformat(args.cutoff), is_synthetic=not args.real_data, app_ids=args.app_ids, report=args.report, progress=True))


if __name__ == '__main__':
    main()
