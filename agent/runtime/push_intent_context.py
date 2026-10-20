"""初始意图协议保留完整用户条件；精简固定说明，为两轮补充留出预算。"""
import json

from .push import DEFINITIONS, DIMENSIONS, ENUMS

# 只精简可重复的协议说明，不截断问题、补充记录或目录，也不调用模型压缩。
PROTOCOL = (
    'Return one JSON: {"plan":PLAN} for single query, {"contract":CONTRACT} for multi-step query or analysis, '
    'or {"clarification":"简洁中文追问"}. query=specified results (including multi-step); analysis=investigate causes using results. '
    'Do not enumerate business question types. No SQL/code/accounts/sources/unknown fields. Preserve ALL original and clarified '
    'conditions. Ask in Chinese for missing metric definition, dates, scope or horizon; never ignore unsupported conditions '
    'or default seven days/24h/all apps/baseline. Chinese dates +08:00, end exclusive. Never mix device/message metrics. '
    'FILTER={field,op:"in"|"not_in",values:[string]}; PERIOD={start:ISO,end_exclusive:ISO}. '
    'PLAN={metrics:[],dimensions:[],time_range:PERIOD,observation_hours:hours,as_of:null|ISO,filters:[]}. '
    'CONTRACT={schema_version:1,domain:"push",task_type:"query"|"analysis",question:original_question,'
    'scope:{filters:[],periods:{current:PERIOD,baseline:optional_PERIOD},observation_hours:hours,as_of:null}}. '
    'query adds result_requirements:{metrics:[],dimensions:[],period_names:[],coverage:"complete",'
    'selection:null|{field,rank_metric,direction:"asc"|"desc",limit:N,value_field:"value"|"delta"|"rate",'
    'filter:null|PREDICATE,tie_breakers:[TIE]}},execution_plan:{schema_version:1,steps:[STEP],'
    'outputs:[{step:id,metrics:[],dimensions:[]}]}. '
    'STEP variants: {id,op:"query",period,metrics:[],dimensions:[],filters:[FILTER],filters_from:[FROM]}; '
    '{id,op:"sort_limit",input:id,metric,direction,limit,value_field,tie_breakers:[TIE]}; '
    '{id,op:"aggregate",input:id,dimensions:[]}; {id,op:"compare",baseline:id,current:id}; '
    '{id,op:"filter_rows",input:id,predicate:PREDICATE}. '
    'FROM={field,op:"in",step:prior_id,column:same_dimension,on_empty:"return_empty"}; '
    'TIE={field,direction:"asc"}; PREDICATE={metric,field:"value"|"delta"|"rate",'
    'comparison:"lt"|"lte"|"gt"|"gte"|"eq",value:decimal_string}. '
    'Complete finite acyclic query plan at once; each query inherits scope. Rank excludes no-sample rows, '
    'ties by group ID ascending; compare equal nonoverlapping periods. '
    'analysis adds target_metrics:[],answer_items:[{id:"a1",text:user_question,source:"question"}]; '
    'omit execution_plan/exploration_permissions. Do not pre-plan investigation. '
    'Distinct SEND values (apps/templates/manufacturers): query CONTRACT, observation_hours=null,as_of=null; '
    'result_requirements:{kind:"dimension_values",metrics:[],dimensions:[dimension],period_names:["current"]}; '
    'steps:[{id:"A",op:"query_values",period:"current",dimensions:[dimension]}],'
    'outputs:[{step:"A",metrics:[],dimensions:[dimension]}]. Needs dates, no receipts/horizon. '
    'Other metric tasks need nonempty metrics and positive observation_hours. '
)


def initial_messages(request, source):
    def compact(value):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    prompt = (PROTOCOL+'Allowed metrics='+compact({k:v['name']
        for k,v in DEFINITIONS.items() if k in request.allowed_metrics})+
        '; dimensions='+compact(DIMENSIONS)+'; enums='+compact(ENUMS)+'; as_of device-only; send end+horizon<=cutoff. Bounds are not user-selected dates. '+
        'Data start='+str(source.data_start.isoformat() if source.data_start else 'unset')+'; cutoff='+source.data_cutoff.isoformat())
    question = request.input.question
    if request.push_dialogue:
        question = compact({'original_question':question,
            'clarifications':[turn.model_dump() for turn in request.push_dialogue]})
    return prompt, question
