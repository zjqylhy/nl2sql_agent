import {useEffect,useRef,useState} from 'react'
import PushThread from './PushThread'

type Api=<T>(path:string,body?:unknown,method?:string)=>Promise<T>
type Metric={id:string;name:string;unit:string;definition:string}
type Capabilities={domain:string;data_start:string|null;data_cutoff:string;is_synthetic:boolean;metrics:Metric[];enums:Record<string,string[]>}
type Cell={value?:string|null;baseline?:string|null;current?:string|null;delta?:string|null;rate?:string|null;delta_unit?:string}
type Row={label:string;values:Record<string,Cell>;dimensions?:Record<string,string>}
export type Result={domain?:string;result_kind?:string;dimensions?:{id:string;name:string}[];rows:Row[];metrics:Metric[];totals?:Record<string,Cell>;summary?:string;message?:string;limitations?:string[];sql?:string;parameters?:string[];is_synthetic?:boolean;period?:{start:string;end_exclusive:string};presentation?:{layout:string;detail:string};context_compacted?:boolean;completeness?:string;claims?:{result_ref:string;metric:string;field:string;value:string|null;unit:string}[];answers?:{item_id:string;status:string;text:string}[];hypotheses?:string[];uninvestigated?:string[];investigated?:{period:string;dimensions:string[];filters:unknown[]}[];stop_reason?:string}
type QueryProgress={message?:string;steps?:{step_id:string;status:string;cached:boolean;result_ref:string}[];partial_outputs?:{step_id:string;result_ref:string;data:Result}[]}
type Usage={model_calls:number;sql_calls:number;tokens:number;cost:number;unknown_calls?:number}
type ResumeRound={round_no:number;status:string;additional_model_calls:number;usage_before:Usage;usage_after:Usage|null;previous_failure:{message:string;result:Result|null}}
type Retry={available:boolean;execution_version:number;error_code?:string;message?:string;requires_call_extension?:boolean;remaining_budget?:{model_calls:number;tokens:number;cost:number;sql_calls:number}}
type Task={id:string;input:{question:string;push_plan?:unknown;query_contract?:unknown};status:string;error?:string;result:(Result&QueryProgress)|QueryProgress|null;kind:string;intent_policy?:{currency:string};contract_version?:number;push_thread_id?:string;retry?:Retry;resume_rounds?:ResumeRound[];clarification?:{version:number;turns:{question:string;answer:string}[];can_answer:boolean;reason:string}}
type Run={id:string;question:string;status:string;version:number;policy?:{currency:string};retry?:Retry;resume_rounds?:ResumeRound[];report?:Result|null;state:Usage&{message?:string;answers:string[];pending_scope?:{scope:unknown}};usage?:{unknown_calls:number};artifacts:{kind:string;result_ref:string;data:Result}[];steps:{id:string;phase:string;action:string;status:string;description?:string;cached?:boolean;resume_round:number}[]}
const states:Record<string,string>={pending:'排队中',accepted:'已接纳',querying:'执行中',completed:'已完成',needs_clarification:'需要补充',failed:'执行失败',canceled:'已停止',unknown:'结果未知',running:'执行中'}
const actions:Record<string,string>={query_metrics:'查询指标',lookup_metrics:'查看口径',compare_periods:'比较两期',aggregate_metrics:'汇总指标',decompose_ratio:'拆解比率变化',request_scope_change:'申请变更查询范围',finish:'核验完成依据'}
const names:Record<string,string>={app_id:'应用',manufacturer:'设备厂商',channel:'推送通道',day:'发送日期',month:'发送月份',huawei:'华为',xiaomi:'小米',oppo:'OPPO',vivo:'vivo',apple:'Apple',huawei_push:'华为推送',xiaomi_push:'小米推送',oppo_push:'OPPO 推送',vivo_push:'vivo 推送',apns:'APNs',fcm:'FCM'}
const deviceMetrics=new Set(['devices','valid_devices','invalid_devices','unknown_devices','active_devices','active_valid_devices','active_rate'])
const format=(value:string|null|undefined)=>value===null||value===undefined?'无样本':Number(value).toLocaleString('zh-CN',{maximumFractionDigits:2})
const instant=(date:string)=>date+'T00:00:00+08:00'

