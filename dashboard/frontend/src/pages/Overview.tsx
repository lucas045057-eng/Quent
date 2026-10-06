import { usePolling } from '../api'
import type { ApiState, ItemList, OverviewData } from '../types'
import { Badge, Empty, Metric, Notice, Panel, SourceFoot, Table, time } from '../components/Ui'

const cards:[string,string][] = [['equity','账户权益'],['available_balance','可用余额'],['unrealized_pnl','未实现盈亏'],['realized_pnl','已实现交易盈亏'],['total_pnl','净盈亏'],['position_count','当前持仓数'],['orders_today','今日订单数'],['trades_today','今日成交数']]
export default function Overview({state}:{state:ApiState<OverviewData>}) {
  const data = state.response?.data
  const decisions = usePolling<ItemList>('/api/decisions?limit=5')
  const logs = usePolling<ItemList>('/api/logs?limit=5')
  return <><Notice state={state}/><div className="page-lead"><span className="eyebrow">LOCAL OPERATIONS / 实时监测</span><h1>Overview</h1><p>账户、执行与系统状态，来自已验证的本地记录。</p></div>
    <div className="status-strip">{[['Quant Core',data?.system],['运行模式',data?.mode],['Paper',data?.paper],['数据库',data?.database],['Jev',data?.jev],['Funding Adapter',data?.funding],['Funding Source',data?.funding_source],['对账',data?.reconciliation]].map(([label,value]) => <div key={label}><span>{label}</span><Badge value={value}/></div>)}</div>
    <div className="metrics">{cards.map(([id,label]) => <Metric key={id} id={id} label={label} value={data?.metrics[id]??null} integer={id.includes('count')||id.includes('today')} unit={id.includes('count')||id.includes('today')?undefined:'USDT'}/>)}</div>
    <div className="two-cols"><Panel title="当前账户观测" subtitle="只显示已验证进程的账户状态" action={<a className="text-link" href="#/paper">查看 Paper →</a>}>
      {data?.metric_scope ? <div className="account-scope"><Badge value="CURRENT"/><p>会话 {data.metric_scope}</p><p>净盈亏 = 已实现 + 未实现 − 费用 + Funding</p></div> : <Empty title="没有当前账户数据" detail="Paper 未运行或心跳无法验证。历史账户快照可在 Paper 页面单独查看。"/>}
    </Panel><Panel title="最近决策" subtitle="DecisionCandidate · 只读审计" action={<a className="text-link" href="#/decisions">检查证据链 →</a>}><Notice state={decisions}/>
      <Table rows={decisions.response?.data.items??[]} columns={[{key:'symbol',label:'SYMBOL'},{key:'direction_bias',label:'方向'},{key:'eligible',label:'允许',render:v=><Badge value={v===true?'PASS':v===false?'REJECT':null}/>},{key:'created_at',label:'时间',render:time}]} empty="没有可读取的 DecisionCandidate。连接已存在的审计数据库后显示记录。"/>
    </Panel></div>
    <Panel title="最近运行记录" subtitle="实际日志与持久化事件" action={<a className="text-link" href="#/logs">全部事件 →</a>}><Notice state={logs}/><Table rows={logs.response?.data.items??[]} columns={[{key:'time',label:'TIME (UTC)',render:time},{key:'level',label:'LEVEL',render:v=><Badge value={v}/>},{key:'module',label:'MODULE'},{key:'message',label:'MESSAGE'}]}/></Panel>
    <SourceFoot response={state.response}/>
  </>
}
