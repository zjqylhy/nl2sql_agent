import {useEffect,useRef,useState} from 'react'
import QueryResult,{type SemanticResult} from './QueryResult'

type Plan={metrics:string[];dimensions:string[];time_range:{start:string;end_exclusive:string};filters:{field:string;op:string;values:string[]}[];order_by:{field:string;direction:string}[];top_n:number|null}
export type Conversation={id:string;title:string;version:number;turn_count:number;analysis_date:string;plan:Plan|null;pending:{missing:string[]}|null;active_task_id:string|null;updated_at:string}
type Turn={id:string;request_id?:string;conversation_turn:number;status:string;input?:{question:string};error?:string;result?:SemanticResult&{message?:string;multiturn?:{changes:{field:string}[];notes:string[]}}}
type API=<T>(path:string,body?:unknown,method?:string)=>Promise<T>
const names:Record<string,string>={gmv:'GMV',orders:'有效订单量',aov:'客单价',cancellation_rate:'取消率',delivery_days:'平均交付时长',review_score:'平均评分',state:'州',month:'月份',day:'日期',status:'订单状态',amount:'商品金额',metrics:'指标',dimensions:'分组',time_range:'时间',filters:'筛选',order_by:'排序',top_n:'Top N',states:'具体州代码',expression:'完整修改条件'}
const statuses:Record<string,string>={pending:'排队中',querying:'正在取数',completed:'已完成',needs_clarification:'需要补充',failed:'执行失败',canceled:'已停止',unavailable:'权限变化，结果不可查看'}

