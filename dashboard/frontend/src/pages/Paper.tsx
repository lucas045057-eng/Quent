import { useState } from 'react'
import { usePolling } from '../api'
import type { ItemList, PaperData } from '../types'
import { Badge, Empty, Fields, Notice, Panel, Raw, SourceFoot, Table, time } from '../components/Ui'

export default function Paper() {
  const [session,setSession] = useState('')
  const [tab,setTab] = useState('positions')
  const paper = usePolling<PaperData>('/api/paper')
  const suffix = session?'?session='+encodeURIComponent(session):''
  const positions = usePolling<ItemList>('/api/positions'+suffix)
  const orders = usePolling<ItemList>('/api/orders'+suffix)
  const trades = usePolling<ItemList>('/api/trades'+suffix)
  const data = paper.response?.data
  const selected = session ? data?.sessions.find(item=>item.session_id===session) : data?.current
  const historical = !!selected && selected.freshness !== 'CURRENT'
  const selectedState = tab==='positions'?positions:tab==='orders'||tab==='audit'?orders:trades
  return <><div className="page-lead"><span className="eyebrow">NATIVE SANDBOX / 运行观测</span><h1>Paper Trading</h1><p>检查进程、账户快照、订单与对账。刷新间隔 3 秒。</p></div><Notice state={paper}/>
    <div className="toolbar"><Badge value={data?.state}/><span className="pill">READ ONLY</span><label>Paper 会话<select aria-label="Paper 会话" value={session} onChange={e=>setSession(e.target.value)}><option value="">当前已验证进程</option>{data?.sessions.map(item=><option key={item.session_id} value={item.session_id}>{item.freshness} · PID {item.pid} · {item.source.split('/').slice(-2).join('/')}</option>)}</select></label></div>
    {historical && <div className="notice warning"><strong>HISTORICAL SNAPSHOT</strong><span>此数据来自历史记录，不能用于判断当前账户或仓位。</span></div>}
    {selected?.acceptance_kind==='FIXTURE_DRIVEN_ACCEPTANCE' && <div className="fixture-banner">FIXTURE_DRIVEN_ACCEPTANCE · 合成数据工程验收 · 策略 edge 未验证</div>}
    <div className="two-cols"><Panel title="进程与会话" subtitle={selected?.source??'当前进程观测'} action={<Badge value={selected?.freshness}/>}>{selected?<Fields data={selected} fields={[["pid","PID"],["process_started_at","进程启动 UTC"],["uptime_seconds","运行秒数"],["heartbeat_at","最后心跳 UTC"],["heartbeat_age_seconds","心跳延迟 / 秒"],["last_restart_at","最后重启 UTC"],["restored","恢复状态"],["session_id","Session ID"]]}/>:<Empty title="当前没有已验证的 Paper 进程" detail="选择历史会话查看已记录状态；启动 Paper 请使用现有命令行流程。"/>}</Panel>
    <Panel title="Reconciliation" subtitle="各项匹配状态分别读取，不由整体状态推断"><div className="reconcile-list">{[['整体对账',selected?.reconciliation],['仓位与 Native 匹配',selected?.position.native_match],['订单匹配',selected?.position.order_match],['成交匹配',selected?.position.trade_match],['数据库匹配',selected?.position.database_match]].map(([label,value])=><div key={String(label)}><span>{String(label)}</span><Badge value={value}/></div>)}</div></Panel></div>
    <Panel title="执行状态" subtitle={historical?'历史快照 · STALE':'当前已验证会话'} action={<div className="tabs">{[['positions','持仓'],['orders','订单'],['trades','成交'],['audit','执行审计']].map(([id,label])=><button key={id} className={tab===id?'active':''} onClick={()=>setTab(id)}>{label}</button>)}</div>}>
      <Notice state={selectedState}/>
      {tab==='positions' && <Table rows={positions.response?.data.items??[]} columns={[{key:'canonical_symbol',label:'SYMBOL'},{key:'side',label:'SIDE',render:v=><Badge value={v}/>},{key:'quantity',label:'数量 / BASE'},{key:'average_entry',label:'入场均价'},{key:'mark_price',label:'MARK'},{key:'unrealized_pnl',label:'未实现 PNL'},{key:'realized_trade_pnl',label:'已实现 PNL'},{key:'fees',label:'FEES'},{key:'funding_cash',label:'FUNDING'},{key:'protection_status',label:'保护',render:v=><Badge value={v}/>},{key:'stop_loss',label:'止损价'},{key:'take_profit',label:'止盈价'},{key:'snapshot_as_of',label:'快照时间',render:time}]} empty="没有当前持仓记录。历史快照需要选择对应会话。"/>}
      {tab==='orders' && <Table rows={orders.response?.data.items??[]} columns={[{key:'client_order_id',label:'CLIENT ORDER ID'},{key:'canonical_symbol',label:'SYMBOL'},{key:'side',label:'SIDE'},{key:'order_type',label:'INTENT TYPE'},{key:'approved_quantity',label:'INTENT 数量'},{key:'requested_quantity',label:'请求数量'},{key:'filled_quantity',label:'累计成交'},{key:'remaining_quantity',label:'剩余数量'},{key:'native_status',label:'NATIVE STATUS',render:v=><Badge value={v}/>},{key:'average_price',label:'均价'},{key:'created_at',label:'创建时间',render:time}]}/ >}
      {tab==='trades' && <Table rows={trades.response?.data.items??[]} columns={[{key:'trade_id',label:'TRADE ID'},{key:'quantity',label:'数量'},{key:'price',label:'价格'},{key:'fees',label:'费用'}]} empty="NO DATA · 当前来源未持久化逐笔成交。累计执行结果不能作为新的 Trade。"/>}
      {tab==='audit' && <><div className="inline-note">累计 ExecutionResult 审计记录；不代表独立的逐笔成交或新增费用。</div><Raw value={orders.response?.data.execution_audit??[]} label="查看执行审计记录"/></>}
    </Panel><SourceFoot response={paper.response}/>
  </>
}
