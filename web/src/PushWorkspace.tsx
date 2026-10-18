import {useEffect,useRef,useState} from 'react'

type Api=<T>(path:string,body?:unknown,method?:string)=>Promise<T>
type Metric={id:string;name:string;unit:string;definition:string}
type Capabilities={domain:string;data_start:string|null;data_cutoff:string;is_synthetic:boolean;metrics:Metric[];enums:Record<string,string[]>}
type Cell={value?:string|null;baseline?:string|null;current?:string|null;delta?:string|null;rate?:string|null;delta_unit?:string}
type Row={label:string;values:Record<string,Cell>}
type Result={domain?:string;rows:Row[];metrics:Metric[];totals?:Record<string,Cell>;summary?:string;message?:string;limitations?:string[];sql?:string;parameters?:string[];is_synthetic?:boolean;period?:{start:string;end_exclusive:string}}
type Task={id:string;input:{question:string;push_plan?:unknown};status:string;error?:string;result:Result|{message?:string}|null;kind:string;clarification?:{version:number;turns:{question:string;answer:string}[];can_answer:boolean;reason:string}}
type Run={id:string;question:string;status:string;version:number;state:{message?:string;model_calls:number;sql_calls:number;tokens:number;answers:string[]};usage?:{unknown_calls:number};artifacts:{kind:string;result_ref:string;data:Result}[];steps:{id:string;phase:string;action:string;status:string}[]}
const states:Record<string,string>={pending:'排队中',querying:'执行中',completed:'已完成',needs_clarification:'需要补充',failed:'执行失败',canceled:'已停止',unknown:'结果未知',running:'执行中'}
const actions:Record<string,string>={query_metrics:'查询指标',lookup_metrics:'查看口径',compare_periods:'比较两期',rank_declines:'选择下降分组',finish:'核验完成依据'}
const names:Record<string,string>={app_id:'应用',manufacturer:'设备厂商',channel:'推送通道',day:'发送日期',month:'发送月份',huawei:'华为',xiaomi:'小米',oppo:'OPPO',vivo:'vivo',apple:'Apple',huawei_push:'华为推送',xiaomi_push:'小米推送',oppo_push:'OPPO 推送',vivo_push:'vivo 推送',apns:'APNs',fcm:'FCM'}
const deviceMetrics=new Set(['devices','valid_devices','invalid_devices','unknown_devices','active_devices','active_valid_devices','active_rate'])
const format=(value:string|null|undefined)=>value===null||value===undefined?'无样本':Number(value).toLocaleString('zh-CN',{maximumFractionDigits:2})
const instant=(date:string)=>date+'T00:00:00+08:00'

function Evidence({result}:{result:Result}){
  const comparison=result.rows.some(r=>Object.values(r.values).some(v=>'delta'in v))
  return <article className="answer"><h3>{result.summary??'查询结果'}</h3>
    {result.is_synthetic&&<p className="badge">模拟数据 · 不代表真实厂商表现</p>}
    {result.period&&<p className="muted">{result.period.start} 至 {result.period.end_exclusive}（结束时刻不含）</p>}
    {result.totals&&<div className="metric-cards">{result.metrics.map(m=><div key={m.id}><strong>{m.name}</strong><div className="metric-value">{format(result.totals?.[m.id]?.value)}<span>{m.unit}</span></div></div>)}</div>}
    <div className="table-scroll"><table><thead><tr><th>分组</th><th>指标</th>{comparison?<><th>基期</th><th>报告期</th><th>差值</th><th>相对变化率</th></>:<th>结果</th>}</tr></thead><tbody>{result.rows.flatMap((row,i)=>result.metrics.map(m=>{const c=row.values[m.id];return <tr key={i+m.id}><td>{row.label}</td><td>{m.name}</td>{comparison?<><td>{format(c?.baseline)}</td><td>{format(c?.current)}</td><td>{format(c?.delta)} {c?.delta_unit}</td><td>{format(c?.rate)}{c?.rate!=null?'%':''}</td></>:<td>{format(c?.value)} {m.unit}</td>}</tr>}))}</tbody></table>{result.rows.length===0&&<p className="muted">该范围无可展示分组。</p>}</div>
    {result.limitations?.map(t=><p key={t} className="muted">{t}</p>)}
    <details><summary>指标口径与数据依据</summary>{result.metrics.map(m=><p key={m.id}><strong>{m.name}：</strong>{m.definition}</p>)}{result.sql&&<><pre>{result.sql}</pre><p>绑定参数：{JSON.stringify(result.parameters)}</p></>}</details>
  </article>
}

