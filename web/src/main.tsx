import React,{lazy,Suspense,useEffect,useState} from 'react'
import {createRoot} from 'react-dom/client'
import './style.css'
import QueryResult,{type SemanticResult} from './QueryResult'
const Chart=lazy(()=>import('./Chart'))

type User={username:string;tenant_id:string;role:string;allowed_metrics:string[]}
type Metric={id:string;name:string;version:string}
type Row={label:string;value:string|number|null;sample_size:number;total_orders:number;excluded_orders:number;unavailable_orders:number}
type Result={metric:string;metric_name:string;definition:string;unit:string;analysis_date:string;period:{start:string;end_exclusive:string};rows:Row[];sql:string;parameters:string[];usage:{parser:string;tokens:number|null};message:string}
type Task={id:string;status:string;input:{question:string};result:Result|SemanticResult|{message:string}|null;error?:string;created_at:string}
const statusNames:Record<string,string>={pending:'排队中',querying:'正在取数',completed:'已完成',needs_clarification:'需要澄清',failed:'执行失败',canceled:'已停止'}

async function api<T>(path:string,body?:unknown,method?:string):Promise<T>{
  const response=await fetch('/api'+path,{method:method??(body?'POST':'GET'),credentials:'same-origin',headers:body?{'Content-Type':'application/json'}:undefined,body:body?JSON.stringify(body):undefined})
  const data=await response.json()
  if(!response.ok)throw new Error(data.error??'请求失败')
  return data
}

