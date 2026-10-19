"""生成独立评审样包；所有设备、通道表现和异常均为模拟，不接入业务系统。"""
import hashlib
import json
import random
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

def generate(output):
    ROOT = Path(output).resolve()
    DATA = ROOT / 'data'
    DATA.mkdir(parents=True, exist_ok=True)
    R = random.Random(20261018)
    TZ = timezone(timedelta(hours=8))
    START = datetime(2026, 4, 1, tzinfo=TZ)
    END = datetime(2026, 10, 1, tzinfo=TZ)
    CUTOFF = datetime(2026, 10, 15, tzinfo=TZ)
    TABLES = {}


    def iso(t):
        return t.isoformat(timespec='seconds') if t else None


    def parse(t):
        return datetime.fromisoformat(t)


    def write(name, rows):
        TABLES[name] = rows
        with (DATA / (name + '.jsonl')).open('w', encoding='utf-8') as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')


    apps = [{'app_id': f'app_{n}', 'app_name': name, 'is_synthetic': True}
            for n, name in enumerate(['示例资讯', '示例商城', '示例办公'], 1)]
    vendors = [('huawei', 'huawei_push'), ('xiaomi', 'xiaomi_push'),
               ('oppo', 'oppo_push'), ('vivo', 'vivo_push'), ('apple', 'apns')]
    devices, state_rows, route_rows, activities = [], [], [], []
    states, routes = {}, defaultdict(list)


    def state_at(key, at):
        return next(s for s in states[key] if parse(s['valid_from']) <= at and
                    (s['valid_to'] is None or at < parse(s['valid_to'])))


    def route_at(route_id, at):
        return next(s for s in routes[route_id] if parse(s['valid_from']) <= at and
                    (s['valid_to'] is None or at < parse(s['valid_to'])))


    def device_status(key, at):
        permission = state_at(key, at)['notification_permission']
        available = [route_at(x, at)['token_status'] for x in device_routes[key]]
        if permission == 'denied' or all(x == 'invalid' for x in available):
            return 'invalid'
        if permission == 'granted' and 'valid' in available:
            return 'valid'
        return 'unknown'


    device_routes = {}
    for ai, app in enumerate(apps):
        for i in range(200):
            # 相同设备 ID 可在不同应用注册，用来检验按应用去重。
            physical = (i + ai * 100) % 400
            device_id = f'dev_{physical:06d}'
            key = app['app_id'], device_id
            vendor, channel = vendors[physical % len(vendors)]
            registered = datetime(2026, 3, 1, tzinfo=TZ) + timedelta(days=R.randrange(120), hours=R.randrange(24))
            permission = R.choices(['granted', 'denied', 'unknown'], [88, 9, 3])[0]
            changes = [(registered, permission)]
            if R.random() < .30:
                at = max(registered + timedelta(days=15), START + timedelta(days=R.randrange(25, 175)))
                changes.append((at, 'denied' if permission == 'granted' else 'granted'))
            states[key] = []
            for j, (at, value) in enumerate(changes):
                record = {'app_id': key[0], 'device_id': key[1], 'valid_from': iso(at),
                          'valid_to': iso(changes[j+1][0]) if j+1 < len(changes) else None,
                          'notification_permission': value, 'is_synthetic': True}
                states[key].append(record)
                state_rows.append(record)
            channels = [channel] + (['fcm'] if vendor != 'apple' and R.random() < .25 else [])
            device_routes[key] = []
            for ci, ch in enumerate(channels):
                route_id = f'route_{ai}_{i}_{ci}'
                device_routes[key].append(route_id)
                token_status = R.choices(['valid', 'invalid', 'unknown'], [91, 6, 3])[0]
                updates = [(registered, token_status)]
                if R.random() < .20:
                    at = max(registered + timedelta(days=20), START + timedelta(days=R.randrange(30, 170)))
                    updates.append((at, 'invalid' if token_status == 'valid' else 'valid'))
                for j, (at, status) in enumerate(updates):
                    row = {'route_id': route_id, 'app_id': key[0], 'device_id': key[1], 'channel': ch,
                           'token_ref': f'synthetic_token_ref_{route_id}', 'token_status': status,
                           'invalid_reason': 'synthetic_token_expired' if status == 'invalid' else None,
                           'valid_from': iso(at), 'valid_to': iso(updates[j+1][0]) if j+1 < len(updates) else None,
                           'is_synthetic': True}
                    routes[route_id].append(row)
                    route_rows.append(row)
            activity_probability = R.uniform(.01, .65)
            at = max(registered, START).replace(hour=0, minute=0, second=0)
            last_active = None
            while at < CUTOFF:
                event_time = at + timedelta(seconds=R.randrange(86400))
                if event_time >= registered and R.random() < activity_probability:
                    last_active = event_time
                    activities.append({'activity_id': f'act_{len(activities)+1:07d}', 'app_id': key[0],
                                       'device_id': key[1], 'event_type': 'app_foreground',
                                       'occurred_at': iso(event_time), 'is_synthetic': True})
                at += timedelta(days=1)
            devices.append({'app_id': key[0], 'device_id': key[1], 'manufacturer': vendor,
                            'os': 'ios' if vendor == 'apple' else 'android', 'registered_at': iso(registered),
                            'last_active_at': iso(last_active), 'notification_permission': state_at(key, CUTOFF)['notification_permission'],
                            'push_status': device_status(key, CUTOFF), 'snapshot_at': iso(CUTOFF), 'is_synthetic': True})

    deliveries, attempts, events = [], [], []


    def event(delivery, kind, at):
        # event_id 是去重键，receipt_id 区分重复上报；采集顺序可以不同于发生顺序。
        key = f"{delivery['delivery_id']}:{kind}"
        row = {'receipt_id': f'rcpt_{len(events)+1:07d}', 'event_id': key, 'delivery_id': delivery['delivery_id'],
               'app_id': delivery['app_id'], 'device_id': delivery['device_id'], 'event_type': kind,
               'occurred_at': iso(at), 'received_at': iso(at + timedelta(seconds=R.randrange(1, 172800))),
               'is_synthetic': True}
        events.append(row)
        if R.random() < .035:
            events.append({**row, 'receipt_id': f'rcpt_{len(events)+1:07d}',
                           'received_at': iso(parse(row['received_at']) + timedelta(seconds=R.randrange(5, 500)))})


    for day in range((END-START).days):
        date = START + timedelta(days=day)
        for app in apps:
            candidates = [d for d in devices if d['app_id'] == app['app_id'] and parse(d['registered_at']) < date]
            for d in R.sample(candidates, min(40, len(candidates))):
                key = d['app_id'], d['device_id']
                sent = date + timedelta(hours=R.choice([8, 10, 12, 18, 20]), seconds=R.randrange(3600))
                rid = R.choice(device_routes[key])
                route = route_at(rid, sent)
                permission = state_at(key, sent)['notification_permission']
                blocked = permission == 'denied' or route['token_status'] == 'invalid'
                # 不额外消耗随机数，保证补充文案不会改变原有发送/回执样本。
                template_index = (day + int(d['device_id'].rsplit('_', 1)[-1])) % 3
                content = [('服务提醒', '你有一条新的服务进度，请打开应用查看。'),
                           ('活动通知', '本期模拟活动已开始，点击查看活动详情。'),
                           ('内容推荐', '为你准备了新的模拟内容，欢迎查看。')][template_index]
                row = {'delivery_id': f'dlv_{len(deliveries)+1:07d}', 'message_id': f"msg_{app['app_id']}_{date:%Y%m%d}",
                       'template_id': f'tpl_{template_index+1:02d}', 'title': content[0], 'body': content[1],
                       'app_id': key[0], 'device_id': key[1], 'manufacturer': d['manufacturer'],
                       'route_id': rid, 'channel': route['channel'], 'sent_at': iso(sent),
                       'accepted_at': None, 'delivered_at': None, 'clicked_at': None,
                       'final_status': 'send_failed', 'failure_reason': None, 'is_synthetic': True}
                for attempt_index in range(1, 4):
                    attempt_time = sent + timedelta(seconds=(attempt_index-1)*30)
                    outcome = 'permanent_failure' if blocked else ('transient_failure' if R.random() < .08 else 'accepted')
                    attempts.append({'attempt_id': f'att_{len(attempts)+1:07d}', 'delivery_id': row['delivery_id'],
                                     'attempt_index': attempt_index, 'sent_at': iso(attempt_time),
                                     'outcome': outcome, 'is_synthetic': True})
                    if outcome == 'permanent_failure':
                        row['failure_reason'] = 'permission_denied' if permission == 'denied' else 'invalid_token'
                        break
                    if outcome == 'accepted':
                        row['accepted_at'] = iso(attempt_time + timedelta(seconds=1))
                        row['final_status'] = 'accepted_unknown_delivery'
                        break
                if row['accepted_at'] is None and row['failure_reason'] is None:
                    row['failure_reason'] = 'transient_retry_exhausted'
                if row['accepted_at']:
                    accepted = parse(row['accepted_at'])
                    # 人工注入固定日期/通道的变化，仅供核查工具验证，不代表真实厂商表现。
                    scenario = d['app_id'] == 'app_1' and route['channel'] == 'xiaomi_push' and day >= 153
                    probability = .58 if scenario else .93
                    arrival = accepted + timedelta(seconds=int(R.expovariate(1/(90 if scenario else 8)))+1)
                    if R.random() < probability:
                        reported_delivery = R.random() >= .06
                        if reported_delivery:
                            row['delivered_at'] = iso(arrival)
                            row['final_status'] = 'delivered'
                            event(row, 'delivered', arrival)
                        if R.random() < .16:
                            click = arrival + timedelta(seconds=R.randrange(3, 172800))
                            row['clicked_at'] = iso(click)
                            row['final_status'] = 'clicked'
                            event(row, 'clicked', click)
                    elif R.random() < .5:
                        row['final_status'] = 'expired'
                        row['failure_reason'] = 'synthetic_expiry_reported'
                        event(row, 'expired', accepted + timedelta(hours=24))
                deliveries.append(row)

    write('apps', apps)
    write('devices', devices)
    write('device_state_history', state_rows)
    write('device_channel_history', route_rows)
    write('device_activity', sorted(activities, key=lambda x: x['occurred_at']))
    write('push_deliveries', deliveries)
    write('push_attempts', attempts)
    write('push_events', sorted(events, key=lambda x: x['received_at']))

    # 从导出的 JSONL 重新读入，验证主外键、生命周期、去重和自定义窗口。
    loaded = {n: [json.loads(line) for line in (DATA / (n+'.jsonl')).read_text(encoding='utf-8').splitlines()] for n in TABLES}
    keys = {(d['app_id'], d['device_id']) for d in loaded['devices']}
    assert len(keys) == len(devices)
    delivery_map = {x['delivery_id']: x for x in loaded['push_deliveries']}
    assert len(delivery_map) == len(deliveries)
    for table in ['device_state_history', 'device_channel_history', 'device_activity', 'push_deliveries', 'push_events']:
        assert all((x['app_id'], x['device_id']) in keys for x in loaded[table])
    for x in loaded['push_attempts'] + loaded['push_events']:
        assert x['delivery_id'] in delivery_map
    for x in loaded['push_deliveries']:
        assert START <= parse(x['sent_at']) < END
        for field in ['accepted_at', 'delivered_at', 'clicked_at']:
            assert x[field] is None or parse(x['sent_at']) <= parse(x[field]) < CUTOFF
        if x['delivered_at']:
            assert x['accepted_at'] and parse(x['accepted_at']) <= parse(x['delivered_at'])
        if x['clicked_at']:
            assert x['accepted_at'] and parse(x['accepted_at']) <= parse(x['clicked_at'])
            assert not x['delivered_at'] or parse(x['delivered_at']) <= parse(x['clicked_at'])
    for x in loaded['push_events']:
        assert parse(x['occurred_at']) <= parse(x['received_at']) < CUTOFF
        assert delivery_map[x['delivery_id']][x['event_type']+'_at'] == x['occurred_at'] if x['event_type'] != 'expired' else True
    for x in devices:
        key = x['app_id'], x['device_id']
        seen = [a['occurred_at'] for a in loaded['device_activity'] if (a['app_id'], a['device_id']) == key]
        assert x['last_active_at'] == (max(seen) if seen else None)
        assert x['push_status'] == device_status(key, CUTOFF)
    for history in [states, routes]:
        for rows in history.values():
            assert all(rows[j]['valid_to'] == rows[j+1]['valid_from'] for j in range(len(rows)-1))
    assert any(x['clicked_at'] and not x['delivered_at'] for x in deliveries)
    assert len(events) > len({x['event_id'] for x in events})
    assert len(attempts) > len(deliveries)

    expected = []
    for start, end, hours in [('2026-04-03', '2026-04-18', 24), ('2026-07-01', '2026-08-01', 72),
                              ('2026-09-10', '2026-09-24', 48), ('2026-04-01', '2026-10-01', 24)]:
        a, b = parse(start+'T00:00:00+08:00'), parse(end+'T00:00:00+08:00')
        cohort = [x for x in deliveries if a <= parse(x['sent_at']) < b]
        reached = [x for x in cohort if x['delivered_at'] and parse(x['delivered_at']) <= parse(x['sent_at'])+timedelta(hours=hours)]
        clicked = [x for x in cohort if x['clicked_at'] and parse(x['clicked_at']) <= parse(x['sent_at'])+timedelta(hours=hours)]
        active = {(x['app_id'], x['device_id']) for x in activities if a <= parse(x['occurred_at']) < b}
        registered = [x for x in devices if parse(x['registered_at']) < b]
        statuses = Counter(device_status((x['app_id'], x['device_id']), b-timedelta(seconds=1)) for x in registered)
        expected.append({'send_period': {'start': iso(a), 'end_exclusive': iso(b)}, 'observation_hours': hours,
                         'sent_count': len(cohort), 'accepted_count': sum(bool(x['accepted_at']) for x in cohort),
                         'reported_delivered_count': len(reached), 'clicked_count': len(clicked),
                         'active_app_devices': len(active), 'device_status_at_period_end': dict(statuses),
                         'delivery_rate_denominator': 'sent_count', 'click_rate_denominator': 'sent_count'})
    write('expected_results', expected)

    validation = {'passed': True, 'seed': 20261018, 'is_synthetic': True,
                  'counts': {n: len(rows) for n, rows in TABLES.items()},
                  'device_status_at_cutoff': dict(Counter(x['push_status'] for x in devices)),
                  'duplicate_event_receipts': len(events)-len({x['event_id'] for x in events}),
                  'click_with_missing_delivery_receipt': sum(bool(x['clicked_at']) and not x['delivered_at'] for x in deliveries),
                  'checks': ['unique_primary_keys', 'foreign_keys', 'timestamp_order', 'history_continuity',
                             'snapshot_history_agreement', 'activity_last_seen_agreement', 'custom_time_windows',
                             'duplicate_callback_cases', 'retry_cases', 'missing_delivery_receipt_cases'],
                  'sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(DATA.glob('*.jsonl'))}}
    (ROOT / 'validation.json').write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in validation.items() if k != 'sha256'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('data/push'))
    generate(parser.parse_args().output)
