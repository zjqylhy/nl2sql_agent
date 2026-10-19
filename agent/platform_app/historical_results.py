"""只读旧结果的最小目录适配；不恢复已经删除的固定目标执行协议。"""
from copy import deepcopy
import hashlib

from agent.runtime import app as runtime
from agent.runtime.multiturn import source_metadata
from agent.runtime.push import metadata_payload


def legacy_push_binding(user, *, analysis_model=None):
    source=runtime.sources().get(user['tenant_id']+':'+user['role'])
    if source is None or source.domain!='push':
        return None
    old=deepcopy(metadata_payload())
    old['version']='push-1'
    old['dimensions'].pop('template_id',None)
    for value in old['metrics'].values():
        value['version']='push-1'
    binding=source_metadata(source,user['tenant_id'],user['role'],push_catalog_payload=old)['binding']
    if analysis_model is not None:
        binding=hashlib.sha256((binding+source.dataset_revision+'push-1'+analysis_model).encode()).hexdigest()
    return binding