export function Evidence({result}:{result:Result}){
  const comparison=result.rows.some(r=>Object.values(r.values).some(v=>'delta'in v))
  const onlyValues=result.result_kind==='dimension_values'
  return <article className="answer"><h3>{result.summary??'查询结果'}</h3>
    {result.is_synthetic&&<p className="badge">模拟数据 · 不代表真实厂商表现</p>}
    {result.completeness==='partial'&&<p className="notice">部分完成：仍有未回答事项，请结合调查范围阅读。</p>}
    {result.period&&<p className="muted">{result.period.start} 至 {result.period.end_exclusive}（结束时刻不含）</p>}
    {onlyValues&&<div className="table-scroll"><p>共 {result.rows.length} 组不同取值</p><table><thead><tr>{result.dimensions?.map(d=><th key={d.id}>{d.name}</th>)}</tr></thead><tbody>{result.rows.map((row,i)=><tr key={i}>{result.dimensions?.map(d=><td key={d.id}>{row.dimensions?.[d.id]??row.label}</td>)}</tr>)}</tbody></table>{!result.rows.length&&<p>该发送期间没有符合条件的记录。</p>}</div>}
    {!onlyValues&&result.presentation?.layout!=='table'&&result.totals&&<div className="metric-cards">{result.metrics.map(m=><div key={m.id}><strong>{m.name}</strong><div className="metric-value">{format(result.totals?.[m.id]?.value)}<span>{m.unit}</span></div></div>)}</div>}
    {!onlyValues&&<div hidden={result.presentation?.layout==='cards'} className="table-scroll"><table><thead><tr><th>分组</th><th>指标</th>{comparison?<><th>基期</th><th>报告期</th><th>差值</th><th>相对变化率</th></>:<th>结果</th>}</tr></thead><tbody>{result.rows.flatMap((row,i)=>result.metrics.map(m=>{const c=row.values[m.id];return <tr key={i+m.id}><td>{row.label}</td><td>{m.name}</td>{comparison?<><td>{format(c?.baseline)}</td><td>{format(c?.current)}</td><td>{format(c?.delta)} {c?.delta_unit}</td><td>{format(c?.rate)}{c?.rate!=null?'%':''}</td></>:<td>{format(c?.value)} {m.unit}</td>}</tr>}))}</tbody></table>{result.rows.length===0&&<p className="muted">该范围无可展示分组。</p>}</div>}
    {Boolean(result.claims?.length)&&<section><h4>经程序核对的数值</h4><ul>{result.claims!.map((claim,i)=><li key={i}>{result.metrics.find(m=>m.id===claim.metric)?.name??claim.metric} · {({value:'结果',baseline:'基期',current:'当前期',delta:'差值',rate:'相对变化率'} as Record<string,string>)[claim.field]}：{format(claim.value)} {claim.unit} <small>证据 {claim.result_ref.slice(0,12)}</small></li>)}</ul></section>}
    {result.answers?.map(item=><p key={item.item_id}><strong>{item.status==='answered'?'分析解释':'未回答事项'}：</strong>{item.text}</p>)}
    {Boolean(result.hypotheses?.length)&&<section><h4>待验证假设</h4><ul>{result.hypotheses!.map((item,i)=><li key={i}>{item}</li>)}</ul></section>}
    {result.investigated&&<details><summary>实际调查范围</summary><ul>{result.investigated.map((item,i)=><li key={i}>{item.period} · {item.dimensions.map(d=>names[d]??d).join('、')||'整体'} · 筛选 {JSON.stringify(item.filters)}</li>)}</ul></details>}
    {Boolean(result.uninvestigated?.length)&&<p className="muted">尚未调查：{result.uninvestigated!.join('；')}</p>}
    {result.stop_reason&&<p className="muted">停止原因：{({answered:'已回应记录中的回答事项',unanswered_items:'仍有未完成事项'} as Record<string,string>)[result.stop_reason]??result.stop_reason}</p>}
    {result.limitations?.map(t=><p key={t} className="muted">{t}</p>)}
    <details open={result.presentation?.detail==='detailed'}><summary>{onlyValues?'查询条件与数据依据':'指标口径与数据依据'}</summary>{result.metrics.map(m=><p key={m.id}><strong>{m.name}：</strong>{m.definition}</p>)}{result.sql&&<><pre>{result.sql}</pre><p>绑定参数：{JSON.stringify(result.parameters)}</p></>}</details>
  </article>
}

