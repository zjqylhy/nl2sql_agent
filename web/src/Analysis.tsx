import {useEffect,useState} from 'react'
import QueryResult,{type SemanticResult} from './QueryResult'

type Artifact={kind:string;result_ref:string;metrics_used:string[];data:Record<string,unknown>}
type Run={id:string;question:string;status:string;version:number;metadata:{analysis_date:string};policy:{currency:string;max_cost:number};state:{phase:string;message:string;model_calls:number;sql_calls:number;tokens:number;cost:number;answers:string[];feedback:{evidence_expired?:boolean}};usage?:{unknown_calls:number};artifacts:Artifact[];steps:{id:string;sequence:number;phase:string;action:string|null;status:string;error_code:string|null}[]}
type History={id:string;question:string;status:string}
type Cell={baseline:string|null;current:string|null;delta:string|null;rate:string|null}
type ComparisonRow={state:string;values:Record<string,Cell>}
type Api=<T>(path:string,body?:unknown,method?:string)=>Promise<T>
const names:Record<string,string>={pending:'排队中',querying:'执行中',completed:'已完成',needs_clarification:'等待补充',failed:'未完成',canceled:'已停止',decide:'模型选择下一步',tool:'执行工具',query_metrics:'查询指标',lookup_metrics:'查询指标定义',compare_periods:'比较两期',rank_declines:'排名下降州',calculate:'确定性计算',finish:'检查完成依据',request_clarification:'请求补充',running:'执行中',unknown:'结果未知',gmv:'GMV',orders:'有效订单量',aov:'客单价'}
const display=(v:unknown)=>v===null||v===undefined?'无数据':String(v)
const number=(v:unknown)=>v===null||v===undefined?'无数据':Number(v).toLocaleString('zh-CN',{maximumFractionDigits:2})
const percentage=(v:unknown)=>v===null||v===undefined?'无数据':Number(v).toLocaleString('zh-CN',{style:'percent',maximumFractionDigits:2})

