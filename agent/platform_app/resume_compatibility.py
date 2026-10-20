"""只允许已回放验证的具体升级组合；清单由发布交付，不接受客户端输入。"""
import json
from pathlib import Path


def compatibility(original, current):
    if any(original.get(k)!=current.get(k) for k in ('binding','graph_version')):
        return None
    if original.get('code_digest')==current.get('code_digest'):
        return {'kind':'same_code','target_digest':current['code_digest']}
    path = Path(__file__).with_name('resume_compatibility.json')
    if not path.exists():
        return None
    try:
        records = json.loads(path.read_text(encoding='utf-8'))
        return next((r for r in records if r.get('source_digest')==original.get('code_digest')
                     and r.get('target_digest')==current.get('code_digest') and r.get('graph_version')==current['graph_version']
                     and r.get('recovery_version')==1 and r.get('replay_tests')),None)
    except (ValueError,TypeError,AttributeError):
        return None