export default function PushWorkspace({api,storageKey}:{api:Api;storageKey:string}){
  const[caps,setCaps]=useState<Capabilities|null>(null),[enabled,setEnabled]=useState(false)
  const[workflow,setWorkflow]=useState('query'),[domain,setDomain]=useState('push'),[compare,setCompare]=useState(false)
  const[selected,setSelected]=useState(['sent','delivered','delivery_rate','clicked','click_rate']),[dimension,setDimension]=useState('channel')
  const[start,setStart]=useState('2026-07-01'),[end,setEnd]=useState('2026-08-01'),[baselineStart,setBaselineStart]=useState('2026-06-01'),[baselineEnd,setBaselineEnd]=useState('2026-07-01')
  const[hours,setHours]=useState(24),[asOf,setAsOf]=useState(''),[app,setApp]=useState(''),[vendor,setVendor]=useState('')
  const[top,setTop]=useState(false),[rankMetric,setRankMetric]=useState('delivery_rate'),[n,setN]=useState(3),[question,setQuestion]=useState('')
  const[task,setTask]=useState<Task|null>(null),[run,setRun]=useState<Run|null>(null),[history,setHistory]=useState<Task[]>([]),[answer,setAnswer]=useState('')
  const[busy,setBusy]=useState(false),[error,setError]=useState('')
  // 网络中断后重用同一提交标识，服务端不会把重试当成下一轮回答。
  const answerRequest=useRef<{id:string;version:number;answer:string;request_id:string}|null>(null)
  const refresh=()=>api<Task[]>('/tasks').then(setHistory)
  const load=async(id:string,kind:string,select=true)=>{if(kind==='analysis'){const value=await api<Run>('/analysis-runs/'+id);setTask(null);setRun(value)}else{const value=await api<Task>('/tasks/'+id);setRun(null);setTask(value);if(select){setWorkflow(value.input.push_plan?'query':'natural');setQuestion(value.input.question)}}localStorage.setItem(storageKey,JSON.stringify({id,kind}))}
  useEffect(()=>{void Promise.all([api<Capabilities>('/query-capabilities').then(setCaps),api<{enabled:boolean}>('/analysis-capabilities').then(c=>setEnabled(c.enabled)),refresh()]).then(()=>{const saved=localStorage.getItem(storageKey);if(saved){try{const {id,kind}=JSON.parse(saved);void load(id,kind).catch(e=>setError(e.message))}catch{localStorage.removeItem(storageKey)}}}).catch(e=>setError(e.message))},[storageKey])
  const current=run??task
  const contextVersion=run?.version??task?.clarification?.version??0
  useEffect(()=>{setAnswer('');answerRequest.current=null},[current?.id,contextVersion])
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
      if(!selected.length&&workflow!=='natural')throw new Error('至少选择一个指标')
      if((top||compare)&&!selected.includes(rankMetric))throw new Error('排名指标必须包含在所选指标中')
      const filters=[...(app?[{field:'app_id',op:'in',values:[app]}]:[]),...(vendor?[{field:'manufacturer',op:'in',values:[vendor]}]:[])]
      const plan={domain:'push',version:1,metrics:selected,dimensions:dimension?dimension.split(','):[],time_range:{start:instant(start),end_exclusive:instant(end)},observation_hours:hours,as_of:domain==='device'&&asOf?asOf+':00+08:00':null,filters}
      const label=`${domain==='device'?'设备':'推送'}分析：${start} 至 ${end}，${selected.map(k=>caps?.metrics.find(m=>m.id===k)?.name??k).join('、')}`
      if(workflow==='analysis'){
        const spec={domain:'push',goal:compare?'compare_push':domain==='device'?'device_summary':'push_summary',current:plan,baseline:compare?{start:instant(baselineStart),end_exclusive:instant(baselineEnd)}:null,rank_metric:compare||top?rankMetric:null,n:compare||top?n:null}
        const value=await api<Run>('/analysis-runs',{question:label,request_id:crypto.randomUUID(),spec});setRun(value);setTask(null);localStorage.setItem(storageKey,JSON.stringify({id:value.id,kind:'analysis'}))
      }else{
        if(workflow==='natural'&&!question.trim())throw new Error('请输入问题')
        const value=await api<Task>('/tasks',workflow==='natural'?{question}:{question:label,push_plan:plan});setTask(value);setRun(null);localStorage.setItem(storageKey,JSON.stringify({id:value.id,kind:'query'}))
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
  function newQuery(edit=false){if(edit&&task){setWorkflow('natural');setQuestion(task.input.question)}else setQuestion('');setTask(null);setRun(null);setAnswer('');setError('');answerRequest.current=null;localStorage.removeItem(storageKey)}
  const continuing=workflow==='natural'&&Boolean(task)
  const report=run?.artifacts.find(a=>a.kind==='report')?.data
  const result=task?.result&&'rows'in task.result?task.result:null
  return <section className="push-workspace">
    <div className="answer-heading"><div><h2>推送与设备分析</h2><p className="muted">查看发送、到达、点击和设备覆盖；支持自定义时间与两期比较。</p></div><button disabled={busy} onClick={()=>newQuery()}>新建查询</button></div>
    {caps?.is_synthetic&&<div className="notice">当前使用模拟数据。发送样本覆盖 2026 年 4—9 月，数据截止 {caps.data_cutoff}。通道表现为人工模拟。</div>}
    {error&&<div role="alert" className="error">{error}</div>}
    <div className="push-toolbar"><label>执行方式<select value={workflow} onChange={e=>setWorkflow(e.target.value)}><option value="query">表单查询（无需模型）</option><option value="analysis">工具分析与两期核查</option><option value="natural">自然语言查询</option></select></label><label>历史任务<select value={current?.id??''} onChange={e=>{const item=history.find(h=>h.id===e.target.value);if(item)void load(item.id,item.kind).catch(e=>setError(e.message))}}><option value="">选择任务</option>{history.map(t=><option key={t.id} value={t.id}>{states[t.status]} · {t.input.question.slice(0,45)}</option>)}</select></label></div>
    <form onSubmit={submit} className="analysis-form">
      {workflow==='natural'?<label>{continuing?'原问题':'问题'}<textarea rows={3} value={continuing?task!.input.question:question} readOnly={continuing} onChange={e=>setQuestion(e.target.value)} maxLength={2000} placeholder="例如：查询 app_1 在2026年7月各通道发送量与到达率，到达观察时长为24小时" required/></label>:<>
        <div className="push-controls"><label>数据粒度<select value={domain} onChange={e=>changeDomain(e.target.value)}><option value="push">逻辑推送</option><option value="device">应用设备</option></select></label><label>{domain==='device'?'活跃期间开始':'发送期间开始'}<input type="date" value={start} onChange={e=>setStart(e.target.value)} required/></label><label>结束日期（不含）<input type="date" value={end} onChange={e=>setEnd(e.target.value)} required/></label>
        {domain==='push'?<label>发送后的回执观察时长（小时）<input type="number" min={1} max={8760} value={hours} onChange={e=>setHours(Number(e.target.value))} required/></label>:<label>设备状态时点（可选，上海时区）<input type="datetime-local" value={asOf} onChange={e=>setAsOf(e.target.value)}/><small>留空使用查询期末前一瞬间</small></label>}
        <label>分组<select value={dimension} onChange={e=>setDimension(e.target.value)}><option value="">整体</option><option value="app_id">应用</option><option value="manufacturer">设备厂商</option><option value="app_id,manufacturer">应用与设备厂商</option>{domain==='push'&&<><option value="channel">实际推送通道</option><option value="app_id,channel">应用与通道</option><option value="day">发送日期</option><option value="month">发送月份</option></>}</select></label><label>应用 ID（可选）<input value={app} onChange={e=>setApp(e.target.value)} maxLength={100} placeholder="例如 app_1"/></label><label>设备厂商<select value={vendor} onChange={e=>setVendor(e.target.value)}><option value="">全部授权厂商</option>{caps?.enums.manufacturer?.map(v=><option key={v} value={v}>{names[v]??v}</option>)}</select></label></div>
        <fieldset className="metric-options"><legend>指标</legend>{caps?.metrics.filter(m=>deviceMetrics.has(m.id)===(domain==='device')).map(m=><label key={m.id} title={m.definition}><input type="checkbox" checked={selected.includes(m.id)} onChange={e=>setSelected(e.target.checked?[...selected,m.id]:selected.filter(k=>k!==m.id))}/>{m.name}</label>)}</fieldset>
        {workflow==='analysis'&&<><div className="push-checks">{domain==='push'&&<label><input type="checkbox" checked={compare} onChange={e=>{setCompare(e.target.checked);setTop(false)}}/>比较两期并排名下降分组</label>}{!compare&&<label><input type="checkbox" checked={top} onChange={e=>setTop(e.target.checked)}/>选取 Top N 并汇总</label>}</div>{compare&&<div className="push-controls"><label>基期开始<input type="date" value={baselineStart} onChange={e=>setBaselineStart(e.target.value)} required/></label><label>基期结束（不含）<input type="date" value={baselineEnd} onChange={e=>setBaselineEnd(e.target.value)} required/></label></div>}{(compare||top)&&<div className="push-controls"><label>排名指标<select value={rankMetric} onChange={e=>setRankMetric(e.target.value)}>{selected.map(id=><option key={id} value={id}>{caps?.metrics.find(m=>m.id===id)?.name??id}</option>)}</select></label><label>前 N 组<input type="number" min={1} max={10} value={n} onChange={e=>setN(Number(e.target.value))}/></label><p className="muted">下降按原始差值排序；比率差值使用百分点。</p></div>}</>}
      </>}
      {workflow==='analysis'&&!enabled&&<p className="muted">工具分析需配置模型与预算；表单查询可独立使用。</p>}
      {continuing?<><p className="muted">补充条件请在下方追问处回答，系统会保留原问题。</p><button type="button" disabled={busy} onClick={()=>newQuery(true)}>修改原问题并新建查询</button></>:<button className="primary" disabled={busy||!caps||(workflow==='analysis'&&!enabled)}>{busy?'正在提交…':workflow==='analysis'?'开始工具分析':'查询指标'}</button>}
    </form>
    {current&&<><div className="task-progress"><strong>{states[current.status]??current.status}</strong><span>任务 {current.id.slice(0,8)}</span>{['pending','querying','needs_clarification'].includes(current.status)&&<button onClick={async()=>{try{await api('/tasks/'+current.id+'/stop',{});await load(current.id,run?'analysis':'query')}catch(e){setError((e as Error).message)}}}>停止</button>}</div>
      {task?.error&&<p className="error">{task.error}</p>}
      {task&&Boolean(task.clarification?.turns.length)&&<article aria-label="已提交的补充记录"><h3>补充记录</h3>{task.clarification!.turns.map((turn,index)=><div key={index}><p className="notice"><strong>助手追问：</strong>{turn.question}</p><p><strong>你的补充：</strong>{turn.answer}</p></div>)}</article>}
      {task?.status==='needs_clarification'&&task.result&&'message'in task.result&&task.result.message&&<div className="notice"><h3>需要你补充</h3><p>{task.result.message}</p>{task.clarification?.can_answer?<form onSubmit={supplement} className="analysis-form"><label>回答这条追问<textarea autoFocus rows={3} value={answer} onChange={e=>setAnswer(e.target.value)} maxLength={1000} placeholder="例如：观察时长24小时，统计2026年8月1日至10月1日（结束不含）" required/></label><p className="muted">只需回答缺少的条件，无需重复原问题。最多补充2次，每次请在24小时内回复。</p><button className="primary" disabled={busy||!answer.trim()}>{busy?'正在提交…':'补充并继续查询'}</button></form>:<p className="muted">{task.clarification?.reason||'请使用表单明确查询条件。'}</p>}</div>}
      {run&&<><p className="muted">模型调用 {run.state.model_calls} · SQL {run.state.sql_calls} · Token 预留与已知消耗 {run.state.tokens}</p>{run.state.message&&<p className="notice">{run.state.message}</p>}{Boolean(run.usage?.unknown_calls)&&<p className="notice">存在用量未知调用，保留预留，不自动重发。</p>}{run.status==='needs_clarification'&&<form onSubmit={supplement}><label>补充条件<textarea value={answer} onChange={e=>setAnswer(e.target.value)} maxLength={1000} required/></label><button disabled={busy}>补充并继续</button></form>}{run.status==='failed'&&<p className="notice">任务未完整完成；下方仅保留已取得的证据。</p>}</>}
      {result&&<Evidence result={result}/>} {report&&<Evidence result={report}/>}
      {run&&<><details open><summary>执行步骤</summary><ol>{run.steps.map(s=><li key={s.id}>{actions[s.action]??(s.phase==='decide'?'模型选择下一步':'准备动作')} · {states[s.status]??s.status}</li>)}</ol></details><details><summary>已保存证据</summary>{run.artifacts.filter(a=>a.kind!=='report').map(a=><details key={a.result_ref}><summary>{a.kind} · {a.result_ref.slice(0,12)}</summary>{a.kind==='query'?<Evidence result={a.data}/>:<pre>{JSON.stringify(a.data,null,2)}</pre>}</details>)}</details></>}
    </>}
  </section>
}
