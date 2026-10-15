"""离线核验固定分母、重复记录、观测总量及分区；不请求模型。"""
import json
import re
from .dataset import load,DATASET
from .scorer import summarize
from evals.understanding.report import text_hashes

def validate(study):
    data=load();identity=study['identity']
    if study.get('report_version')!=1 or study.get('status')!='complete':raise ValueError('incomplete study')
    if not re.fullmatch('[0-9a-f]{40}',identity['source_commit']):raise ValueError('missing source commit')
    if identity['dataset_hash'] not in text_hashes(DATASET) or identity['annotation_status']!=data.annotation_status or identity['reviewer']!=data.reviewer:raise ValueError('annotation identity mismatch')
    selected=[c for c in data.chains if identity['split']=='all' or c.split==identity['split']]
    if set(study['chain_ids'])!={c.id for c in selected} or len(study['chain_ids'])!=len(selected):raise ValueError('diagnostic or missing chains cannot represent full partition')
    expected={(m,c.id,n) for m in ('independent','controlled') for c in selected for n in range(1,len(c.turns)+1)}
    golden={(c.id,n):t for c in selected for n,t in enumerate(c.turns,1)}
    if not 1<=len(study['rounds'])<=3:raise ValueError('invalid rounds')
    for index,pair in enumerate(study['rounds'],1):
        if pair['round']!=index or pair['status']!='complete':raise ValueError('incomplete round')
        records=pair['records']
        if len(records)!=len(expected) or {(r['mode'],r['chain'],r['turn']) for r in records}!=expected:raise ValueError('missing or duplicate turns')
        if pair['issued_calls']!=sum(r['model_calls'] for r in records) or pair['issued_calls']>120:raise ValueError('incomplete usage or budget exceeded')
        if any(type(r['model_calls']) is not int or r['model_calls']<0 or r['unknown_usage_calls']>r['model_calls'] for r in records):raise ValueError('invalid usage')
        for r in records:
            turn=golden[(r['chain'],r['turn'])]
            if r['action']!=turn.action or r['expected_status']!=turn.status:raise ValueError('golden contract differs')
            if r['passed'] and turn.status=='completed' and (r['plan_correct'] is not True or r['result_correct'] is not True or r['sql_calls']<1):raise ValueError('execution alone cannot establish correctness')
            if r['passed'] and (not r['status_correct'] or r['unsafe_execution']):raise ValueError('invalid passed verdict')
            if r['passed'] and r['mode']=='controlled' and (r['context_correct'] is not True or r['draft_correct'] is not True):raise ValueError('missing persistent state verdict')
        for mode in ('independent','controlled'):
            if pair['summary'][mode]!=summarize([r for r in records if r['mode']==mode]):raise ValueError('summary denominator mismatch')
    return study

def inspect(path):
    study=validate(json.loads(path.read_text(encoding='utf-8')))
    return {'source_commit':study['identity']['source_commit'],'annotation_status':study['identity']['annotation_status'],'rounds':[r['summary'] for r in study['rounds']]}
