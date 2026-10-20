"""从原始样本独立逐行计算，核对扩展题单的 SQL 汇总与分组结果。"""
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
import json

from agent.tests.test_push import platform, bundle, push_platform
from agent.runtime.push import DEFINITIONS, DEVICE_METRICS, PushPlan, execute_push
from evals.push_queries import CASES, expected


def oracle(plan, data):
    spec=plan.model_dump(mode='json')
    begin,end=(datetime.fromisoformat(spec['time_range'][k]) for k in ('start','end_exclusive'))
    at=datetime.fromisoformat(spec['as_of']) if spec['as_of'] else end-timedelta(microseconds=1)
    device=bool(set(spec['metrics']) & DEVICE_METRICS)
    active={(r['app_id'],r['device_id']) for r in data['device_activity'] if begin<=datetime.fromisoformat(r['occurred_at'])<end}
    def contains(row):
        return datetime.fromisoformat(row['valid_from'])<=at and (not row['valid_to'] or at<datetime.fromisoformat(row['valid_to']))
    permissions=defaultdict(list); tokens=defaultdict(list)
    for row in data['device_state_history']:
        if contains(row):permissions[row['app_id'],row['device_id']].append(row['notification_permission'])
    for row in data['device_channel_history']:
        if contains(row):tokens[row['app_id'],row['device_id']].append(row['token_status'])
    expired={}
    for row in data['push_events']:
        if row['event_type']=='expired':expired[row['delivery_id']]=datetime.fromisoformat(row['occurred_at'])
    groups=defaultdict(list)
    for raw in data['devices' if device else 'push_deliveries']:
        row=dict(raw)
        if device:
            if datetime.fromisoformat(row['registered_at'])>at:continue
        else:
            sent=datetime.fromisoformat(row['sent_at'])
            if not begin<=sent<end:continue
            row.update(day=sent.strftime('%Y-%m-%d'),month=sent.strftime('%Y-%m'),failure_reason=row['failure_reason'] or 'none')
        if any((row[f['field']] in f['values']) != (f['op']=='in') for f in spec['filters']):continue
        groups[tuple(row[d] for d in spec['dimensions'])].append(row)
    if not spec['dimensions'] and not groups:groups[()]=[]
    def summarize(rows):
        counts=defaultdict(int)
        for row in rows:
            if device:
                key=row['app_id'],row['device_id']; ps=permissions[key]; ts=tokens[key]
                state='invalid' if 'denied' in ps or ts and all(t=='invalid' for t in ts) else 'valid' if 'granted' in ps and 'valid' in ts else 'unknown'
                counts['devices']+=1; counts[state+'_devices']+=1
                counts['active_devices']+=key in active
                counts['active_valid_devices']+=key in active and state=='valid'
            else:
                sent=datetime.fromisoformat(row['sent_at']); horizon=sent+timedelta(hours=spec['observation_hours'])
                arrived=bool(row['delivered_at']) and datetime.fromisoformat(row['delivered_at'])<=horizon
                clicked=bool(row['clicked_at']) and datetime.fromisoformat(row['clicked_at'])<=horizon
                fail=not row['accepted_at'] or row['delivery_id'] in expired and expired[row['delivery_id']]<=horizon
                counts['sent']+=1; counts['accepted']+=bool(row['accepted_at'])
                counts['delivered']+=arrived; counts['clicked']+=clicked; counts['delivered_clicked']+=arrived and clicked
                counts['failed']+=fail; counts['unknown_delivery']+=bool(row['accepted_at']) and not fail and not arrived and not clicked
                if arrived:counts['duration']+=int((datetime.fromisoformat(row['delivered_at'])-sent).total_seconds())
        ratios={'delivery_rate':('delivered','sent'),'click_rate':('clicked','sent'),
                'click_to_delivery_rate':('delivered_clicked','delivered'),'active_rate':('active_devices','devices')}
        values={}
        for metric in spec['metrics']:
            if metric in ratios:
                a,b=ratios[metric]; values[metric]=Decimal(counts[a])/counts[b]*100 if counts[b] else None
            elif metric=='delivery_seconds':
                values[metric]=Decimal(counts['duration'])/counts['delivered'] if counts['delivered'] else None
            else:values[metric]=Decimal(counts[metric])
        return values
    return {group:summarize(rows) for group,rows in groups.items()},summarize([row for rows in groups.values() for row in rows])


def test_expanded_query_matrix_against_raw_sample_oracle(push_platform,bundle):
    _,_,source=push_platform
    tables=['devices','device_state_history','device_channel_history','device_activity','push_deliveries','push_events']
    data={name:[json.loads(line) for line in (bundle/(name+'.jsonl')).read_text(encoding='utf-8').splitlines()] for name in tables}
    plans=[(c['id'],c['plan']) for c in CASES if c['status']=='completed']
    plans += [
        ('empty_app',expected(['sent','delivery_rate'],['manufacturer'],filters=[{'field':'app_id','op':'in','values':['app_missing']} ])),
        ('unknown_delivery',expected(['accepted','unknown_delivery','failed'],['app_id'])),
        ('no_groups',expected(['sent','delivery_rate'])),
        ('exclude_channel',expected(['clicked','click_rate'],['manufacturer'],hours=1,filters=[{'field':'channel','op':'not_in','values':['fcm']} ])),
        ('activity_17_days',expected(['devices','active_devices','active_rate'],['app_id','manufacturer'],start='2026-08-03',end='2026-08-20',as_of='2026-08-20T00:00:00+08:00')),
    ]
    for name,proposed in plans:
        plan=PushPlan.model_validate(proposed)
        actual=execute_push(source,plan,list(DEFINITIONS),{})
        expected_rows,totals=oracle(plan,data)
        def numeric(values):return {k:Decimal(v['value']) if v['value'] is not None else None for k,v in values.items()}
        rows={tuple(row['dimensions'][dim] for dim in plan.dimensions):numeric(row['values']) for row in actual['rows']}
        assert rows==expected_rows,name
        assert numeric(actual['totals'])==totals,name
