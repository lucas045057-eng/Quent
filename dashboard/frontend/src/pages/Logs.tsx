import { useState } from 'react'
import { usePolling } from '../api'
import type { ItemList, RecordData } from '../types'
import { Badge, Notice, Panel, SourceFoot, Table, time } from '../components/Ui'
import JsonPanel from '../components/JsonPanel'

export default function Logs() {
  const [level,setLevel] = useState('')
  const [module,setModule] = useState('')
  const [query,setQuery] = useState('')
  const [search,setSearch] = useState('')
  const [selected,setSelected] = useState<RecordData|null>(null)
  const params = new URLSearchParams()
  if (level) params.set('level',level)
  if (module) params.set('module',module)
  if (search) params.set('q',search)
  const state = usePolling<ItemList>('/api/logs'+(params.size?'?'+params.toString():''))
  const data = state.response?.data
  return <><div className="page-lead"><span className="eyebrow">AUDIT STREAM / 运行事件</span><h1>Logs / Events</h1><p>读取真实日志尾部与审计事件。凭据、连接串和敏感字段在服务端脱敏。</p></div>
    <form className="log-filters" onSubmit={e=>{e.preventDefault();setSearch(query)}}><label>LEVEL<select aria-label="日志级别" value={level} onChange={e=>setLevel(e.target.value)}><option value="">全部级别</option>{['DEBUG','INFO','WARNING','ERROR','CRITICAL','UNKNOWN'].map(item=><option key={item}>{item}</option>)}</select></label>
      <label>MODULE<select aria-label="日志模块" value={module} onChange={e=>setModule(e.target.value)}><option value="">全部模块</option>{data?.modules?.map(item=><option key={item}>{item}</option>)}</select></label>
      <label className="log-search">SEARCH<input aria-label="搜索日志" value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜索消息 / correlation ID" maxLength={128}/></label><button type="submit">筛选事件</button><span className="pill">3s REFRESH</span></form>
    <Notice state={state}/><Panel title="Event stream" subtitle={`实际记录 · 最多 100 条 · ${data?.items.length??0} 条匹配`} action={<Badge value={state.response?.availability}/>}><Table rows={data?.items??[]} columns={[{key:'time',label:'TIME (UTC)',render:time},{key:'level',label:'LEVEL',render:v=><Badge value={v}/>},{key:'module',label:'MODULE'},{key:'message',label:'MESSAGE'},{key:'correlation_id',label:'CORRELATION'},{key:'event_id',label:'DETAIL',render:(v,row)=><button className="table-link" onClick={()=>setSelected(row)} aria-label={'查看事件 '+String(v)}>展开 ↗</button>}]} empty="没有匹配的实际事件。日志为空和过滤无结果都不会生成示例行。"/></Panel>
    {selected&&<Panel title="事件详情" subtitle={String(selected.event_id)} action={<button className="table-link" onClick={()=>setSelected(null)}>关闭</button>}><JsonPanel value={selected} label="查看脱敏事件记录"/></Panel>}<SourceFoot response={state.response}/>
  </>
}
