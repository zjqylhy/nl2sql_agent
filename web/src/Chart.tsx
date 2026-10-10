import {useEffect,useRef} from 'react'
import * as echarts from 'echarts/core'
import {BarChart,LineChart} from 'echarts/charts'
import {GridComponent,TooltipComponent,LegendComponent} from 'echarts/components'
import {CanvasRenderer} from 'echarts/renderers'

echarts.use([BarChart,LineChart,GridComponent,TooltipComponent,LegendComponent,CanvasRenderer])

type Row={label:string;value:string|number|null}
export default function Chart({rows=[],unit,kind='bar',series}:{rows?:Row[];unit:string;kind?:'bar'|'line';series?:{name:string;rows:Row[]}[]}){
  const ref=useRef<HTMLDivElement>(null)
  useEffect(()=>{
    if(!ref.current)return
    const chart=echarts.init(ref.current)
    const data=series??[{name:unit,rows}]
    const labels=[...new Set(data.flatMap(s=>s.rows.map(r=>r.label)))]
    if(kind==='line')labels.sort()
    chart.setOption({grid:{left:65,right:20,top:data.length>1?65:45,bottom:45},tooltip:{trigger:'axis'},legend:{show:data.length>1,type:'scroll',top:8,left:65,right:20},xAxis:{type:'category',data:labels,axisLine:{lineStyle:{color:'#c7d2df'}}},yAxis:{type:'value',name:unit,splitLine:{lineStyle:{color:'#edf1f5'}}},series:data.map(s=>{const values=new Map(s.rows.map(r=>[r.label,r.value]));return {name:s.name,type:kind,connectNulls:false,data:labels.map(label=>{const value=values.get(label);return value===null||value===undefined?null:Number(value)}),barMaxWidth:40}})})
    const observer=new ResizeObserver(()=>chart.resize());observer.observe(ref.current)
    return()=>{observer.disconnect();chart.dispose()}
  },[rows,unit,kind,series])
  return <div ref={ref} className="chart" role="img" aria-label={kind==='line'?'按时间分组的指标趋势图':'分组指标柱状图'}/>
}