export default function AnalysisWorkspace({api,storageKey}:{api:Api;storageKey:string}){
  const[run,setRun]=useState<Run|null>(null),[history,setHistory]=useState<History[]>([])
  const[question,setQuestion]=useState(''),[answer,setAnswer]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('')
  const[enabled,setEnabled]=useState<boolean|null>(null)
  const[modelMode,setModelMode]=useState('provider')
  const loadHistory=()=>api<History[]>('/analysis-runs').then(setHistory)
  const load=async(id:string)=>{try{const value=await api<Run>('/analysis-runs/'+id);setRun(value);localStorage.setItem(storageKey,id)}catch(e){setRun(null);setError((e as Error).message)}}
  useEffect(()=>{void api<{enabled:boolean;model_mode?:string}>('/analysis-capabilities').then(async c=>{setEnabled(c.enabled);setModelMode(c.model_mode??'provider');if(c.enabled){await loadHistory();const id=localStorage.getItem(storageKey);if(id)await load(id)}}).catch(e=>setError((e as Error).message))},[storageKey])
  useEffect(()=>{
    if(!run||!['pending','querying'].includes(run.status))return
    const id=run.id,source=new EventSource('/api/tasks/'+id+'/events');let alive=true
    const refresh=()=>{if(alive)void load(id).then(()=>loadHistory()).catch(()=>{})}
    for(const event of ['step','accepted','error','done','canceled','clarification'])source.addEventListener(event,refresh)
    source.onerror=refresh
    const timer=setInterval(refresh,5000)
    return()=>{alive=false;source.close();clearInterval(timer)}
  },[run?.id,run?.status])
  async function submit(event:React.FormEvent){
    event.preventDefault();if(!question.trim()||busy)return;setBusy(true);setError('')
    try{const value=await api<Run>('/analysis-runs',{question,request_id:crypto.randomUUID()});setRun(value);localStorage.setItem(storageKey,value.id);await loadHistory()}catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  async function supplement(event:React.FormEvent){
    event.preventDefault();if(!run||!answer.trim()||busy)return;setBusy(true);setError('')
    try{setRun(await api<Run>('/analysis-runs/'+run.id+'/answers',{answer,request_id:crypto.randomUUID(),expected_version:run.version}));setAnswer('')}catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  async function stop(){if(!run)return;setBusy(true);try{await api('/tasks/'+run.id+'/stop',{});await load(run.id);await loadHistory()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  const report=run?.artifacts.find(a=>a.kind==='report')?.data
  const rows=(report?.rows??[]) as ComparisonRow[]
  return <section className="analysis-panel">
    <div className="answer-heading"><h2>有限经营分析</h2><button onClick={()=>{setRun(null);setQuestion('');localStorage.removeItem(storageKey)}}>新建分析</button></div>
    <p className="muted">比较两期 GMV 下降州，核查订单量与客单价；或汇总 GMV 前 N 州。指标变化用于描述性分析。</p>
    {modelMode==='simulated'&&<div className="notice">模拟模型演示：查询与计算使用实际数据，工具选择由演示适配器模拟，不代表真实模型效果。</div>}
    {enabled===false&&<div className="notice">经营分析尚未启用。需配置不可变数据版本、模型及费用预算；原问数可继续使用。</div>}
    {error&&<p role="alert" className="error">{error}</p>}
    {history.length>0&&<label>分析历史<select value={run?.id??''} onChange={e=>{setError('');if(e.target.value)void load(e.target.value)}}><option value="">选择历史任务</option>{history.map(h=><option key={h.id} value={h.id}>{names[h.status]} · {h.question.slice(0,50)}</option>)}</select></label>}
    {!run?<form onSubmit={submit} className="analysis-form"><label>分析问题<textarea value={question} onChange={e=>setQuestion(e.target.value)} maxLength={2000} rows={4} placeholder="例如：比较 2018 年 6 月和 7 月各州 GMV，按下降金额取前三，核查订单量和客单价"/></label><div className="examples">{['2018 年 7 月 GMV 前 3 个州的 GMV 合计是多少？','比较 2018 年 6 月和 7 月各州 GMV，按下降金额取前三，并核查订单量和客单价'].map(q=><button key={q} type="button" onClick={()=>setQuestion(q)}>{q}</button>)}</div><button className="primary" disabled={!enabled||busy||!question.trim()}>{busy?'受理中…':'开始分析'}</button></form>:<>
      <div className="question-bubble">{run.question}</div><div className="task-progress"><strong>{names[run.status]??run.status}</strong><span>基准 {run.metadata.analysis_date}</span>{['pending','querying','needs_clarification'].includes(run.status)&&<button disabled={busy} onClick={()=>void stop()}>停止任务</button>}</div>
      <p className="muted">模型调用 {run.state.model_calls}/10 · SQL {run.state.sql_calls}/6 · Token 计费预留与已知消耗 {run.state.tokens}/30000 · 预算估算 {(run.state.cost/1000000).toFixed(6)} / {(run.policy.max_cost/1000000).toFixed(6)} {run.policy.currency}</p>
      {Boolean(run.usage?.unknown_calls)&&<div className="notice">有 {run.usage?.unknown_calls} 次调用的用量尚未确认；上述预算包含其预留。</div>}
      {run.state.message&&<div className={'notice '+(run.status==='failed'?'error':'')}>{run.state.message}</div>}
      {run.state.feedback?.evidence_expired&&<div className="notice">详细步骤与原始依据已超过 30 天保留期；保留最终报告与汇总用量，任务不能恢复。</div>}
      {run.status==='failed'&&<div className="notice">这是部分执行记录，不代表完整分析成功。已完成的依据保留在下方。</div>}
      {run.status==='needs_clarification'&&<form onSubmit={supplement} className="analysis-form"><label>补充条件<textarea rows={2} value={answer} maxLength={1000} onChange={e=>setAnswer(e.target.value)}/></label><button className="primary" disabled={busy||!answer.trim()}>补充并继续</button></form>}
      {report&&<article className="answer"><h3>分析结果</h3><p>{display(report.summary)}</p>
        <p className="muted">报告期：{display((report.current as Record<string,unknown>)?.start)} 至 {display((report.current as Record<string,unknown>)?.end_exclusive)}（不含结束日） · {display(report.current_days)} 天{report.baseline?`；基期：${display((report.baseline as Record<string,unknown>).start)} 至 ${display((report.baseline as Record<string,unknown>).end_exclusive)}（不含结束日） · ${display(report.baseline_days)} 天；按下降${report.ranking==='rate'?'比例':'金额'}排名`:''}</p>
        {Array.isArray(report.narrative)&&report.narrative.map((point:Record<string,unknown>,i:number)=><p key={i}>{display(point.text)} <small className="muted">依据 {display(point.result_ref).slice(0,12)}</small></p>)}
        {report.total&&typeof report.total==='object'?<div className="metric-value">{number((report.total as Record<string,unknown>).value)}<span> BRL · 选中州 GMV 合计</span></div>:null}
        {report.goal==='top_n_total'&&<><p>所选州：{display((report.total as Record<string,unknown>).states)}</p>{report.extras&&typeof report.extras==='object'&&Object.entries(report.extras).map(([key,value])=><p key={key}>{key==='share'?'占全范围 GMV 比例':names[key]}：{key==='share'?percentage((value as Record<string,unknown>).value):number((value as Record<string,unknown>).value)} {key==='share'?'':display((value as Record<string,unknown>).unit)}</p>)}</>}
        {report.goal==='compare_declines'&&rows.length>0&&rows[0].values&&<div className="table-scroll"><table><thead><tr><th>州</th><th>指标</th><th>基期</th><th>报告期</th><th>差值</th><th>变化率</th></tr></thead><tbody>{rows.flatMap(row=>['gmv','orders','aov'].filter(metric=>row.values[metric]).map(metric=>{const c=row.values[metric];return <tr key={row.state+metric}><td>{row.state}</td><td>{names[metric]}</td><td>{number(c.baseline)}</td><td>{number(c.current)}</td><td>{number(c.delta)}</td><td>{c.rate===null?'不可计算':number(c.rate)+'%'}</td></tr>}))}</tbody></table></div>}
      </article>}
      <details open><summary>执行步骤与状态</summary><ol>{run.steps.map(step=><li key={step.id}>{step.phase==='decide'?'模型选择下一步':step.phase==='fixed'?'固定流程选择下一步':names[step.action??'']??'工具'} · {names[step.status]??step.status}{step.error_code?' · '+step.error_code:''}</li>)}</ol></details>
      <div className="analysis-evidence"><h3>数据依据</h3>{run.artifacts.map(a=><details key={a.result_ref}><summary>{a.kind} · {a.metrics_used.map(m=>names[m]??m).join('、')} · {a.result_ref.slice(0,12)}</summary>{a.kind==='query'?<QueryResult result={a.data as unknown as SemanticResult}/>:<pre>{JSON.stringify(a.data,null,2)}</pre>}</details>)}</div>
    </>}
  </section>
}
