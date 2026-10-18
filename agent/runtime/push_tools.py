"""推送工具使用同一平台账本；不能查询确认规格之外的条件。"""
from .analysis import Clarify, Finish, Rank, artifact, reject
from .push import DEFINITIONS, execute_push
from .push_analysis import Compare, Lookup, PushSpec, Query, compare, finish, plans, rank


def tool(request, source):
    action, spec = request.action, request.spec
    if isinstance(action, Clarify):
        return {'status': 'needs_clarification', **action.arguments.model_dump()}
    if isinstance(action, Lookup):
        ids = action.arguments.metrics
        if not set(ids) <= set(request.allowed_metrics):
            reject('permission_denied', '指标未授权')
        result = artifact('catalog', {m: DEFINITIONS[m] for m in ids}, ids, request.binding)
    else:
        if not isinstance(spec, PushSpec):
            reject('missing_conditions', '请先明确指标、时间、观察窗口和分组')
        if isinstance(action, Query):
            plan = action.arguments.plan
            if plan not in plans(spec):
                reject(message='查询不能更改已确认分析条件')
            data = execute_push(source, plan, request.allowed_metrics, {'parser': 'tool', 'tokens': 0})
            result = artifact('query', data, data['metrics_used'], request.binding)
        elif isinstance(action, Compare):
            result = compare(action.arguments, request.artifacts, spec, request.binding)
        elif isinstance(action, Rank):
            if spec.goal != 'compare_push':
                reject()
            result = rank(action.arguments, request.artifacts, spec, request.binding)
        elif isinstance(action, Finish):
            result = finish(action.arguments, request.artifacts, spec, request.binding)
            result['data']['is_synthetic'] = source.is_synthetic
            if not source.is_synthetic:
                result['data']['limitations'] = [x for x in result['data']['limitations'] if '模拟' not in x]
            # 修改产物内容后重新计算内容引用，避免保存哈希与正文不一致。
            result = artifact(result['kind'], result['data'], result['metrics_used'], request.binding)
        else:
            reject(message='当前数据域不支持该工具协议')
    if not set(result['metrics_used']) <= set(request.allowed_metrics):
        reject('permission_denied', '派生指标未授权')
    return {'status': 'completed', 'artifact': result}
