"""独立 CSV 预期对照真实 SQL 和工具链；不测模型意图识别或声称模型准确率。"""
import csv
import hashlib
import os
from pathlib import Path
import secrets
import sys

import pytest
from fastapi.testclient import TestClient
from runtime import app as runtime
from runtime.analysis import Spec
from runtime.analysis_api import Identity, context
from runtime.analysis_fixed import next_action

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from evals.analysis.dataset import ROOT, load
from evals.analysis.oracle import expected, score
from scripts.evaluate_agent import cleanup, provision


@pytest.mark.parametrize('fixture',['orders.csv','edge-orders.csv'])
def test_independent_corpus_gold_against_real_tools(fixture,monkeypatch):
    if not os.getenv('MYSQL_TEST_HOST'):
        pytest.skip('MYSQL_TEST_HOST not configured')
    with (ROOT/fixture).open(encoding='utf-8',newline='') as stream:
        rows=[{'purchased_at':r['date'],'state':r['state'],'status':r['status'],'amount':r['amount'] or None,'delivery':None,'review':None} for r in csv.DictReader(stream)]
    prefix=secrets.token_hex(8)
    db,names,sources=provision(prefix,rows)
    try:
        key='ea_'+prefix+':admin'
        source=runtime.Datasource.model_validate({**sources[key],'dataset_revision':hashlib.sha256((ROOT/fixture).read_bytes()).hexdigest()})
        monkeypatch.setattr(runtime,'sources',lambda:{key:source})
        monkeypatch.setenv('SERVICE_KEY','s'*40)
        identity={'tenant_id':'ea_'+prefix,'role':'admin','allowed_metrics':['gmv','orders','aov']}
        binding=context(Identity.model_validate(identity))['binding']
        cases=[c for c in load()['cases'] if c['fixture']==fixture and c.get('spec') and c['id'].endswith('-1')]
        with TestClient(runtime.app) as client:
            for case in cases:
                spec=Spec.model_validate(case['spec'])
                artifacts=[]
                report=None
                for _ in range(12):
                    decision=next_action(spec,artifacts)
                    result=client.post('/internal/analysis/tool',headers={'X-Service-Key':'s'*40},json={
                        **identity,'binding':binding,'question':case['question'],'spec':decision['spec'],
                        'artifacts':artifacts,'action':decision['action']})
                    assert result.status_code==200,(case['id'],result.text)
                    output=result.json()
                    if output['status']!='completed':
                        assert output['error_code']=='missing_evidence',(case['id'],output)
                        break
                    artifacts.append(output['artifact'])
                    if artifacts[-1]['kind']=='report':
                        report=artifacts[-1]['data']
                        break
                gold=expected(ROOT/fixture,case['spec'])
                assert score(report,gold) if gold['complete'] else report is None,(case['id'],report,gold)
    finally:
        cleanup(db,names)
