"""用独立文件统计核对真实只读查询；不调用模型，也不放宽查询超时。"""
import argparse
from collections import Counter
from decimal import Decimal
import json
import os
from pathlib import Path
from types import SimpleNamespace
import time

from fastapi import HTTPException
import psycopg
from psycopg.conninfo import conninfo_to_dict
from pydantic import TypeAdapter

from agent.runtime.app import Datasource
from agent.runtime.exploration import ExplorationAction,tool
from agent.runtime.push import DEFINITIONS,PushPlan,compile_push,execute_push
from agent.runtime.query_executor import execute_program
from agent.runtime.task_contracts import TaskContract,with_capabilities
from agent.tools.push_bundle import BundleReader


def classify_group_rejection(exc):
    """组数拒绝与执行截止分别记录；其他数据库故障不能当作保护通过。"""
    if exc.status_code==422 and '1000' in str(exc.detail):
        return {'reason':'row_limit','row_count_checked':True}
    cause=exc.__cause__
    deadline=(isinstance(cause,psycopg.errors.QueryCanceled) and 'statement timeout' in str(cause)) or isinstance(cause,TimeoutError)
    if exc.status_code==503 and deadline:
        return {'reason':'query_timeout','row_count_checked':False}
    raise exc


def verify(source, folder, report):
    folder=Path(folder)
    manifest=json.loads((folder/'validation.json').read_text(encoding='utf-8'))
    expected=json.loads((folder/'data/expected_large.json').read_text(encoding='utf-8'))
    if not manifest['passed'] or not expected['groups'] or 'day' not in expected['groups'][0]:
        raise ValueError('需要已完成且带每日独立核对值的样本')
    metrics=['sent','accepted','delivered','clicked','delivery_rate','click_rate']
    records=[]
    plans=[]
    periods=[('day','2026-09-15','2026-09-16'),('week','2026-09-08','2026-09-15'),
             ('month','2026-09-01','2026-10-01'),('all','2026-04-01','2026-10-01')]
    def run(name,plan):
        start=time.monotonic()
        result=execute_push(source,plan,list(DEFINITIONS),{'parser':'scale_verification','tokens':0})
        elapsed=time.monotonic()-start
        sums={}
        for row in expected['groups']:
            if not plan.time_range.start.date().isoformat()<=row['day']<plan.time_range.end_exclusive.date().isoformat():
                continue
            if any((row[f.field] in f.values)!=(f.op=='in') for f in plan.filters):
                continue
            key=tuple(row[d] for d in plan.dimensions)
            values=sums.setdefault(key,dict.fromkeys(['sent','accepted','delivered','clicked'],0))
            for metric in values:
                values[metric]+=row[metric]
        actual={tuple(r['dimensions'][d] for d in plan.dimensions):r['values'] for r in result['rows']}
        if set(actual)!=set(sums):
            raise ValueError(name+'：分组不一致')
        for key,values in sums.items():
            for metric,value in values.items():
                if Decimal(actual[key][metric]['value'])!=value:
                    raise ValueError(name+'：计数不一致 '+metric)
            for metric,numerator in [('delivery_rate','delivered'),('click_rate','clicked')]:
                calculated=Decimal(values[numerator])/values['sent']*100 if values['sent'] else None
                observed=actual[key][metric]['value']
                if (Decimal(observed) if observed is not None else None)!=calculated:
                    raise ValueError(name+'：比率不一致 '+metric)
        records.append({'case':name,'seconds':round(elapsed,4),'rows':len(result['rows']),'passed':True,
                        'plan':plan.model_dump(mode='json'),'sent':result['totals']['sent']['value']})
        print(json.dumps({k:v for k,v in records[-1].items() if k!='plan'},ensure_ascii=False),flush=True)
        plans.append((name,plan))
    for label,start,end in periods:
        for dimensions in [[],['channel'],['manufacturer'],['template_id']]:
            run(label+'_'+('-'.join(dimensions) or 'overall'),PushPlan(metrics=metrics,dimensions=dimensions,
                time_range={'start':start+'T00:00:00+08:00','end_exclusive':end+'T00:00:00+08:00'}))
    run('filtered_app_vendor',PushPlan(metrics=metrics,dimensions=['channel','template_id'],
        time_range={'start':'2026-09-01T00:00:00+08:00','end_exclusive':'2026-10-01T00:00:00+08:00'},
        filters=[{'field':'app_id','values':['app_1']},{'field':'manufacturer','values':['xiaomi']}]))
    device_plan=PushPlan(metrics=['devices','valid_devices','invalid_devices','unknown_devices','active_devices'],
        dimensions=['manufacturer'],as_of=manifest['data_cutoff'],
        time_range={'start':'2026-09-01T00:00:00+08:00','end_exclusive':'2026-10-01T00:00:00+08:00'})
    started=time.monotonic()
    devices=execute_push(source,device_plan,list(DEFINITIONS),{})
    device_elapsed=time.monotonic()-started
    if int(devices['totals']['devices']['value'])!=manifest['counts']['devices']:
        raise ValueError('设备总数不一致')
    statuses=Counter()
    physical=set()
    for row in BundleReader(folder/'data').records('devices'):
        statuses[row['push_status']]+=1
        physical.add(row['device_id'])
    active={(row['app_id'],row['device_id']) for row in BundleReader(folder/'data').records('device_activity')
            if '2026-09-01'<=row['occurred_at'][:10]<'2026-10-01'}
    if len(physical)!=manifest['counts']['devices'] or int(devices['totals']['active_devices']['value'])!=len(active):
        raise ValueError('物理设备或活跃设备数不一致')
    for status in ['valid','invalid','unknown']:
        if int(devices['totals'][status+'_devices']['value'])!=statuses[status]:
            raise ValueError('设备状态与快照历史归并不一致')
    records.append({'case':'device_stock_and_activity','seconds':round(device_elapsed,4),
                    'passed':True,'totals':devices['totals']})
    scope={'filters':[{'field':'app_id','values':['app_1']}],
           'periods':{'current':{'start':'2026-09-01T00:00:00+08:00','end_exclusive':'2026-10-01T00:00:00+08:00'}},'observation_hours':24}
    contract=TaskContract(task_type='query',question='查最低到达率通道，再查该通道每日厂商表现',scope=scope,
        result_requirements={'metrics':['sent','delivery_rate'],'dimensions':['manufacturer','day'],'period_names':['current'],
            'selection':{'field':'channel','rank_metric':'delivery_rate','direction':'asc','limit':1,'tie_breakers':[{'field':'channel','direction':'asc'}]}},
        execution_plan={'steps':[
            {'id':'A','op':'query','period':'current','metrics':['sent','delivery_rate'],'dimensions':['channel']},
            {'id':'B','op':'sort_limit','input':'A','metric':'delivery_rate','direction':'asc','limit':1,'tie_breakers':[{'field':'channel','direction':'asc'}]},
            {'id':'C','op':'query','period':'current','metrics':['sent','delivery_rate'],'dimensions':['manufacturer','day'],
             'filters_from':[{'field':'channel','step':'B','column':'channel'}]}],
            'outputs':[{'step':'C','metrics':['sent','delivery_rate'],'dimensions':['manufacturer','day']}]})
    program=execute_program(contract,source,list(DEFINITIONS))
    if program['status']!='completed' or len(program['steps'])!=3:
        raise ValueError('大样本明确取数依赖链未完成')
    analysis=with_capabilities(TaskContract(task_type='analysis',question='核实两个七天窗口的点击率变化',
        scope={**scope,'periods':{'baseline':{'start':'2026-09-01T00:00:00+08:00','end_exclusive':'2026-09-08T00:00:00+08:00'},
            'current':{'start':'2026-09-08T00:00:00+08:00','end_exclusive':'2026-09-15T00:00:00+08:00'}}},
        target_metrics=['click_rate'],answer_items=[{'id':'a1','text':'核实点击率变化'}]),list(DEFINITIONS))
    artifacts=[]
    def action(name,arguments):
        request=SimpleNamespace(spec=analysis,action=TypeAdapter(ExplorationAction).validate_python({'action':name,'arguments':arguments}),
            allowed_metrics=list(DEFINITIONS),binding='a'*64,artifacts=artifacts)
        artifact=tool(request,source)['artifact']
        artifacts.append(artifact)
        return artifact['result_ref']
    a,b=[action('query_metrics',{'period':p,'metrics':['click_rate']}) for p in ['baseline','current']]
    comparison=action('compare_periods',{'baseline_ref':a,'current_ref':b})
    action('finish',{'evidence_refs':[a,b,comparison],
        'answers':[{'item_id':'a1','status':'answered','text':'已取得同口径两期证据，业务原因仍待验证','evidence_refs':[a,b,comparison]}],
        'claims':[{'result_ref':comparison,'row':0,'metric':'click_rate','field':'delta'}]})
    if artifacts[-1]['data']['completeness']!='complete':
        raise ValueError('大样本开放分析工具协议未完成')
    rejection=None
    try:
        execute_push(source,PushPlan(metrics=['sent'],dimensions=['day','template_id'],
            time_range={'start':'2026-04-01T00:00:00+08:00','end_exclusive':'2026-10-01T00:00:00+08:00'}),list(DEFINITIONS),{})
    except HTTPException as exc:
        rejection=classify_group_rejection(exc)
    if rejection is None:
        raise ValueError('超量分组没有被拒绝')
    # 执行计划通过同一个只读账号取得，保留数据库原有 10 秒截止。
    explanations=[]
    result={'passed':False,'main_checks_passed':True,'model_calls':0,
        'mode':'real readonly SQL and deterministic tool scenarios',
        'queries':records,'max_seconds':max(r['seconds'] for r in records),'program_steps':program['steps'],
        'open_analysis_tool_report':artifacts[-1]['data'],'row_limit_guard':rejection['row_count_checked'],
        'large_group_request_rejected':True,'large_group_rejection':rejection,'query_explanations':explanations}
    # 附加诊断失败不能抹掉已经核对的数值、依赖步骤和报告。
    Path(report).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    with psycopg.connect(host=source.host,port=source.port,dbname=source.database,user=source.user,password=source.password,
        options=f'-c search_path={source.schema_name},pg_catalog -c statement_timeout=10000 -c default_transaction_read_only=on',autocommit=True) as conn:
        for name,plan in [plans[0],plans[12]]:
            statement,params=compile_push(plan)
            bound=statement.replace('%','%%').replace('?','%s')
            kind='actual'
            try:
                explanation=conn.execute('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+bound,params).fetchone()[0]
            except psycopg.errors.QueryCanceled as exc:
                if 'statement timeout' not in str(exc):
                    raise
                # 带统计的诊断增加执行开销；只降级诊断，不放宽真实取数截止。
                kind='estimated_after_diagnostic_timeout'
                explanation=conn.execute('EXPLAIN (FORMAT JSON) '+bound,params).fetchone()[0]
            explanations.append({'case':name,'kind':kind,'explain':explanation})
    result['passed']=True
    Path(report).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'phase':'query_verification_complete','queries':len(records),'max_seconds':result['max_seconds'],
        'program':'passed','open_tools':'passed','large_group_rejection':rejection}),flush=True)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--schema',required=True)
    parser.add_argument('--revision',required=True)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    params=conninfo_to_dict(os.environ['PUSH_VERIFY_DATABASE_URL'])
    manifest=json.loads((args.bundle/'validation.json').read_text(encoding='utf-8'))
    source=Datasource(driver='postgresql',domain='push',host=params['host'],port=int(params.get('port',5432)),
        database=params['dbname'],user=params['user'],password=params['password'],schema_name=args.schema,
        analysis_date='2026-10-14',data_start=manifest['data_start'],data_cutoff=manifest['data_cutoff'],
        dataset_revision=args.revision,is_synthetic=True)
    verify(source,args.bundle,args.report)


if __name__=='__main__':
    main()