export default function PushWorkspace({api,storageKey}:{api:Api;storageKey:string}){
  const[caps,setCaps]=useState<Capabilities|null>(null),[enabled,setEnabled]=useState(false)
  const[workflow,setWorkflow]=useState('query'),[queryMode,setQueryMode]=useState('natural'),[domain,setDomain]=useState('push'),[compare,setCompare]=useState(false)
  const[selected,setSelected]=useState(['sent','delivered','delivery_rate','clicked','click_rate']),[dimension,setDimension]=useState('channel')
  const[start,setStart]=useState('2026-07-01'),[end,setEnd]=useState('2026-08-01'),[baselineStart,setBaselineStart]=useState('2026-06-01'),[baselineEnd,setBaselineEnd]=useState('2026-07-01')
  const[hours,setHours]=useState(24),[asOf,setAsOf]=useState(''),[app,setApp]=useState(''),[vendor,setVendor]=useState('')
  const[top,setTop]=useState(false),[rankMetric,setRankMetric]=useState('delivery_rate'),[n,setN]=useState(3),[question,setQuestion]=useState('')
  const[task,setTask]=useState<Task|null>(null),[run,setRun]=useState<Run|null>(null),[history,setHistory]=useState<Task[]>([]),[answer,setAnswer]=useState('')
  const[busy,setBusy]=useState(false),[error,setError]=useState('')
  const[threadId,setThreadId]=useState<string|null>(null)
  const[extraCalls,setExtraCalls]=useState(10),[confirmExtra,setConfirmExtra]=useState(false)
  // 网络中断后重用同一提交标识，服务端不会把重试当成下一轮回答。
  const answerRequest=useRef<{id:string;version:number;answer:string;request_id:string}|null>(null)
  const retryRequest=useRef<{id:string;request_id:string;version:number;extra:number}|null>(null),retryInFlight=useRef(false)
  const refresh=()=>api<Task[]>('/tasks').then(setHistory)
  const load=async(id:string,kind:string,select=true):Promise<void>=>{if(kind==='tree'){setThreadId(id);setTask(null);setRun(null)}else if(kind==='analysis'){const value=await api<Run>('/analysis-runs/'+id);setTask(null);setRun(value);setWorkflow('analysis');setQuestion(value.question)}else{const value=await api<Task>('/tasks/'+id);if(value.kind==='analysis'){await load(id,'analysis',select);return}if(value.push_thread_id&&select){setThreadId(value.push_thread_id);localStorage.setItem(storageKey,JSON.stringify({id:value.push_thread_id,kind:'tree'}));return}setRun(null);setTask(value);if(select){setWorkflow('query');setQueryMode(value.input.push_plan||value.input.query_contract?'form':'natural');setQuestion(value.input.question)}}localStorage.setItem(storageKey,JSON.stringify({id,kind}))}
  useEffect(()=>{void Promise.all([api<Capabilities>('/query-capabilities').then(setCaps),api<{enabled:boolean}>('/analysis-capabilities').then(c=>setEnabled(c.enabled)),refresh()]).then(()=>{const saved=localStorage.getItem(storageKey);if(saved){try{const {id,kind}=JSON.parse(saved);void load(id,kind).catch(e=>setError(e.message))}catch{localStorage.removeItem(storageKey)}}}).catch(e=>setError(e.message))},[storageKey])
  const current=run??task
  const currency=run?.policy?.currency??task?.intent_policy?.currency??''
  const cost=(micros:number)=>`${(micros/1000000).toLocaleString('zh-CN',{maximumFractionDigits:6})} ${currency}`
  const contextVersion=run?.version??task?.clarification?.version??0
  useEffect(()=>{setAnswer('');answerRequest.current=null},[current?.id,contextVersion])
  useEffect(()=>{setConfirmExtra(false);setExtraCalls(10)},[current?.id,current?.retry?.execution_version])
  useEffect(()=>{
    if(!current||!['pending','querying'].includes(current.status))return
    const id=current.id,kind=run?'analysis':'query',source=new EventSource('/api/tasks/'+id+'/events')
    let alive=true
    const update=()=>{if(alive)void load(id,kind,false).then(refresh).catch(e=>setError(e.message))}
    for(const name of ['step','accepted','done','error','clarification','canceled'])source.addEventListener(name,update)
    source.onerror=update
    const timer=setInterval(update,5000)
    return()=>{alive=false;source.close();clearInterval(timer)}
  },[current?.id,current?.status])
  function changeDomain(value:string){setDomain(value);setSelected(value==='device'?['devices','valid_devices','invalid_devices','unknown_devices','active_devices']:['sent','delivered','delivery_rate','clicked','click_rate']);setDimension(value==='device'?'manufacturer':'channel');setCompare(false);setTop(false);setRankMetric(value==='device'?'valid_devices':'delivery_rate')}
  async function submit(event:React.FormEvent){
    event.preventDefault();if(busy)return;setBusy(true);setError('')
    try{
      {
        let input:unknown={question}
        if(queryMode==='natural'){
          if(!question.trim())throw new Error('请输入问题')
        }else{
          if(!selected.length)throw new Error('至少选择一个指标')
          if((top||compare)&&!selected.includes(rankMetric))throw new Error('排名指标必须包含在所选指标中')
          const dimensions=dimension?dimension.split(','):[]
          if((top||compare)&&dimensions.length!==1)throw new Error('排名请选择一个分组维度')
          const filters=[...(app?[{field:'app_id',op:'in',values:[app]}]:[]),...(vendor?[{field:'manufacturer',op:'in',values:[vendor]}]:[])]
          const timeRange={start:instant(start),end_exclusive:instant(end)}
          const stateAt=domain==='device'&&asOf?asOf+':00+08:00':null
          const plan={domain:'push',version:1,metrics:selected,dimensions,time_range:timeRange,observation_hours:hours,as_of:stateAt,filters}
          const label=`${domain==='device'?'设备':'推送'}取数：${start} 至 ${end}，${selected.map(k=>caps?.metrics.find(m=>m.id===k)?.name??k).join('、')}`
          if(top||compare){
            const steps:Record<string,unknown>[]=[{id:'A',op:'query',period:'current',metrics:selected,dimensions}]
            const periods:Record<string,unknown>={current:timeRange}
            let output='A'
            let predicate:Record<string,string>|null=null
            if(compare){
              periods.baseline={start:instant(baselineStart),end_exclusive:instant(baselineEnd)}
              steps.push({id:'B',op:'query',period:'baseline',metrics:selected,dimensions},{id:'C',op:'compare',baseline:'B',current:'A'})
              predicate={metric:rankMetric,field:'delta',comparison:'lt',value:'0'}
              steps.push({id:'F',op:'filter_rows',input:'C',predicate});output='F'
            }
            const tieBreakers=[{field:dimension,direction:'asc'}]
            const direction=compare?'asc':'desc',valueField=compare?'delta':'value'
            steps.push({id:'S',op:'sort_limit',input:output,metric:rankMetric,direction,limit:n,value_field:valueField,tie_breakers:tieBreakers})
            const contract={task_type:'query',question:label,scope:{filters,periods,observation_hours:hours,as_of:stateAt},
              result_requirements:{metrics:selected,dimensions,period_names:compare?['baseline','current']:['current'],coverage:'complete',
                selection:{field:dimension,rank_metric:rankMetric,direction,limit:n,value_field:valueField,filter:predicate,tie_breakers:tieBreakers}},
              execution_plan:{schema_version:1,steps,outputs:[{step:'S',metrics:selected,dimensions}]}}
            input={question:label,query_contract:contract}
          }else input={question:label,push_plan:plan}
        }
        const value=await api<Task>('/tasks',input);setTask(value);setRun(null)
        localStorage.setItem(storageKey,JSON.stringify({id:value.id,kind:'query'}))
      }
      await refresh()
    }catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  async function supplement(event:React.FormEvent){
    event.preventDefault();if(busy||!current||!answer.trim())return
    const version=run?.version??task?.clarification?.version??0
    const previous=answerRequest.current
    const request=previous?.id===current.id&&previous.version===version&&previous.answer===answer?previous:{id:current.id,version,answer,request_id:crypto.randomUUID()}
    answerRequest.current=request;setBusy(true);setError('')
    try{
      const path=run?'/analysis-runs/':'/tasks/'
      const value=await api<Run|Task>(path+current.id+'/answers',{answer:request.answer,request_id:request.request_id,expected_version:request.version})
      if(run)setRun(value as Run);else setTask(value as Task)
      setAnswer('');answerRequest.current=null;await refresh()
    }catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  async function retryModel(){
    if(busy||retryInFlight.current||!current?.retry?.available)return
    const extra=current.retry.requires_call_extension?extraCalls:0
    if(extra&&(!confirmExtra||!Number.isInteger(extra)||extra<1||extra>10))return
    const previous=retryRequest.current
    const version=current.retry.execution_version
    const request=previous?.id===current.id&&previous.version===version&&previous.extra===extra?previous:{id:current.id,request_id:crypto.randomUUID(),version,extra}
    retryRequest.current=request;retryInFlight.current=true;setBusy(true);setError('')
    try{
      const value=await api<Task>('/tasks/'+current.id+'/retry',{request_id:request.request_id,expected_execution_version:request.version,additional_model_calls:request.extra})
      await load(value.id,value.kind);retryRequest.current=null;await refresh()
    }catch(e){setError((e as Error).message)}finally{retryInFlight.current=false;setBusy(false)}
  }
  function newQuery(edit=false){if(edit&&current){setWorkflow('query');setQueryMode('natural');setQuestion(run?.question??task!.input.question)}else setQuestion('');setTask(null);setRun(null);setWorkflow('query');setAnswer('');setError('');answerRequest.current=null;retryRequest.current=null;localStorage.removeItem(storageKey)}
  const continuing=queryMode==='natural'&&Boolean(task)
  const report=run?.report
  const result=task?.result&&'rows'in task.result?task.result:null
  const phase=run?'已识别：开放任务分析':task?.contract_version?'已识别：明确条件取数':current&&['pending','querying'].includes(current.status)?'正在识别并执行':task?.status==='needs_clarification'?'等待补充任务条件':'任务已停止或未完成'
  if(threadId)return <section className="push-workspace"><div className="answer-heading"><h2>推送与设备分析会话</h2><button onClick={()=>{setThreadId(null);newQuery()}}>返回查询</button></div><PushThread api={api} id={threadId}/></section>
  return <section className="push-workspace">
    <div className="answer-heading"><div><h2>推送与设备分析</h2><p className="muted">直接描述取数或探索问题，系统识别意图后执行对应流程。</p></div><button disabled={busy} onClick={()=>newQuery()}>新建任务</button></div>
    {caps?.is_synthetic&&<div className="notice">当前使用模拟数据。发送样本覆盖 2026 年 4—9 月，数据截止 {caps.data_cutoff}。通道表现为人工模拟。</div>}
    {error&&<div role="alert" className="error">{error}</div>}
    <div className="push-toolbar"><span>输入后自动识别任务类型</span><label>输入方式<select value={queryMode} onChange={e=>{setQueryMode(e.target.value);setWorkflow('query');setTask(null);setRun(null)}}><option value="natural">描述问题或分析需求</option><option value="form">辅助表单：明确查询条件</option></select></label><label>历史任务<select value={current?.id??''} onChange={e=>{const item=history.find(h=>h.id===e.target.value);if(item)void load(item.id,item.kind).catch(e=>setError(e.message))}}><option value="">选择任务</option>{history.map(t=><option key={t.id} value={t.id}>{states[t.status]} · {t.input.question.slice(0,45)}</option>)}</select></label></div>
    <form onSubmit={submit} className="analysis-form">
      {queryMode==='natural'?<label>{continuing?'原问题':'问题'}<textarea rows={3} value={continuing?task!.input.question:question} readOnly={continuing} onChange={e=>setQuestion(e.target.value)} maxLength={2000} placeholder="例如：查询 app_1 在2026年7月各通道发送量与到达率，到达观察时长为24小时" required/></label>:<>
        <div className="push-controls"><label>数据粒度<select value={domain} onChange={e=>changeDomain(e.target.value)}><option value="push">逻辑推送</option><option value="device">应用设备</option></select></label><label>{domain==='device'?'活跃期间开始':'发送期间开始'}<input type="date" value={start} onChange={e=>setStart(e.target.value)} required/></label><label>结束日期（不含）<input type="date" value={end} onChange={e=>setEnd(e.target.value)} required/></label>
        {domain==='push'?<label>发送后的回执观察时长（小时）<input type="number" min={1} max={8760} value={hours} onChange={e=>setHours(Number(e.target.value))} required/></label>:<label>设备状态时点（可选，上海时区）<input type="datetime-local" value={asOf} onChange={e=>setAsOf(e.target.value)}/><small>留空使用查询期末前一瞬间</small></label>}
        <label>分组<select value={dimension} onChange={e=>setDimension(e.target.value)}><option value="">整体</option><option value="app_id">应用</option><option value="manufacturer">设备厂商</option><option value="app_id,manufacturer">应用与设备厂商</option>{domain==='push'&&<><option value="channel">实际推送通道</option><option value="app_id,channel">应用与通道</option><option value="day">发送日期</option><option value="month">发送月份</option></>}</select></label><label>应用 ID（可选）<input value={app} onChange={e=>setApp(e.target.value)} maxLength={100} placeholder="例如 app_1"/></label><label>设备厂商<select value={vendor} onChange={e=>setVendor(e.target.value)}><option value="">全部授权厂商</option>{caps?.enums.manufacturer?.map(v=><option key={v} value={v}>{names[v]??v}</option>)}</select></label></div>
        <fieldset className="metric-options"><legend>指标</legend>{caps?.metrics.filter(m=>deviceMetrics.has(m.id)===(domain==='device')).map(m=><label key={m.id} title={m.definition}><input type="checkbox" checked={selected.includes(m.id)} onChange={e=>setSelected(e.target.checked?[...selected,m.id]:selected.filter(k=>k!==m.id))}/>{m.name}</label>)}</fieldset>
        {workflow==='query'&&<><div className="push-checks">{domain==='push'&&<label><input type="checkbox" checked={compare} onChange={e=>{setCompare(e.target.checked);setTop(false)}}/>比较两期并排名下降分组</label>}{!compare&&<label><input type="checkbox" checked={top} onChange={e=>setTop(e.target.checked)}/>选取 Top N 并汇总</label>}</div>{compare&&<div className="push-controls"><label>基期开始<input type="date" value={baselineStart} onChange={e=>setBaselineStart(e.target.value)} required/></label><label>基期结束（不含）<input type="date" value={baselineEnd} onChange={e=>setBaselineEnd(e.target.value)} required/></label></div>}{(compare||top)&&<div className="push-controls"><label>排名指标<select value={rankMetric} onChange={e=>setRankMetric(e.target.value)}>{selected.map(id=><option key={id} value={id}>{caps?.metrics.find(m=>m.id===id)?.name??id}</option>)}</select></label><label>前 N 组<input type="number" min={1} max={10} value={n} onChange={e=>setN(Number(e.target.value))}/></label><p className="muted">下降按原始差值排序；比率差值使用百分点。</p></div>}</>}
      </>}
      {workflow==='analysis'&&!enabled&&<p className="muted">开放分析需配置模型与预算；明确条件表单取数可独立使用。</p>}
      {continuing?<><p className="muted">补充条件请在下方追问处回答，系统会保留原问题。</p><button type="button" disabled={busy} onClick={()=>newQuery(true)}>修改原问题并新建查询</button></>:<button className="primary" disabled={busy||!caps}>{busy?'正在提交…':queryMode==='natural'?'提交任务':'执行明确取数'}</button>}
    </form>
    {current&&<><div className="task-progress"><strong>{states[current.status]??current.status}</strong><span>{phase}</span><span>任务 {current.id.slice(0,8)}</span>{['pending','querying','needs_clarification'].includes(current.status)&&<button onClick={async()=>{try{await api('/tasks/'+current.id+'/stop',{});await load(current.id,run?'analysis':'query')}catch(e){setError((e as Error).message)}}}>停止</button>}</div>
      {task?.error&&<p className="error">{task.error}</p>}
      {current.status==='failed'&&<section className="notice">{current.retry?.available?<>
        <p>{run?'保持当前任务，复用已完成查询和计算，从失败决策处继续。':'重试识别，保留原问题和已补充条件。'}累计消耗不会归零，新调用照常计费，未知调用的原预留保留。</p>
        {current.retry.requires_call_extension&&<><p>模型调用次数已耗尽。剩余Token {current.retry.remaining_budget?.tokens}，费用额度 {cost(current.retry.remaining_budget?.cost??0)}，SQL {current.retry.remaining_budget?.sql_calls} 次；其他预算上限不变。</p><label>追加模型调用次数（1至10）<input type="number" min={1} max={10} value={extraCalls} disabled={busy} onChange={e=>{setExtraCalls(Number(e.target.value));setConfirmExtra(false)}}/></label><label><input type="checkbox" checked={confirmExtra} disabled={busy} onChange={e=>setConfirmExtra(e.target.checked)}/>确认本次追加最多 {extraCalls} 次模型调用，其余预算不变</label></>}
        <button type="button" className="primary" disabled={busy||Boolean(current.retry.requires_call_extension&&(!confirmExtra||!Number.isInteger(extraCalls)||extraCalls<1||extraCalls>10))} onClick={retryModel}>{busy?'正在提交…':current.retry.requires_call_extension?'确认追加并继续':'从失败处继续'}</button>
        {current.retry.error_code==='model_quota'&&<p>请先恢复模型网关可用额度，再继续。</p>}
      </>:<p>{current.retry?.message||'当前故障不支持从失败处继续。'}</p>}<button type="button" disabled={busy} onClick={()=>newQuery(true)}>重新开始（新建任务）</button></section>}
      {Boolean(current.resume_rounds?.length)&&<details open><summary>续跑历史与新增消耗</summary>{current.resume_rounds!.map(r=>{const usage=r.usage_after??run?.state;return <section key={r.round_no}><h4>第 {r.round_no} 轮续跑 · {states[r.status]??r.status}</h4><p>此前失败：{r.previous_failure.message}；本轮授权追加 {r.additional_model_calls} 次。</p>{usage&&<p>本轮新增模型调用 {usage.model_calls-r.usage_before.model_calls} · SQL {usage.sql_calls-r.usage_before.sql_calls} · Token预留及消耗 {usage.tokens-r.usage_before.tokens} · 费用预留及消耗 {cost(usage.cost-r.usage_before.cost)}；累计模型调用 {usage.model_calls} · SQL {usage.sql_calls} · Token {usage.tokens}</p>}{r.previous_failure.result?.rows&&<details><summary>上一轮部分结果（历史记录）</summary><Evidence result={r.previous_failure.result}/></details>}</section>})}</details>}
      {Boolean(task?.result?.steps?.length)&&<details><summary>程序执行步骤与完整结果</summary>{task!.result!.steps!.map(step=><p key={step.step_id}>{step.step_id} · {states[step.status]??step.status}{step.cached?' · 复用已保存结果':''} · <a href={'/api/tasks/'+task!.id+'/evidence/'+step.result_ref} target="_blank" rel="noreferrer">查看完整结果</a></p>)}</details>}
      {task?.result?.partial_outputs?.map(output=><details key={output.step_id}><summary>已完成步骤 {output.step_id}（任务整体未完成）</summary><Evidence result={output.data}/><a href={'/api/tasks/'+task.id+'/evidence/'+output.result_ref} target="_blank" rel="noreferrer">查看该步骤完整结果</a></details>)}
      {task&&Boolean(task.clarification?.turns.length)&&<article aria-label="已提交的补充记录"><h3>补充记录</h3>{task.clarification!.turns.map((turn,index)=><div key={index}><p className="notice"><strong>助手追问：</strong>{turn.question}</p><p><strong>你的补充：</strong>{turn.answer}</p></div>)}</article>}
      {task?.status==='needs_clarification'&&task.result&&'message'in task.result&&task.result.message&&<div className="notice"><h3>需要你补充</h3><p>{task.result.message}</p>{task.clarification?.can_answer?<form onSubmit={supplement} className="analysis-form"><label>回答这条追问<textarea autoFocus rows={3} value={answer} onChange={e=>setAnswer(e.target.value)} maxLength={1000} placeholder="例如：观察时长24小时，统计2026年8月1日至10月1日（结束不含）" required/></label><p className="muted">只需回答缺少的条件，无需重复原问题。最多补充2次，每次请在24小时内回复。</p><button className="primary" disabled={busy||!answer.trim()}>{busy?'正在提交…':'补充并继续查询'}</button></form>:<p className="muted">{task.clarification?.reason||'请使用表单明确查询条件。'}</p>}</div>}
      {run&&<><p className="muted">模型调用 {run.state.model_calls} · SQL {run.state.sql_calls} · Token 预留与已知消耗 {run.state.tokens}</p>{run.state.message&&<p className="notice">{run.state.message}</p>}{Boolean(run.usage?.unknown_calls)&&<p className="notice">存在用量未知调用，保留预留，不自动重发。</p>}{run.status==='needs_clarification'&&!run.state.pending_scope&&<form onSubmit={supplement}><label>补充条件<textarea value={answer} onChange={e=>setAnswer(e.target.value)} maxLength={1000} required/></label><button disabled={busy}>补充并继续</button></form>}{run.status==='needs_clarification'&&run.state.pending_scope&&<section className="notice"><h3>确认本次查询范围变更</h3><pre>{JSON.stringify(run.state.pending_scope.scope,null,2)}</pre><p>确认后保存新约定，重新取证；已用预算保留。</p>{[true,false].map(approved=><button key={String(approved)} disabled={busy} onClick={async()=>{setBusy(true);try{const value=await api<Run>('/analysis-runs/'+run.id+'/scope-confirmations',{approved,expected_version:run.version,request_id:crypto.randomUUID()});setRun(value)}catch(e){setError((e as Error).message)}finally{setBusy(false)}}}>{approved?'确认变更并继续':'拒绝，保留原范围'}</button>)}</section>}{run.status==='failed'&&<p className="notice">任务未完整完成；下方仅保留已取得的证据。</p>}</>}
      {result&&<><Evidence result={result}/>{result.result_kind!=='dimension_values'&&<button disabled={busy} onClick={async()=>{setBusy(true);try{const value=await api<{id:string}>('/push-threads',{task_id:task!.id});await load(value.id,'tree')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}}>继续追问或调整回答</button>}</>} {report&&<Evidence result={report}/>}
      {run&&<><details open><summary>执行步骤</summary>{[...new Set(run.steps.map(s=>s.resume_round))].map(round=><section key={round}><h4>{round?'第 '+round+' 轮续跑':'原执行'}</h4><ol>{run.steps.filter(s=>s.resume_round===round).map(s=><li key={s.id}>{s.description??actions[s.action]??(s.phase==='decide'?'根据已有证据选择下一步':'校验并准备本步操作')} · {states[s.status]??s.status}{s.cached?' · 复用已保存结果':''}</li>)}</ol></section>)}</details><details><summary>已保存证据</summary>{run.artifacts.filter(a=>a.kind!=='report').map(a=><details key={a.result_ref}><summary>{a.kind} · {a.result_ref.slice(0,12)}</summary>{a.kind==='query'?<Evidence result={a.data}/>:<pre>{JSON.stringify(a.data,null,2)}</pre>}</details>)}</details></>}
    </>}
  </section>
}
