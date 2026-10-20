"""大样本按设备及投递流式写出；不会缓存千万条发送和回执。"""
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random
import shutil
import time

from .push_bundle import BundleReader, BundleWriter, peak_memory_bytes

TZ = timezone(timedelta(hours=8))
START = datetime(2026,4,1,tzinfo=TZ)
END = datetime(2026,10,1,tzinfo=TZ)
CUTOFF = datetime(2026,10,15,tzinfo=TZ)
BASE = datetime(2026,3,1,tzinfo=TZ)
VENDORS = [('huawei','huawei_push'),('xiaomi','xiaomi_push'),('oppo','oppo_push'),('vivo','vivo_push'),('apple','apns')]
NAMES = ['apps','devices','device_state_history','device_channel_history','device_activity',
         'push_deliveries','push_attempts','push_events']


def iso(seconds):
    return (BASE+timedelta(seconds=seconds)).isoformat(timespec='seconds') if seconds is not None else None


def seconds(value):
    return int((datetime.fromisoformat(value)-BASE).total_seconds())


@dataclass(slots=True)
class Route:
    route_id: str
    channel: str
    initial: str
    changed: int | None

    def status(self, at):
        return ('invalid' if self.initial=='valid' else 'valid') if self.changed is not None and at>=self.changed else self.initial


@dataclass(slots=True)
class Device:
    app_id: str
    device_id: str
    manufacturer: str
    registered: int
    permission: str
    changed: int | None
    routes: tuple

    def permission_at(self, at):
        return ('denied' if self.permission=='granted' else 'granted') if self.changed is not None and at>=self.changed else self.permission