function App(){
  const[user,setUser]=useState<User|null>(null),[metrics,setMetrics]=useState<Metric[]>([]),[tasks,setTasks]=useState<Task[]>([]),[task,setTask]=useState<Task|null>(null)
  const[loading,setLoading]=useState(true),[busy,setBusy]=useState(false),[error,setError]=useState('')
  const[question,setQuestion]=useState(''),[metric,setMetric]=useState(''),[period,setPeriod]=useState(''),[group,setGroup]=useState(''),[start,setStart]=useState(''),[end,setEnd]=useState('')
  const[tenant,setTenant]=useState(''),[username,setUsername]=useState(''),[password,setPassword]=useState('')
  const[showUsers,setShowUsers]=useState(false)
  const refresh=async()=>{setTasks(await api<Task[]>('/tasks'))}
  useEffect(()=>{api<User>('/me').then(setUser).catch(()=>{}).finally(()=>setLoading(false))},[])
  useEffect(()=>{if(!user)return;api<Metric[]>('/metrics').then(setMetrics).catch(e=>setError(e.message));refresh().catch(e=>setError(e.message));const timer=setInterval(()=>refresh().catch(()=>{}),5000);return()=>clearInterval(timer)},[user])
  useEffect(()=>{
    if(!task||!['pending','querying'].includes(task.status))return
    const id=task.id
    const source=new EventSource('/api/tasks/'+id+'/events')
    let disposed=false
    const update=()=>api<Task>('/tasks/'+id).then(t=>{if(!disposed)setTask(t)}).catch(e=>{if(!disposed){setError(e.message);source.close()}})
    for(const kind of ['accepted','tool_start','retry','done','error','clarification','canceled'])source.addEventListener(kind,()=>{void update();void refresh().catch(()=>{})})
    source.onerror=()=>{void update()}
    return()=>{disposed=true;source.close()}
  },[task?.id,task?.status])

  async function login(event:React.FormEvent){event.preventDefault();setBusy(true);setError('');try{setUser(await api<User>('/login',{tenant,username,password}));setPassword('')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function submit(event?:React.FormEvent){event?.preventDefault();if(!question.trim()||busy)return;setBusy(true);setError('');try{setTask(await api<Task>('/tasks',{question,...(metric?{metric}:{}),...(period?{period}:{}),...(group?{group_by:group}:{}),...(period==='custom'?{start,end}:{})}));await refresh()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function stop(){if(!task)return;try{await api('/tasks/'+task.id+'/stop',{});setTask(await api<Task>('/tasks/'+task.id));await refresh()}catch(e){setError((e as Error).message)}}
  async function logout(){try{await api('/logout',{});setUser(null);setTasks([]);setTask(null)}catch(e){setError((e as Error).message)}}
  const result=task?.result&&'metric'in task.result?task.result:null
  const semantic=task?.result&&'result_version'in task.result&&task.result.result_version===2?task.result:null
  if(loading)return <div className="loading">正在连接工作台…</div>
  if(!user)return <main className="login-page"><section className="login-intro"><div className="brand-mark">O</div><p className="eyebrow">OLIST · BUSINESS ANALYTICS</p><h1>让数据回答<br/>经营问题。</h1><p>在统一口径下查询指标，每一次结果都有数据依据。</p><div className="intro-note">巴西电商历史数据 · 受限只读查询</div></section><form className="login-form" onSubmit={login}><h2>登录工作台</h2><p className="muted">使用管理员分配的租户与账号</p><label>租户<input value={tenant} onChange={e=>setTenant(e.target.value)} required autoComplete="organization" placeholder="租户标识"/></label><label>账号<input value={username} onChange={e=>setUsername(e.target.value)} required autoComplete="username" placeholder="你的账号"/></label><label>密码<input type="password" value={password} onChange={e=>setPassword(e.target.value)} required autoComplete="current-password"/></label>{error&&<p role="alert" className="error">{error}</p>}<button className="primary" disabled={busy}>{busy?'正在登录…':'进入工作台'}</button></form></main>
  return <div className="app-shell">
    <aside className="sidebar"><div className="brand"><span className="brand-mark">O</span><span>经营分析<span className="brand-sub">Olist 工作台</span></span></div><button className="new-task" onClick={()=>{setTask(null);setQuestion('');setError('')}}>＋ 新建问数</button><div className="sidebar-label">最近任务</div><nav aria-label="历史任务">{tasks.length===0?<p className="empty-history">还没有任务，开始第一次问数</p>:tasks.map(t=><button key={t.id} className={'history-item '+(t.id===task?.id?'selected':'')} onClick={()=>api<Task>('/tasks/'+t.id).then(setTask).catch(e=>setError(e.message))}><span>{t.input.question}</span><small><i className={'dot '+t.status}/>{statusNames[t.status]} · {new Date(t.created_at).toLocaleDateString('zh-CN')}</small></button>)}</nav><div className="account"><strong>{user.username}</strong><small>{user.tenant_id} · {({admin:'管理员',data:'数据人员',ops:'运营人员'} as Record<string,string>)[user.role]}</small><div>{user.role==='admin'&&<button onClick={()=>setShowUsers(!showUsers)}>账号权限</button>}<button onClick={logout}>退出</button></div></div></aside>
    <main className="workspace"><header><div><p className="eyebrow">ANALYSIS WORKSPACE</p><h1>问数工作台</h1></div><span className="badge">Olist · 历史数据</span></header>
      {error&&<div role="alert" className="error banner">{error}<button onClick={()=>setError('')}>关闭</button></div>}
      {showUsers?<Users onError={setError}/>:<>
      <section className="conversation">
        {!task?<div className="welcome"><div className="welcome-icon">↗</div><h2>你想了解哪项经营指标？</h2><p>用自然语言提问，或选择指标和时间。结果按已发布口径计算。</p><div className="examples">{['本月 GMV 和有效订单量按州统计','最近 6 个月 GMV 和客单价按月统计','本月 GMV 按州排名，取前 5 名'].map(q=><button key={q} onClick={()=>{setQuestion(q);setMetric('');setPeriod('');setGroup('')}}>{q} <span>↗</span></button>)}</div><div className="scope-note"><strong>当前可查询</strong><span>{metrics.map(m=>m.name).join(' · ')||'暂无获授权指标'}</span><small>相对日期以结果中的分析基准日期为准。归因与报告能力将在后续里程碑交付。</small></div></div>:<>
          <div className="question-bubble">{task.input.question}</div><div className="task-progress"><i className={'dot '+task.status}/><strong>{statusNames[task.status]}</strong><span>任务 {task.id.slice(0,8)}</span>{['pending','querying'].includes(task.status)&&<button onClick={stop}>停止任务</button>}</div>
          {['pending','querying'].includes(task.status)&&<div className="skeleton" aria-label="正在处理任务"><div/><div/><div/></div>}
          {task.status==='failed'&&<div className="notice error">{task.error||'执行失败，请检查数据源或稍后重试。'}<button onClick={()=>setQuestion(task.input.question)}>重新编辑</button></div>}
          {task.status==='needs_clarification'&&<div className="notice"><strong>需要补充信息</strong><p>{task.result&&'message'in task.result?task.result.message:''}</p><button onClick={()=>setQuestion(task.input.question)}>在下方补充指标与时间</button></div>}
          {task.status==='canceled'&&<div className="notice">任务已停止。已完成的结果仍保留在任务记录中。</div>}
          {semantic&&<QueryResult result={semantic}/>}
          {result&&<article className="answer"><div className="answer-heading"><h2>{result.metric_name}</h2><span className="badge">口径 v1 · {result.usage.parser==='llm'?'模型解析':'规则解析'}</span></div><p className="muted">{result.period.start} 至 {result.period.end_exclusive}（不含结束日） · 分析基准 {result.analysis_date}</p>
            {result.rows.length===1&&result.rows[0].label==='整体'?<div className="metric-value">{result.rows[0].value===null?'无数据':Number(result.rows[0].value).toLocaleString('zh-CN',{maximumFractionDigits:2})}<span>{result.unit}</span></div>:result.rows.length>0?<Suspense fallback={<p className="muted">正在加载图表…</p>}><Chart rows={result.rows} unit={result.unit}/></Suspense>:<div className="notice">该时间区间或授权范围内没有数据。</div>}
            <p>{result.message}</p><div className="definition">{result.definition}</div>
            <div className="table-scroll"><table><thead><tr><th>范围</th><th>指标值</th><th>有效样本</th><th>排除数量</th><th>不可履约</th></tr></thead><tbody>{result.rows.map(row=><tr key={row.label}><td>{row.label}</td><td>{row.value===null?'无数据':Number(row.value).toLocaleString('zh-CN',{maximumFractionDigits:2})}</td><td>{row.sample_size??0}</td><td>{row.excluded_orders??0}</td><td>{row.unavailable_orders??0}</td></tr>)}</tbody></table></div>
            <details><summary>查看 SQL 与数据依据</summary><pre>{result.sql}</pre><p>绑定参数：{JSON.stringify(result.parameters)}</p><p>Token 用量：{result.usage.tokens??'提供商未返回'}</p></details>
          </article>}
        </>}
      </section>
      <form className="composer" onSubmit={submit}><div className="controls"><label>指标<select value={metric} onChange={e=>setMetric(e.target.value)}><option value="">从问题识别</option>{metrics.map(m=><option key={m.id} value={m.id}>{m.name}</option>)}</select></label><label>时间<select value={period} onChange={e=>setPeriod(e.target.value)}><option value="">从问题识别</option>{[['today','今天'],['yesterday','昨日'],['this_week','本周'],['last_week','上周'],['this_month','本月'],['last_month','上月'],['custom','指定日期']].map(([v,n])=><option key={v} value={v}>{n}</option>)}</select></label><label>维度<select value={group} onChange={e=>setGroup(e.target.value)}><option value="">从问题识别 / 整体</option><option value="state">巴西州</option><option value="day">下单日期</option><option value="month">下单月份</option></select></label>{period==='custom'&&<><label>开始<input type="date" value={start} onChange={e=>setStart(e.target.value)} required/></label><label>结束（不含）<input type="date" value={end} onChange={e=>setEnd(e.target.value)} required/></label></>}</div><div className="input-row"><textarea value={question} onChange={e=>setQuestion(e.target.value)} placeholder="例如：本月 GMV 按州统计" maxLength={2000} rows={2} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();void submit()}}}/><button className="primary" disabled={busy||!question.trim()||metrics.length===0}>{busy?'受理中…':'发送 ↗'}</button></div><small>Enter 发送 · Shift + Enter 换行 · 金额单位 BRL，GMV 不含运费</small></form>
      </>}
    </main>
  </div>
}

function Users({onError}:{onError:(message:string)=>void}){
  const[users,setUsers]=useState<{user:User&{id:string};enabled:boolean}[]>([])
  const[name,setName]=useState(''),[password,setPassword]=useState(''),[role,setRole]=useState('ops'),[busy,setBusy]=useState(false)
  const load=()=>api<typeof users>('/users').then(setUsers).catch(e=>onError(e.message))
  useEffect(()=>{void load()},[])
  async function create(event:React.FormEvent){event.preventDefault();setBusy(true);try{await api('/users',{username:name,password,role,allowed_metrics:['gmv','orders','aov','cancellation_rate','delivery_days','review_score']});setName('');setPassword('');await load()}catch(e){onError((e as Error).message)}finally{setBusy(false)}}
  return <section className="user-panel"><h2>租户账号</h2><p className="muted">新账号默认授予六项指标。禁用立即影响会话、历史结果和正在执行的任务。</p><table><thead><tr><th>账号</th><th>角色</th><th>状态</th><th>操作</th></tr></thead><tbody>{users.map(({user,enabled})=><tr key={user.id}><td>{user.username}</td><td>{user.role}</td><td>{enabled?'启用':'停用'}</td><td><button onClick={async()=>{try{await api('/users/'+user.id,{enabled:!enabled,allowed_metrics:user.allowed_metrics},'PATCH');await load()}catch(e){onError((e as Error).message)}}}>{enabled?'停用':'启用'}</button></td></tr>)}</tbody></table><form className="user-form" onSubmit={create}><h3>创建账号</h3><label>账号<input value={name} onChange={e=>setName(e.target.value)} required minLength={2} maxLength={100}/></label><label>初始密码<input type="password" value={password} onChange={e=>setPassword(e.target.value)} minLength={12} maxLength={72} required autoComplete="new-password"/></label><label>角色<select value={role} onChange={e=>setRole(e.target.value)}><option value="ops">运营人员</option><option value="data">数据人员</option><option value="admin">管理员</option></select></label><button className="primary" disabled={busy}>创建账号</button></form></section>
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>)
