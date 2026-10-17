import {lazy,Suspense} from 'react'
const Chart=lazy(()=>import('./Chart'))

type Cell={value:string|number|null;sample_size:number;excluded_orders:number}
type Metric={id:string;name:string;unit:string;definition:string;version:string}
type Plan={metrics:string[];dimensions:string[];time_range:{start:string;end_exclusive:string};filters:{field:string;op:string;values:string[]}[];order_by:{field:string;direction:string}[];top_n:number|null}
export type SemanticResult={result_version:2;metrics:Metric[];dimensions:{id:string;name:string}[];plan:Plan;analysis_date:string;period:{start:string;end_exclusive:string};rows:{label:string;dimensions:Record<string,string>;values:Record<string,Cell>;total_orders:number;unavailable_orders:number}[];sql:string;parameters:string[];usage:{parser:string;tokens:number|null};message:string;truncated:boolean}
const fieldNames:Record<string,string>={state:'巴西州',status:'订单状态',amount:'订单商品金额（不含运费）'}
const opNames:Record<string,string>={eq:'等于',ne:'不等于',in:'包含',not_in:'不含',gt:'大于',gte:'大于等于',lt:'小于',lte:'小于等于'}
const statusNames:Record<string,string>={created:'已创建',approved:'已批准',invoiced:'已开票',processing:'处理中',shipped:'已发货',delivered:'已交付',unavailable:'不可履约',canceled:'已取消'}
const display=(value:string|number|null)=>value===null?'无数据':Number(value).toLocaleString('zh-CN',{maximumFractionDigits:2})

export default function QueryResult({result}:{result:SemanticResult}){
  const time=result.plan.dimensions.find(d=>d==='day'||d==='month')
  const overall=result.plan.dimensions.length===0
  return <article className="answer">
    <div className="answer-heading"><h2>{result.metrics.map(m=>m.name).join(' · ')}</h2><span className="badge">口径 v1 · {result.usage.parser==='tool'?'授权工具查询':result.usage.parser==='llm'?'模型解析':'规则解析'}</span></div>
    <p className="muted">{result.period.start} 至 {result.period.end_exclusive}（不含结束日） · 分析基准 {result.analysis_date}</p>
    {result.plan.filters.length>0&&<p className="definition">筛选：{result.plan.filters.map(f=>`${fieldNames[f.field]} ${opNames[f.op]} ${f.values.map(v=>f.field==='status'?statusNames[v]??v:v).join('、')}`).join('；')}</p>}
    {result.truncated&&<div className="notice">{result.message} 排序：{result.plan.order_by.map(o=>(result.metrics.find(m=>m.id===o.field)?.name??o.field)+(o.direction==='desc'?'降序':'升序')).join('、')}</div>}
    {result.rows.length===0?<div className="notice">该时间区间或授权范围内没有数据。</div>:overall?<div className="metric-cards">{result.metrics.map(m=><div key={m.id}><strong>{m.name}</strong><div className="metric-value">{display(result.rows[0].values[m.id].value)}<span>{m.unit}</span></div><small>有效样本 {result.rows[0].values[m.id].sample_size}</small></div>)}</div>:<Suspense fallback={<p>正在加载图表…</p>}>{result.metrics.map(m=>{
      // 联合分组按州拆成时间序列；不同单位的指标分别绘图。
      const groups=time&&result.plan.dimensions.includes('state')?[...new Set(result.rows.map(r=>r.dimensions.state))]:['']
      return <div key={m.id}><h3>{m.name} · {m.unit}</h3><Chart unit={m.unit} kind={time?'line':'bar'} series={groups.map(group=>({name:group||m.name,rows:result.rows.filter(r=>!group||r.dimensions.state===group).map(r=>({label:time?r.dimensions[time]:r.label,value:r.values[m.id].value}))}))}/></div>
    })}</Suspense>}
    {!result.truncated&&<p>{result.message}</p>}
    {result.metrics.map(m=><div className="definition" key={m.id}>{m.name}（口径 v{m.version}）：{m.definition}</div>)}
    <div className="table-scroll"><table><thead><tr><th>范围</th>{result.metrics.map(m=><th key={m.id}>{m.name}（{m.unit}）</th>)}{result.metrics.map(m=><th key={m.id}>{m.name}样本 / 排除</th>)}<th>总订单 / 不可履约</th></tr></thead><tbody>{result.rows.map((row,i)=><tr key={i}><td>{row.label}</td>{result.metrics.map(m=><td key={m.id}>{display(row.values[m.id].value)}</td>)}{result.metrics.map(m=><td key={m.id}>{row.values[m.id].sample_size} / {row.values[m.id].excluded_orders}</td>)}<td>{row.total_orders} / {row.unavailable_orders}</td></tr>)}</tbody></table></div>
    <details><summary>查看 SQL 与数据依据</summary><pre>{result.sql}</pre><p>绑定参数：{JSON.stringify(result.parameters)}</p><p>Token 用量：{result.usage.tokens??'提供商未返回'}</p><pre>{JSON.stringify(result.plan,null,2)}</pre></details>
  </article>
}
