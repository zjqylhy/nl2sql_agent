"""只读容器上的多轮查询，使用原始 Olist CSV 独立重算黄金结果。"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from evals.multiturn.dataset import load
from evals.multiturn.scorer import score
from scripts.verify_semantic import facts

PROBE='''import json,os,sys,httpx
chains=json.loads(sys.stdin.read())
headers={'X-Service-Key':os.environ['SERVICE_KEY']}
with httpx.Client(timeout=35,trust_env=False) as client:
 meta=client.post('http://127.0.0.1:8000/internal/context',headers=headers,json={'tenant_id':'alpha','role':'admin'})
 meta.raise_for_status();binding=meta.json()['binding'];records=[]
 for chain in chains:
  context={'binding':binding,'version':0,'plan':None,'pending':None}
  for index,turn in enumerate(chain['turns'],1):
   response=client.post('http://127.0.0.1:8000/internal/query',headers=headers,json={'tenant_id':'alpha','role':'admin','allowed_metrics':['gmv','orders','aov','cancellation_rate','delivery_days','review_score'],'context':context,'input':{'question':turn['question']}})
   result=response.json()
   if response.status_code==200:
    if result['status']=='completed':context.update(plan=result['plan'],pending=None)
    elif (result.get('multiturn') or {}).get('pending'):context['pending']=result['multiturn']['pending']
   context['version']+=2
   records.append({'chain':chain['id'],'turn':index,'http_status':response.status_code,'result':result,'conversation':dict(context)})
 print(json.dumps(records))
'''

def run(args):
    selected=[c.model_dump() for c in load().chains if c.id in ('chain-01','chain-02','chain-16')]
    if not args.with_model:selected=[c for c in selected if c['category']!='unfamiliar']
    command=['docker','compose','-f',str(args.compose_file),'exec','-T','agent','python','-c',PROBE]
    response=subprocess.run(command,input=json.dumps(selected).encode(),capture_output=True,timeout=180)
    if response.returncode:raise RuntimeError('readonly_probe_failed')
    observed=json.loads(response.stdout)
    rows=[{**r,'purchased_at':r['date']} for r in facts(args.csv_dir)]
    turns={(c['id'],n):t for c in selected for n,t in enumerate(c['turns'],1)}
    if len(observed)!=len(turns) or len({(r['chain'],r['turn']) for r in observed})!=len(turns):raise RuntimeError('missing_or_duplicate_observation')
    records=[]
    for r in observed:
        result=r['result'];trace=result.get('telemetry') or {}
        trace={**trace,'sql_calls':int('sql' in trace.get('stages_ms',{}))}
        usage=result.get('usage') or {}
        task={'status':result.get('status'),'result':result,'http_status':r['http_status']}
        verdict=score(turns[(r['chain'],r['turn'])],task,trace,r['conversation'],'controlled',rows)
        records.append({'chain':r['chain'],'turn':r['turn'],**verdict,'http_status':r['http_status'],'task_status':task['status'],'error_code':trace.get('error_code',''),'parser':usage.get('parser','unknown'),'tokens':usage.get('tokens'),'model_calls':trace.get('model_calls',0),'sql_calls':trace.get('sql_calls',0)})
    report={'status':'complete' if all(r['passed'] for r in records) else 'failed','scope':'python_mysql_readonly_olist_csv','sql_observation':'runtime_sql_stage','source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'csv_hashes':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in args.csv_dir.glob('*.csv')},'records':records}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({'status':report['status'],'turns':len(records),'correct':sum(r['passed'] for r in records),'model_calls':sum(r['model_calls'] for r in records),'tokens':sum(r['tokens'] or 0 for r in records)}))
    if report['status']!='complete':raise RuntimeError('readonly_csv_comparison_failed')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compose-file',type=Path,required=True)
    parser.add_argument('--csv-dir',type=Path,default=ROOT/'data/olist')
    parser.add_argument('--with-model',action='store_true')
    parser.add_argument('--output',type=Path,required=True)
    run(parser.parse_args())
