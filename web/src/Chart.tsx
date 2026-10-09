import {useEffect,useRef} from 'react'
import * as echarts from 'echarts/core'
import {BarChart} from 'echarts/charts'
import {GridComponent,TooltipComponent} from 'echarts/components'
import {CanvasRenderer} from 'echarts/renderers'

echarts.use([BarChart,GridComponent,TooltipComponent,CanvasRenderer])

type Row={label:string;value:string|number|null}
export default function Chart({rows,unit}:{rows:Row[];unit:string}){
  const ref=useRef<HTMLDivElement>(null)
  useEffect(()=>{
    if(!ref.current)return
    const chart=echarts.init(ref.current)
    chart.setOption({grid:{left:65,right:20,top:25,bottom:35},tooltip:{trigger:'axis'},xAxis:{type:'category',data:rows.map(r=>r.label),axisLine:{lineStyle:{color:'#c7d2df'}}},yAxis:{type:'value',name:unit,splitLine:{lineStyle:{color:'#edf1f5'}}},series:[{type:'bar',data:rows.map(r=>r.value===null?null:Number(r.value)),itemStyle:{color:'#157f78',borderRadius:[4,4,0,0]},barMaxWidth:40}]})
    const observer=new ResizeObserver(()=>chart.resize());observer.observe(ref.current)
    return()=>{observer.disconnect();chart.dispose()}
  },[rows,unit])
  return <div ref={ref} className="chart" role="img" aria-label="按巴西州分组的指标柱状图"/>
}
