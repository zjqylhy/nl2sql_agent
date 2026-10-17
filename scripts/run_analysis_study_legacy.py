"""在显式测试双库中分别运行 A 单步、B 自适应、C 固定流程；默认仅审计题集。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import csv
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.analysis.dataset import ROOT as DATA, load
from evals.analysis.report import paired_difference, summary
from scripts.evaluate_agent import cleanup, provision, wait_health
from scripts.evaluate_analysis import evaluate

HIDDEN = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


@contextmanager
def stack(binary, fixture, strategy, max_calls):
    """复用已有隔离建库器；每种策略和数据夹具使用全新的身份及 schema。"""
    with (DATA/fixture).open(encoding='utf-8', newline='') as stream:
        rows = [{'purchased_at': r['date'], 'state': r['state'], 'status': r['status'],
                 'amount': r['amount'] or None, 'delivery': None, 'review': None} for r in csv.DictReader(stream)]
    prefix, password, key = secrets.token_hex(8), secrets.token_hex(24), secrets.token_hex(24)
    connection, names, sources = provision(prefix, rows)
    processes = []
    env = {**os.environ, 'PYTHONPATH': str(ROOT/'agent')+os.pathsep+str(ROOT), 'PYTHONUTF8': '1',
           'EVAL_PREFIX': prefix, 'EVAL_PASSWORD': password, 'SERVICE_KEY': key,
           'EVAL_MAX_MODEL_CALLS': str(max_calls), 'ANALYSIS_ENABLED': 'true', 'ANALYSIS_STRATEGY': strategy}
    env.pop('EVAL_CLEANUP_ONLY', None)
    for source in sources.values():
        source['dataset_revision'] = hashlib.sha256((DATA/fixture).read_bytes()).hexdigest()
    env['TENANT_DATASOURCES'] = json.dumps(sources)
    old = {k: os.environ.get(k) for k in ('ANALYSIS_EVAL_TENANT','ANALYSIS_EVAL_USER','ANALYSIS_EVAL_PASSWORD','SERVICE_KEY')}
    try:
        with socket.socket() as available:
            available.bind(('127.0.0.1',0))
            port = available.getsockname()[1]
        probe = f'http://127.0.0.1:{port}'
        env['AGENT_URL'] = probe
        with tempfile.TemporaryFile() as log:
            agent = subprocess.Popen([sys.executable,'-m','uvicorn','scripts.analysis_eval_runtime:app','--host','127.0.0.1','--port',str(port),'--no-access-log'],
                                     cwd=ROOT,env=env,stdout=log,stderr=log,**HIDDEN)
            processes.append(agent)
            wait_health(probe, agent)
            server = subprocess.Popen([str(binary.resolve())],cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=log,text=True,**HIDDEN)
            processes.append(server)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(server.stdout.readline)
                try:
                    base = json.loads(future.result(timeout=20))['api_url']
                except Exception:
                    server.kill()
                    raise RuntimeError('isolated_fixture_start_failed') from None
            wait_health(base,server)
            os.environ.update(ANALYSIS_EVAL_TENANT='ea_'+prefix,ANALYSIS_EVAL_USER='admin',ANALYSIS_EVAL_PASSWORD=password,SERVICE_KEY=key)
            yield base, probe
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
        # Windows terminate 不保证 defer 执行；只清理本次随机前缀对应的测试 schema。
        try:
            subprocess.run([str(binary.resolve())],cwd=ROOT,env={**env,'EVAL_CLEANUP_ONLY':'true'},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20,check=True,**HIDDEN)
        finally:
            cleanup(connection,names)
            for name,value in old.items():
                if value is None:
                    os.environ.pop(name,None)
                else:
                    os.environ[name]=value


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--with-model',action='store_true')
    p.add_argument('--fixture-server',type=Path,default=ROOT/'.cache/analysis-evalfixture.exe')
    p.add_argument('--split',choices=['development','acceptance'],default='development')
    p.add_argument('--repeats',type=int,default=3)
    p.add_argument('--max-model-calls',type=int,default=0,help='每种策略、每个数据夹具的供应商请求上限，真实运行必须显式填写')
    p.add_argument('--output',type=Path,default=ROOT/'.cache/analysis-study.json')
    args=p.parse_args()
    data=load()
    if not args.with_model:
        print(json.dumps({'mode':'dataset_audit','expressions':len(data['cases']),'intent_groups':len({c['group'] for c in data['cases']}),'annotation_status':data['annotation_status'],'model_calls':0}))
        return
    if not 1<=args.repeats<=10 or not 1<=args.max_model_calls<=5000:
        p.error('repeats 1..10 and explicit max-model-calls 1..5000 required')
    if not all(os.getenv(k) for k in ('TEST_DATABASE_URL','MYSQL_TEST_HOST','MYSQL_TEST_PASSWORD','LLM_API_KEY','ANALYSIS_CURRENCY','ANALYSIS_PRICE_VERSION','ANALYSIS_INPUT_PRICE_MICROS','ANALYSIS_OUTPUT_PRICE_MICROS','ANALYSIS_MAX_COST_MICROS','ANALYSIS_TOKENIZER')):
        p.error('explicit test databases, model, tokenizer and real pricing required')
    paths=['agent/runtime','internal','cmd','evals/analysis','scripts/evaluate_analysis.py','scripts/run_analysis_study.py','scripts/analysis_eval_runtime.py']
    if subprocess.check_output(['git','status','--porcelain','--',*paths],cwd=ROOT).strip():
        p.error('commit study code before live execution')
    study={'mode':'live_http','source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
           'annotation_status':data['annotation_status'],'split':args.split,'repeats':args.repeats,
           'model':os.getenv('LLM_MODEL'),'provider_weight_version':'unknown',
           'dataset_sha256':hashlib.sha256((DATA/'cases.json').read_bytes()).hexdigest(),
           'fixture_binary_sha256':hashlib.sha256(args.fixture_server.read_bytes()).hexdigest(),
           'pricing':{k:os.getenv(k) for k in ('ANALYSIS_CURRENCY','ANALYSIS_PRICE_VERSION','ANALYSIS_INPUT_PRICE_MICROS','ANALYSIS_OUTPUT_PRICE_MICROS','ANALYSIS_MAX_COST_MICROS')},'groups':{}}
    for group in ('baseline','adaptive','fixed'):
        results=[]
        for fixture in ('orders.csv','edge-orders.csv'):
            cases=[c for c in data['cases'] if c['split']==args.split and c['fixture']==fixture]
            if not cases:
                continue
            with stack(args.fixture_server,fixture,'fixed' if group=='fixed' else 'adaptive',args.max_model_calls) as (base,probe):
                results.extend(evaluate(base,group,cases,args.repeats,probe,fixture))
            study['groups'][group]={'results':results,'summary':summary(results)}
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(study,ensure_ascii=False,indent=2),encoding='utf-8')
    study['paired_adaptive_minus_baseline']=paired_difference(study['groups']['baseline']['results'],study['groups']['adaptive']['results'])
    args.output.write_text(json.dumps(study,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({g:v['summary'] for g,v in study['groups'].items()},ensure_ascii=False))


if __name__=='__main__':
    main()
