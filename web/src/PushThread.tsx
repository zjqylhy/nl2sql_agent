import {useEffect,useRef,useState} from 'react'
import {Evidence,type Result} from './PushWorkspace'

type Api=<T>(path:string,body?:unknown,method?:string)=>Promise<T>
type Node={id:string;parent_message_id:string|null;seq:number;role:string;kind:string;content:{text?:string};plan?:unknown}
type Tree={id:string;version:number;cursor_id:string;active_branch_id:string;active_task_id:string|null;messages:Node[];path_ids:string[];branches:{id:string;label:string;head_message_id:string;fork_message_id:string|null}[];result:Result|null;task:{id:string;status:string;error?:string;clarification:{version:number;can_answer:boolean;reason:string}}|null}

export default function PushThread({api,id}:{api:Api;id:string}){
  const[tree,setTree]=useState<Tree|null>(null),[question,setQuestion]=useState(''),[answer,setAnswer]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('')
  const request=useRef<{key:string;id:string}|null>(null)
  const refresh=()=>api<Tree>('/push-threads/'+id).then(setTree)
  useEffect(()=>{setTree(null);setError('');void refresh().catch(e=>setError(e.message))},[id])
  useEffect(()=>{
    if(!tree?.task||!['pending','querying'].includes(tree.task.status))return
    let alive=true
    const update=()=>{if(alive)void refresh().catch(e=>setError(e.message))}
    const source=new EventSource('/api/tasks/'+tree.task.id+'/events')
    for(const name of ['accepted','step','done','error','clarification','canceled'])source.addEventListener(name,update)
    source.onerror=update
    const timer=setInterval(update,5000)
    return()=>{alive=false;source.close();clearInterval(timer)}
  },[tree?.task?.id,tree?.task?.status])
  async function seek(message_id:string){if(!tree||busy)return;setBusy(true);setError('');try{setTree(await api<Tree>('/push-threads/'+id+'/cursor',{expected_version:tree.version,message_id}));setQuestion('');setAnswer('');request.current=null}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function submit(event:React.FormEvent,clarification=false){
    event.preventDefault();if(!tree||busy)return
    const text=clarification?answer:question
    if(!text.trim())return
    const version=clarification?tree.task!.clarification.version:tree.version
    const key=JSON.stringify([id,tree.task?.id,clarification,version,text])
    if(request.current?.key!==key)request.current={key,id:crypto.randomUUID()}
    setBusy(true);setError('')
    try{
      if(clarification){await api('/tasks/'+tree.task!.id+'/answers',{expected_version:version,request_id:request.current.id,answer:text});await refresh();setAnswer('')}
      else{setTree(await api<Tree>('/push-threads/'+id+'/turns',{expected_version:version,request_id:request.current.id,question:text}));setQuestion('')}
      request.current=null
    }catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  if(!tree)return <p>{error||'正在恢复会话…'}</p>
  const active=Boolean(tree.active_task_id)
  const versions=tree.messages.filter(m=>m.plan)
  const path=tree.messages.filter(m=>tree.path_ids.includes(m.id)&&['text','answer','clarification','failed','canceled'].includes(m.kind))
  return <>
    {error&&<p className="error" role="alert">{error}</p>}
    <div className="push-toolbar"><label>分支<select value={tree.active_branch_id} disabled={busy||active} onChange={e=>{const branch=tree.branches.find(b=>b.id===e.target.value);if(branch)void seek(branch.head_message_id)}}>{tree.branches.map(b=><option key={b.id} value={b.id}>{b.label}</option>)}</select></label><label>回答版本 / 回退位置<select value={versions.some(v=>v.id===tree.cursor_id)?tree.cursor_id:''} disabled={busy||active} onChange={e=>void seek(e.target.value)}><option value="" disabled>当前消息位置</option>{versions.map((m,i)=><option key={m.id} value={m.id}>回答 {i+1} · {m.content.text?.split('\n')[0]}</option>)}</select></label></div>
    <p className="muted">从历史回答继续提问会保留原对话并创建新分支。只使用当前路径的上下文。</p>
    <div aria-label="当前分支消息">{path.map(m=><article key={m.id} className={m.role==='user'?'question-bubble':'notice'}><strong>{m.role==='user'?'你':'助手'}</strong><p style={{whiteSpace:'pre-wrap'}}>{m.content.text}</p>{Boolean(m.plan)&&m.id!==tree.cursor_id&&<button disabled={busy||active} onClick={()=>void seek(m.id)}>回到这版回答</button>}</article>)}</div>
    {tree.result?.context_compacted&&<p className="notice">较早对话已摘要，完整原文仍保留；回退到对应回答可查看。</p>}
    {tree.result&&<Evidence result={tree.result}/>}
    {tree.task&&<div className="task-progress"><strong>{tree.task.status==='needs_clarification'?'需要补充':tree.task.status==='pending'?'排队中':'执行中'}</strong><button disabled={busy} onClick={async()=>{try{await api('/tasks/'+tree.task!.id+'/stop',{});await refresh()}catch(e){setError((e as Error).message)}}}>停止</button></div>}
    {tree.task?.status==='needs_clarification'?tree.task.clarification.can_answer?<form className="analysis-form" onSubmit={e=>void submit(e,true)}><label>回答这条追问<textarea autoFocus value={answer} onChange={e=>setAnswer(e.target.value)} maxLength={1000} required/></label><button className="primary" disabled={busy||!answer.trim()}>补充并继续查询</button></form>:<p className="error">{tree.task.clarification.reason}</p>:<form className="analysis-form" onSubmit={e=>void submit(e)}><label>继续追问或调整回答<textarea value={question} onChange={e=>setQuestion(e.target.value)} maxLength={2000} disabled={active} placeholder="例如：只看华为和小米；改查9月；简洁一点，用表格展示" required/></label><button className="primary" disabled={busy||active||!question.trim()}>{busy?'正在提交…':'发送'}</button></form>}
  </>
}
