import { Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { RecordData } from '../types'

const tooltip = {background:'#101d2b',border:'1px solid #30465c',borderRadius:7,color:'#d0e4ee',fontSize:11}
const tick = {fill:'#6e8aa1',fontSize:9}

export function SeriesChart({title,points,valueKey}:{title:string;points:RecordData[];valueKey:string}) {
  const data = points.map(point=>({...point,value:Number(point[valueKey]),label:String(point.time).slice(5,16).replace('T',' ')}))
  const color = valueKey==='equity'?'#4dd8bd':'#ec8f9c'
  return <div className="chart-box" role="img" aria-label={`${title} · ${points.length} recorded points`}><ResponsiveContainer width="100%" height="100%" minWidth={0} initialDimension={{width:800,height:240}}>
    <AreaChart data={data} margin={{top:10,right:18,left:0,bottom:4}}><CartesianGrid vertical={false} stroke="#203349" strokeDasharray="3 5"/><XAxis dataKey="label" tick={tick} tickLine={false} axisLine={false} minTickGap={30}/><YAxis tick={tick} tickLine={false} axisLine={false} domain={['auto','auto']}/><Tooltip contentStyle={tooltip}/><Area type="linear" dataKey="value" name={title} stroke={color} fill={color} fillOpacity={.09} strokeWidth={2} isAnimationActive={false}/></AreaChart>
  </ResponsiveContainer><p className="chart-caption">{points.length} 个已记录时间点 · UTC · {valueKey==='equity'?'账户权益':'按采样净值计算的回撤'}</p></div>
}

export function CostChart({metrics}:{metrics:RecordData}) {
  const keys = [['net_pnl','NET PNL'],['fees','FEES'],['funding_cash','FUNDING'],['spread_cost','SPREAD'],['slippage_cost','SLIPPAGE']]
  const data = keys.filter(([key])=>metrics[key]!=null&&Number.isFinite(Number(metrics[key]))).map(([key,label])=>({label,value:Number(metrics[key])}))
  if (!data.length) return null
  return <div className="chart-box" role="img" aria-label="Recorded PnL and costs"><ResponsiveContainer width="100%" height="100%" minWidth={0} initialDimension={{width:800,height:240}}>
    <BarChart data={data} margin={{top:8,right:18,left:0,bottom:4}}><CartesianGrid vertical={false} stroke="#203349" strokeDasharray="3 5"/><XAxis dataKey="label" tick={tick} tickLine={false} axisLine={false}/><YAxis tick={tick} tickLine={false} axisLine={false}/><Tooltip contentStyle={tooltip}/><ReferenceLine y={0} stroke="#3c536b"/><Bar dataKey="value" name="USDT" radius={[3,3,0,0]} maxBarSize={46} isAnimationActive={false}>{data.map(row=><Cell key={row.label} fill={row.value<0?'#c76678':'#3f9e8d'}/>)}</Bar></BarChart>
  </ResponsiveContainer><p className="chart-caption">原始记录的带符号金额 · USDT · 各项保持原始成本口径</p></div>
}