def generate_large(output, *, devices, deliveries, seed=20261018):
    if type(devices) is not int or type(deliveries) is not int or devices<3 or deliveries<1:
        raise ValueError('设备数至少 3，发送数必须为正整数')
    root = Path(output).resolve()
    if root.exists():
        raise ValueError('大样本必须使用不存在的新输出目录，不能覆盖已有快照或失败构建')
    root.parent.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(root.parent).free < 8*1024**3:
        raise ValueError('工作盘需至少保留 8 GiB 可用空间')
    root.mkdir()
    data=root/'data'
    data.mkdir()
    started=time.monotonic()
    rng=random.Random(seed)
    start,end,cutoff=map(lambda d:int((d-BASE).total_seconds()),[START,END,CUTOFF])
    days=(END-START).days
    manifest={'format_version':2,'passed':False,'is_synthetic':True,'seed':seed,
              'requested_devices':devices,'requested_deliveries':deliveries,
              'data_start':START.isoformat(),'data_cutoff':CUTOFF.isoformat()}
    (root/'validation.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    counts={}
    with ExitStack() as stack:
        writers={}
        for name in NAMES:
            writers[name]=BundleWriter(data,name)
            stack.callback(writers[name].close)
        for i,name in enumerate(['示例资讯','示例商城','示例办公'],1):
            writers['apps'].write({'app_id':f'app_{i}','app_name':name,'is_synthetic':True})
        by_app=[[],[],[]]
        activity_id=0
        for i in range(devices):
            app_id=f'app_{i%3+1}'
            device_id=f'dev_{i:07d}'
            vendor,channel=VENDORS[i%5]
            registered=rng.randrange(120*86400)
            permission=rng.choices(['granted','denied','unknown'],[88,9,3])[0]
            changed=max(registered+15*86400,start+rng.randrange(25,175)*86400) if rng.random()<.30 else None
            changes=[(registered,permission)]
            if changed is not None:
                changes.append((changed,'denied' if permission=='granted' else 'granted'))
            for j,(at,value) in enumerate(changes):
                writers['device_state_history'].write({'app_id':app_id,'device_id':device_id,'valid_from':iso(at),
                    'valid_to':iso(changes[j+1][0]) if j+1<len(changes) else None,'notification_permission':value,'is_synthetic':True})
            routes=[]
            channels=[channel]+(['fcm'] if vendor!='apple' and rng.random()<.25 else [])
            for ci,ch in enumerate(channels):
                initial=rng.choices(['valid','invalid','unknown'],[91,6,3])[0]
                route_change=max(registered+20*86400,start+rng.randrange(30,170)*86400) if rng.random()<.20 else None
                route=Route(f'route_{i}_{ci}',ch,initial,route_change)
                routes.append(route)
                updates=[(registered,initial)]
                if route_change is not None:
                    updates.append((route_change,route.status(route_change)))
                for j,(at,value) in enumerate(updates):
                    writers['device_channel_history'].write({'route_id':route.route_id,'app_id':app_id,'device_id':device_id,
                        'channel':ch,'token_ref':'synthetic_'+route.route_id,'token_status':value,
                        'invalid_reason':'synthetic_token_expired' if value=='invalid' else None,
                        'valid_from':iso(at),'valid_to':iso(updates[j+1][0]) if j+1<len(updates) else None,'is_synthetic':True})
            device=Device(app_id,device_id,vendor,registered,permission,changed,tuple(routes))
            by_app[i%3].append(device)
            probability=rng.uniform(.01,.65)
            last=None
            for day in range(max(start,registered)//86400,cutoff//86400):
                if rng.random()<probability:
                    at=day*86400+rng.randrange(86400)
                    if at>=registered:
                        last=at
                        activity_id+=1
                        writers['device_activity'].write({'activity_id':f'act_{activity_id:09d}','app_id':app_id,'device_id':device_id,
                            'event_type':'app_foreground','occurred_at':iso(at),'is_synthetic':True})
            tokens=[route.status(cutoff) for route in routes]
            current_permission=device.permission_at(cutoff)
            status='invalid' if current_permission=='denied' or all(t=='invalid' for t in tokens) else 'valid' if current_permission=='granted' and 'valid' in tokens else 'unknown'
            writers['devices'].write({'app_id':app_id,'device_id':device_id,'manufacturer':vendor,'os':'ios' if vendor=='apple' else 'android',
                'registered_at':iso(registered),'last_active_at':iso(last),'notification_permission':current_permission,
                'push_status':status,'snapshot_at':iso(cutoff),'is_synthetic':True})
        print(json.dumps({'phase':'devices_generated','devices':devices,'activities':activity_id}),flush=True)
        for group in by_app:
            group.sort(key=lambda d:d.registered)
        eligible=[0,0,0]
        delivery_id=attempt_id=receipt_id=0
        duplicate_receipts=missing_delivery=0
        for day in range(days):
            date=start+day*86400
            for ai,group in enumerate(by_app):
                while eligible[ai]<len(group) and group[eligible[ai]].registered<date:
                    eligible[ai]+=1
                if not eligible[ai]:
                    raise ValueError('所选规模在发送起始日没有已注册设备，请使用更多设备或调整种子')
            daily=deliveries//days+int(day<deliveries%days)
            for n in range(daily):
                ai=n%3
                device=by_app[ai][rng.randrange(eligible[ai])]
                route=rng.choice(device.routes)
                sent=date+rng.choice([8,10,12,18,20])*3600+rng.randrange(3600)
                permission=device.permission_at(sent)
                blocked=permission=='denied' or route.status(sent)=='invalid'
                delivery_id+=1
                key=f'dlv_{delivery_id:09d}'
                template=(day+int(device.device_id[4:]))%30
                title=['服务提醒','活动通知','内容推荐'][template%3]
                body=['你有一条新的服务进度，请打开应用查看。','本期模拟活动已开始，点击查看活动详情。','为你准备了新的模拟内容，欢迎查看。'][template%3]
                row={'delivery_id':key,'message_id':f'msg_{delivery_id:09d}','template_id':f'tpl_{template+1:03d}',
                    'title':title+f'·示例{template+1}','body':body,'app_id':device.app_id,'device_id':device.device_id,
                    'manufacturer':device.manufacturer,'route_id':route.route_id,'channel':route.channel,'sent_at':iso(sent),
                    'accepted_at':None,'delivered_at':None,'clicked_at':None,'final_status':'send_failed','failure_reason':None,'is_synthetic':True}
                for attempt_index in range(1,4):
                    at=sent+(attempt_index-1)*30
                    outcome='permanent_failure' if blocked else 'transient_failure' if rng.random()<.08 else 'accepted'
                    attempt_id+=1
                    writers['push_attempts'].write({'attempt_id':f'att_{attempt_id:09d}','delivery_id':key,
                        'attempt_index':attempt_index,'sent_at':iso(at),'outcome':outcome,'is_synthetic':True})
                    if outcome=='permanent_failure':
                        row['failure_reason']='permission_denied' if permission=='denied' else 'invalid_token'
                        break
                    if outcome=='accepted':
                        row['accepted_at']=iso(at+1)
                        row['final_status']='accepted_unknown_delivery'
                        break
                event_times=[]
                if row['accepted_at']:
                    accepted=seconds(row['accepted_at'])
                    scenario=ai==0 and route.channel=='xiaomi_push' and day>=153
                    arrival=accepted+int(rng.expovariate(1/(90 if scenario else 8)))+1
                    if rng.random()<(.58 if scenario else .93):
                        if rng.random()>=.06:
                            row['delivered_at']=iso(arrival)
                            row['final_status']='delivered'
                            event_times.append(('delivered',arrival))
                        if rng.random()<.16:
                            click=arrival+rng.randrange(3,172800)
                            row['clicked_at']=iso(click)
                            row['final_status']='clicked'
                            event_times.append(('clicked',click))
                            missing_delivery+=int(row['delivered_at'] is None)
                    elif rng.random()<.5:
                        row['final_status']='expired'
                        row['failure_reason']='synthetic_expiry_reported'
                        event_times.append(('expired',accepted+86400))
                elif row['failure_reason'] is None:
                    row['failure_reason']='transient_retry_exhausted'
                writers['push_deliveries'].write(row)
                for kind,at in event_times:
                    received=at+rng.randrange(1,172800)
                    receipt_id+=1
                    receipt={'receipt_id':f'rcpt_{receipt_id:09d}','event_id':key+':'+kind,'delivery_id':key,
                        'app_id':device.app_id,'device_id':device.device_id,'event_type':kind,
                        'occurred_at':iso(at),'received_at':iso(received),'is_synthetic':True}
                    writers['push_events'].write(receipt)
                    if rng.random()<.035:
                        receipt_id+=1
                        duplicate_receipts+=1
                        writers['push_events'].write({**receipt,'receipt_id':f'rcpt_{receipt_id:09d}',
                            'received_at':iso(received+rng.randrange(5,500))})
            if day%14==0 or day==days-1:
                print(json.dumps({'phase':'deliveries_generated','day':day+1,'deliveries':delivery_id,
                    'elapsed_seconds':round(time.monotonic()-started,1),'peak_memory_bytes':peak_memory_bytes()}),flush=True)
                if shutil.disk_usage(root).free<8*1024**3:
                    raise ValueError('工作盘已接近预留的 8 GiB 余量，停止生成')
    # ExitStack 在正常结束和失败时都关闭全部文件；只有关闭成功才读取文件大小。
    counts={name:{'rows':writer.count,'sha256':writer.digest.hexdigest(),'bytes':writer.path.stat().st_size}
            for name,writer in writers.items()}
    del by_app, device, group
    manifest.update(files=counts,counts={n:v['rows'] for n,v in counts.items()},
        duplicate_event_receipts=duplicate_receipts,click_with_missing_delivery_receipt=missing_delivery)
    manifest['checks']=verify_large(data,manifest)
    manifest.update(passed=True,elapsed_seconds=round(time.monotonic()-started,3),peak_memory_bytes=peak_memory_bytes())
    (root/'validation.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'phase':'complete',**{k:v for k,v in manifest.items() if k not in {'files','checks'}}},ensure_ascii=False),flush=True)
    return manifest


def verify_large(folder, manifest):
    """独立重读导出文件，以顺序合并核对父子记录，不建立千万投递字典。"""
    reader=BundleReader(folder)
    apps={r['app_id'] for r in reader.records('apps')}
    devices={}
    for r in reader.records('devices'):
        key=r['app_id'],r['device_id']
        if key in devices or key[0] not in apps:
            raise ValueError('设备主键或应用关联不合法')
        devices[key]=(r['registered_at'],r['last_active_at'])
    for name in ['device_state_history','device_channel_history']:
        for r in reader.records(name):
            if (r['app_id'],r['device_id']) not in devices or r['valid_to'] and r['valid_to']<=r['valid_from']:
                raise ValueError('状态历史关联或时间不合法')
    last={}
    for r in reader.records('device_activity'):
        key=r['app_id'],r['device_id']
        if key not in devices or r['occurred_at']<devices[key][0] or r['occurred_at']>=CUTOFF.isoformat():
            raise ValueError('活跃事件关联或时间不合法')
        last[key]=max(last.get(key,''),r['occurred_at'])
    if any(value[1]!=last.get(key) for key,value in devices.items()):
        raise ValueError('最近活跃时间与事件不一致')
    children={n:reader.records(n) for n in ['push_attempts','push_events']}
    pending={n:next(it,None) for n,it in children.items()}
    totals={'sent':0,'accepted':0,'delivered':0,'clicked':0}
    grouped={}
    for index,r in enumerate(reader.records('push_deliveries'),1):
        if r['delivery_id']!=f'dlv_{index:09d}' or (r['app_id'],r['device_id']) not in devices:
            raise ValueError('发送主键或设备关联不合法')
        sent=datetime.fromisoformat(r['sent_at'])
        if not START<=sent<END or r['sent_at']<devices[r['app_id'],r['device_id']][0]:
            raise ValueError('发送早于注册或超出范围')
        for name in ['accepted_at','delivered_at','clicked_at']:
            if r[name] and not sent<=datetime.fromisoformat(r[name])<CUTOFF:
                raise ValueError('投递时间不合法')
        if any(not r[k] for k in ['template_id','title','body']):
            raise ValueError('模板字段不完整')
        horizon=sent+timedelta(hours=24)
        values=[1,int(r['accepted_at'] is not None),int(bool(r['delivered_at']) and datetime.fromisoformat(r['delivered_at'])<=horizon),
                int(bool(r['clicked_at']) and datetime.fromisoformat(r['clicked_at'])<=horizon)]
        for metric,value in zip(totals,values):
            totals[metric]+=value
        key=(r['sent_at'][:10],r['app_id'],r['manufacturer'],r['channel'],r['template_id'])
        bucket=grouped.setdefault(key,[0,0,0,0])
        for i,value in enumerate(values):
            bucket[i]+=value
        for name,it in children.items():
            signatures={}
            count=0
            while pending[name] is not None and pending[name]['delivery_id']==r['delivery_id']:
                child=pending[name]
                count+=1
                if name=='push_events':
                    signature=(child['event_type'],child['occurred_at'])
                    if child['event_id'] in signatures and signatures[child['event_id']]!=signature:
                        raise ValueError('重复回执内容冲突')
                    signatures[child['event_id']]=signature
                    if (child['app_id'],child['device_id'])!=(r['app_id'],r['device_id']) or not r['accepted_at'] or not r['accepted_at']<=child['occurred_at']<=child['received_at']<CUTOFF.isoformat():
                        raise ValueError('回执范围或时间不合法')
                    if child['event_type']!='expired' and r[child['event_type']+'_at']!=child['occurred_at']:
                        raise ValueError('回执与投递时间不一致')
                elif child['attempt_index']!=count or child['sent_at']<r['sent_at']:
                    raise ValueError('重试序号或时间不合法')
                pending[name]=next(it,None)
            if name=='push_attempts' and not count:
                raise ValueError('投递缺少发送尝试')
        if index%1000000==0:
            print(json.dumps({'phase':'verifying','deliveries':index}),flush=True)
    if any(r is not None for r in pending.values()):
        raise ValueError('存在孤儿或失序的发送尝试、回执')
    if reader.counts!={n:manifest['files'][n]['rows'] for n in NAMES} or reader.hashes!={n:manifest['files'][n]['sha256'] for n in NAMES}:
        raise ValueError('重读后的数量或文件摘要不一致')
    (Path(folder)/'expected_large.json').write_text(json.dumps({'observation_hours':24,'totals':totals,
        'groups':[{'day':k[0],'month':k[0][:7],'app_id':k[1],'manufacturer':k[2],'channel':k[3],'template_id':k[4],
                   **dict(zip(totals,v))} for k,v in sorted(grouped.items())]},ensure_ascii=False),encoding='utf-8')
    return ['streamed_primary_keys','device_foreign_keys','sequential_child_join','receipt_deduplication',
            'timestamp_order','activity_last_seen','template_fields','recomputed_counts_and_hashes','independent_24h_totals_and_groups']
