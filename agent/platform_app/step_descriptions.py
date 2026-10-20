"""从已执行的结构化参数生成过程说明，不让模型另写或提前宣称结论。"""
from agent.runtime.push import DEFINITIONS, DIMENSIONS


def describe_step(step, spec=None):
    decision=step.get('decision') or {}
    action=step.get('action') or decision.get('action')
    args=step.get('arguments') or decision.get('arguments') or {}
    scope=(spec or {}).get('scope',{})
    def metrics(values):
        return '、'.join(DEFINITIONS.get(m,{}).get('name',m) for m in values) or '所选指标'
    if action=='query_metrics':
        period=args.get('period','')
        label={'current':'本期','baseline':'基期'}.get(period,period or '指定期间')
        window=scope.get('periods',{}).get(period)
        if window:label+='（'+window['start'][:10]+' 至 '+window['end_exclusive'][:10]+'，结束不含）'
        text='查询'+label+'的'+metrics(args.get('metrics',[]))
        if args.get('dimensions'):
            text+='，按'+'、'.join(DIMENSIONS.get(d,d) for d in args['dimensions'])+'分组'
        if args.get('parent_ref'):text+='，沿已保存的真实分组下钻'
        filters=[*scope.get('filters',[]),*args.get('filters',[])]
        conditions=list(dict.fromkeys(('排除' if f.get('op')=='not_in' else '')+DIMENSIONS.get(f['field'],f['field'])+'：'+'、'.join(f.get('values',[])[:6]) for f in filters))
        if conditions:text+='；'+'；'.join(conditions)
    elif action=='compare_periods':text='比较基期与本期的'+metrics(args.get('metrics',[]) or (spec or {}).get('target_metrics',[]))+'变化'
    elif action=='inspect_result':
        offset=args.get('offset',0);limit=args.get('limit',10)
        text=f'查看已保存结果的第 {offset+1} 至 {offset+limit} 行（实际行数以结果为准）'
        if args.get('sort_metric'):text+='，按'+metrics([args['sort_metric']])+('升序' if args.get('direction')=='asc' else '降序')
    elif action=='decompose_ratio':text='拆解'+metrics([args.get('metric','')])+'的变化，核对组内变化与结构变化贡献'
    elif action=='aggregate_metrics':text='将已保存结果汇总到'+('、'.join(DIMENSIONS.get(d,d) for d in args.get('dimensions',[])) or '整体')
    elif action=='lookup_metrics':text='查看'+metrics(args.get('metrics',[]))+'的计算口径'
    elif action=='request_scope_change':text='提出查询范围变更，等待用户确认'
    elif action=='request_clarification':text='确认分析中缺少的条件'
    elif action=='finish':text='核对证据引用与回答覆盖，整理报告及未验证假设'
    elif step['phase']=='decide':return '根据已确认条件、已有证据和剩余额度选择下一步'
    else:return '校验并准备执行本步操作'
    return ('确定下一步：' if step['phase']=='decide' else '')+text
