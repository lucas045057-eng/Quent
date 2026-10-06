import { usePolling } from '../api'
import type { HealthData } from '../types'
import { Badge, Metric, Notice, Panel, SourceFoot, Table } from '../components/Ui'
import JsonPanel from '../components/JsonPanel'

const names:Record<string,string> = {quant_core:'Quant Core',database:'PostgreSQL',paper:'Local Paper',backtest:'Backtest Engine',
  nautilus_adapter:'Nautilus Adapter',funding_adapter:'Funding Adapter',funding_source:'Funding Source',
  reconciliation:'Reconciliation',jev:'Jev',live:'Live Trading'}

export default function Health() {
  const state = usePolling<HealthData>('/api/health')
  const data = state.response?.data
  const resource = data?.resources??{}
  const mb = (value:unknown) => value==null?null:Number(value)/1048576
  return <><div className="page-lead"><span className="eyebrow">SYSTEM OBSERVABILITY / 健康监测</span><h1>System Health</h1><p>当前监测值和历史健康事件分别显示。缺少监测来源时为 N/A。</p></div><Notice state={state}/>
    <div className="health-grid">{data?.components.map(item=><div className="health-card" key={item.component}><div className="health-card-icon">⊕</div><div><h2>{names[item.component]??item.component}</h2><span>{item.source}</span></div><Badge value={item.status}/></div>)}</div>
    <Panel title="进程与资源" subtitle="内存为实际进程观测；未接入的监控不补零"><div className="metrics health-metrics"><Metric id="dashboard_rss" label="Dashboard RSS" value={mb(resource.dashboard_rss_bytes)} unit="MiB"/><Metric id="paper_rss" label="当前 Paper RSS" value={mb(resource.paper_rss_bytes)} unit="MiB"/><Metric id="cpu_percent" label="CPU 利用率" value={resource.cpu_percent} unit="%"/><Metric id="disk_free" label="剩余磁盘" value={resource.disk_free_bytes==null?null:Number(resource.disk_free_bytes)/1073741824} unit="GiB"/></div>
      <div className="monitor-fields">{[['WebSocket',resource.websocket],['重连次数',resource.reconnect_count],['降级次数',resource.degradation_count],['Native 异常数',resource.native_exception_count]].map(([label,value])=><div key={String(label)}><span>{String(label)}</span><Badge value={value}/></div>)}</div>
    </Panel><Panel title="历史健康事件" subtitle="旧 RUNNING 记录不会作为当前健康证明"><Table rows={data?.recorded_health_events??[]} columns={[{key:'component',label:'COMPONENT'},{key:'state',label:'RECORDED STATE',render:v=><Badge value={v}/>},{key:'reason',label:'REASON'},{key:'created_at',label:'RECORDED UTC'},{key:'duration_seconds',label:'DURATION'}]}/></Panel>
    <JsonPanel value={{missing_tables:data?.database_missing_tables??[],controls:data?.controls??null}} label="数据源诊断"/><SourceFoot response={state.response}/></>
}
