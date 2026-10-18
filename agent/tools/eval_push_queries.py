"""固定开发题单的真实解析验证；硬限请求数，不执行 SQL 或写任务。"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from agent.runtime import push_query, telemetry
from agent.runtime.app import QueryRequest
from agent.runtime.model_transport import ModelTransport
from agent.runtime.push import DEFINITIONS, DEVICE_METRICS
from evals.push_queries import CASES


def mismatches(expected, actual):
    if actual.get('status') != expected['status']:
        return ['status']
    if expected['status'] != 'completed':
        return []
    plan, want = actual['plan'], expected['plan']
    errors=[]
    for field in ('metrics','dimensions'):
        if set(plan[field]) != set(want[field]):
            errors.append(field)
    for field in ('start','end_exclusive'):
        if datetime.fromisoformat(plan['time_range'][field]) != datetime.fromisoformat(want['time_range'][field]):
            errors.append('time_range.'+field)
    if not set(want['metrics']) <= DEVICE_METRICS and plan['observation_hours'] != want['observation_hours']:
        errors.append('observation_hours')
    def normalized(filters):
        return sorted((f['field'],f.get('op','in'),tuple(sorted(f['values']))) for f in filters)
    if normalized(plan['filters']) != normalized(want['filters']):
        errors.append('filters')
    point = plan.get('as_of')
    if (point is None) != (want['as_of'] is None) or point and datetime.fromisoformat(point) != datetime.fromisoformat(want['as_of']):
        errors.append('as_of')
    return errors


def run(output, max_requests, case_ids=None, interval_seconds=20):
    if not 1 <= max_requests <= 20:
        raise ValueError('max_requests must be between 1 and 20')
    if interval_seconds < 20:
        raise ValueError('minimum interval is 20 seconds')
    ids=case_ids or [c['id'] for c in CASES]
    known={c['id']:c for c in CASES}
    if len(set(ids)) != len(ids) or not set(ids)<=set(known):
        raise ValueError('unknown or duplicate case ids')
    selected=[known[name] for name in ids][:max_requests]
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    ledger=output/'requests.jsonl'
    # 同一输出目录不能再次发起请求，避免重启脚本超出人工授权额度。
    with ledger.open('x',encoding='utf-8'):
        pass
    source=SimpleNamespace(domain='push', data_start=datetime.fromisoformat('2026-04-01T00:00:00+08:00'),
                           data_cutoff=datetime.fromisoformat('2026-10-15T00:00:00+08:00'))
    original=ModelTransport.post
    sent=0
    last_sent=None
    current_id=None
    visible={}
    results=[]
    def post(self,*args,**kwargs):
        nonlocal sent,visible,last_sent
        if sent >= max_requests:
            raise RuntimeError('model_request_budget_exhausted')
        if last_sent is not None:
            time.sleep(max(0,last_sent+interval_seconds-time.monotonic()))
        sent+=1
        with ledger.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'sequence':sent,'case':current_id})+'\n')
            stream.flush()
        last_sent=time.monotonic()
        response=original(self,*args,**kwargs)
        visible={'http_status':response.status_code}
        retry=response.headers.get('retry-after','')
        if retry.isdigit() and len(retry)<=6:
            visible['retry_after_seconds']=int(retry)
        if response.is_success:
            try:
                raw=response.json(); choice=raw['choices'][0]
                visible.update(content=choice['message']['content'],finish_reason=choice.get('finish_reason'))
            except (ValueError,KeyError,IndexError,TypeError):
                visible={'invalid_envelope':True}
        return response
    def validate_only(source,plan,allowed,usage):
        plan.check_source(source)
        return {'status':'completed','plan':plan.model_dump(mode='json'),'usage':usage}
    with patch.object(ModelTransport,'post',post), patch.object(push_query,'execute_push',validate_only):
        for item in selected:
            current_id=item['id']
            visible={}; started=time.monotonic()
            request=QueryRequest(tenant_id='push',role='admin',allowed_metrics=list(DEFINITIONS),
                                 input={'question':item['question']},push_dialogue=item['dialogue'])
            with telemetry.capture(None) as trace:
                try:
                    actual=push_query.run_push_query(request,source)
                except HTTPException as exc:
                    actual={'status':'error','message':exc.detail}
            result={'id':item['id'],'passed':not mismatches(item,actual),'mismatches':mismatches(item,actual),
                    'actual':actual,'model_response':visible,'error_code':trace['error_code'],
                    'usage':{k:trace[k] for k in ('input_tokens','output_tokens','total_tokens')},
                    'elapsed_ms':round((time.monotonic()-started)*1000)}
            results.append(result)
            (output/'results.json').write_text(json.dumps({'requests':sent,'cases':results},ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({k:result[k] for k in ('id','passed','mismatches','error_code')},ensure_ascii=False),flush=True)
            if trace['error_code']=='model_rate_limit':
                # 一次限流即停止，不消耗后续题目的额度，也不重发本题。
                break
    return results


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--live',action='store_true',required=True)
    parser.add_argument('--max-requests',type=int,required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--case-ids',nargs='+')
    args=parser.parse_args()
    run(args.output,args.max_requests,args.case_ids)