export default function ConversationWorkspace({id,onSelect,onChanged,api,metrics}:{id:string|null;onSelect:(id:string)=>void;onChanged:()=>void;api:API;metrics:{id:string;name:string}[]}){
  const[c,setConversation]=useState<Conversation|null>(null),[turns,setTurns]=useState<Turn[]>([])
  const[question,setQuestion]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('')
  const[metric,setMetric]=useState(''),[period,setPeriod]=useState(''),[group,setGroup]=useState('')
  const[start,setStart]=useState(''),[end,setEnd]=useState(''),[composeVersion,setComposeVersion]=useState<number|null>(null)
  const pendingRequest=useRef<{id:string;version:number;signature:string}|null>(null)
  const selected=useRef(id),fetching=useRef(false),created=useRef<string|null>(null)
  selected.current=id
  const active=!!c?.active_task_id
  const clear=()=>{setQuestion('');setMetric('');setPeriod('');setGroup('');setStart('');setEnd('');setComposeVersion(null);pendingRequest.current=null}

  async function reload(target=id){
    if(!target||fetching.current)return
    fetching.current=true
    try{
      const data=await api<{conversation:Conversation;tasks:Turn[]}>('/conversations/'+target)
      if(selected.current!==target)return
      setConversation(data.conversation);setTurns(data.tasks)
      if(pendingRequest.current&&data.tasks.some(t=>t.request_id===pendingRequest.current?.id))clear()
    }catch(e){if(selected.current===target)setError((e as Error).message)}finally{fetching.current=false}
  }
  useEffect(()=>{
    // 首次提交创建会话时保留输入与请求 ID，网络重发仍指向同一轮。
    if(created.current===id&&id)created.current=null
    else{setConversation(null);setTurns([]);setError('');clear()}
    void reload(id);const timer=setInterval(()=>void reload(id),3000);return()=>clearInterval(timer)
  },[id])
  useEffect(()=>{
    if(!c?.active_task_id)return
    const source=new EventSource('/api/tasks/'+c.active_task_id+'/events')
    const update=()=>{void reload();onChanged()}
    for(const kind of ['accepted','tool_start','retry','done','error','clarification','canceled'])source.addEventListener(kind,update)
    source.onerror=update
    return()=>source.close()
  },[c?.active_task_id,id])

  async function submit(event?:React.FormEvent){
    event?.preventDefault();if(!question.trim()||busy||active)return
    setBusy(true);setError('')
    try{
      let current=c,target=id
      if(!target){current=await api<Conversation>('/conversations',{});target=current.id;created.current=target;selected.current=target;onSelect(target);setConversation(current)}
      if(!current){const data=await api<{conversation:Conversation}>('/conversations/'+target);current=data.conversation}
      const input={question,...(metric?{metric}:{}),...(period?{period}:{}),...(group?{group_by:group}:{}),...(period==='custom'?{start,end}:{})}
      const signature=JSON.stringify(input)
      if(pendingRequest.current?.signature!==signature)pendingRequest.current={id:crypto.randomUUID(),version:composeVersion??current.version,signature}
      const pending=pendingRequest.current
      const task=await api<Turn>('/conversations/'+target+'/turns',{...input,request_id:pending.id,expected_version:pending.version})
      if(selected.current===target){setTurns(old=>old.some(t=>t.id===task.id)?old:[...old,task]);clear();await reload(target)}
      onChanged()
    }catch(e){setError((e as Error).message);await reload()}finally{setBusy(false)}
  }
  function edited(){if(composeVersion===null)setComposeVersion(c?.version??0)}
  async function discard(){if(!c)return;setBusy(true);setError('');try{await api('/conversations/'+c.id+'/discard-pending',{expected_version:c.version});await reload();onChanged()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function stop(){if(!c?.active_task_id)return;try{await api('/tasks/'+c.active_task_id+'/stop',{});await reload();onChanged()}catch(e){setError((e as Error).message)}}
  const changed=composeVersion!==null&&c!==null&&composeVersion!==c.version
  const examples=['最近半年 GMV 按月统计','本月 GMV 和有效订单量按州统计','本月 GMV 按州排名，取前 5 名']
  return <>
    {error&&<p className="error" role="alert">{error}</p>}
    {c?.plan&&<aside className="context-panel" aria-label="当前查询条件"><strong>当前查询条件</strong><span>{c.plan.metrics.map(m=>names[m]??m).join('、')} · {c.plan.dimensions.length?'按'+c.plan.dimensions.map(d=>names[d]??d).join('、'):'整体'}</span><span>{c.plan.time_range.start} 至 {c.plan.time_range.end_exclusive}（不含结束日）</span>{c.plan.filters.length>0&&<span>{c.plan.filters.map(f=>(names[f.field]??f.field)+' '+f.op+' '+f.values.join('、')).join('；')}</span>}<small>相对日期以分析基准 {c.analysis_date} 为准；追问继承未修改条件。</small></aside>}
    <section className="conversation" aria-label="多轮问数历史">
      {turns.length===0?<div className="welcome"><div className="welcome-icon">↗</div><h2>从一个问题开始，接着追问。</h2><p>例如先按月统计，再说“改成按州”。缺少条件时会请你补充。</p><div className="examples">{examples.map(q=><button key={q} onClick={()=>{setQuestion(q);edited()}}>{q}<span>↗</span></button>)}</div></div>:turns.map(t=><article className="conversation-turn" key={t.id}>
        {t.input&&<div className="question-bubble">{t.input.question}</div>}
        <div className="task-progress"><i className={'dot '+t.status}/><strong>{statuses[t.status]??t.status}</strong><span>第 {t.conversation_turn} 轮</span></div>
        {['pending','querying'].includes(t.status)&&<div className="skeleton"><div/><div/></div>}
        {t.status==='needs_clarification'&&<div className="notice">{t.result?.message??'请补充查询条件。'}</div>}
        {t.status==='failed'&&<div className="notice error">{t.error??'执行失败，请重新编辑追问。'}<button onClick={()=>{setQuestion(t.input?.question??'');setComposeVersion(c?.version??0)}}>重新编辑</button></div>}
        {t.status==='completed'&&t.result&&<><div className="turn-changes">{t.result.multiturn?.changes.length?'本轮修改：'+t.result.multiturn.changes.map(x=>names[x.field]??x.field).join('、'):'沿用当前条件重新取数'}{t.result.multiturn?.notes.map(n=><span key={n}>{n}</span>)}</div><QueryResult result={t.result}/></>}
      </article>)}
    </section>
    {c?.pending&&<div className="notice pending-draft"><strong>等待补充</strong><p>{c.pending.missing.map(k=>names[k]??k).join('、')}。已明确的条件保留在草稿中。</p><button disabled={busy||active} onClick={discard}>取消这次修改</button></div>}
    {changed&&<div className="notice" role="alert">会话已有更新，请确认上方当前条件。<button onClick={()=>{setComposeVersion(c?.version??0);pendingRequest.current=null;setError('')}}>按当前条件继续</button></div>}
    <form className="composer" onSubmit={submit}><div className="controls">
      <label>指标<select disabled={active} value={metric} onChange={e=>{edited();setMetric(e.target.value)}}><option value="">继承 / 从问题识别</option>{metrics.map(m=><option key={m.id} value={m.id}>{m.name}</option>)}</select></label>
      <label>时间<select disabled={active} value={period} onChange={e=>{edited();setPeriod(e.target.value)}}><option value="">继承 / 从问题识别</option>{[['today','今天'],['yesterday','昨日'],['this_week','本周'],['last_week','上周'],['this_month','本月'],['last_month','上月'],['custom','指定日期']].map(([v,n])=><option key={v} value={v}>{n}</option>)}</select></label>
      <label>分组<select disabled={active} value={group} onChange={e=>{edited();setGroup(e.target.value)}}><option value="">继承 / 从问题识别</option>{[['state','州'],['day','日期'],['month','月份']].map(([v,n])=><option key={v} value={v}>{n}</option>)}</select></label>
      {period==='custom'&&<><label>开始<input type="date" disabled={active} value={start} onChange={e=>{edited();setStart(e.target.value)}} required/></label><label>结束（不含）<input type="date" disabled={active} value={end} onChange={e=>{edited();setEnd(e.target.value)}} required/></label></>}
    </div><div className="input-row"><textarea aria-label="问数或追问" disabled={active} value={question} onChange={e=>{edited();setQuestion(e.target.value)}} placeholder={c?.pending?'补充缺少的条件，例如：SP 和 RJ，按 GMV 从高到低':c?.plan?'继续追问，例如：改成按州':'例如：最近半年 GMV 按月统计'} rows={2} maxLength={2000} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();void submit()}}}/><button className="primary" disabled={busy||active||!question.trim()||metrics.length===0}>{busy?'受理中…':active?'正在执行…':'发送 ↗'}</button>{active&&<button type="button" onClick={stop}>停止</button>}</div><small>Enter 发送 · Shift + Enter 换行 · 同一会话一次处理一条追问 · 最多 30 轮</small></form>
  </>
}
