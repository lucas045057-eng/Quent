import { lazy, Suspense, useState } from 'react'
import { usePolling } from '../api'
import type { BacktestDetail, ItemList, RecordData } from '../types'
import { Badge, Empty, Fields, Metric, Notice, Panel, SourceFoot, Table, number, text, time } from '../components/Ui'
import JsonPanel from '../components/JsonPanel'

const CostChart = lazy(()=>import('../components/Charts').then(module=>({default:module.CostChart})))
const SeriesChart = lazy(()=>import('../components/Charts').then(module=>({default:module.SeriesChart})))
const chartLoading = <div className="notice loading">正在加载图表…</div>

const metrics:[string,string][] = [['initial_equity','初始权益'],['final_equity','最终权益'],['net_pnl','净盈亏'],['total_return','总收益率'],['max_drawdown','最大回撤'],['sharpe','Sharpe'],['sortino','Sortino'],['win_rate','胜率'],['profit_factor','盈亏比'],['trade_count','成交笔数'],['fees','手续费'],['funding_cash','Funding 现金流']]

export default function Backtests() {
  const [id,setId] = useState('')
  const list = usePolling<ItemList>('/api/backtests',0)
  const detail = usePolling<BacktestDetail|null>(id?'/api/backtests/'+encodeURIComponent(id):null,0)
  const data = detail.response?.data
  const rows = list.response?.data.items??[]
  function select(row:RecordData){setId(String(row.run_id))}
  return <><div className="page-lead"><span className="eyebrow">RESEARCH TERMINAL / 回测审计</span><h1>Backtest</h1><p>已持久化的回测报告与真实记录曲线。工程验收不代表策略收益验证。</p></div><Notice state={list}/>
    <Panel title="历史运行记录" subtitle={`${rows.length} 个独立 case / run · 报告 digest 已校验`} action={<span className="pill">ON DEMAND</span>}>
      {rows.length?<Table rows={rows} select={select} columns={[{key:'canonical_symbol',label:'SYMBOL / CASE',render:(v,row)=><span>{text(v)} · {text(row.side)}</span>},{key:'strategy',label:'STRATEGY'},{key:'status',label:'工程状态',render:v=><Badge value={v}/>},{key:'net_pnl',label:'NET PNL',render:(_v,row)=><span className={Number((row.metrics as RecordData)?.net_pnl)<0?'loss':'profit'}>{number((row.metrics as RecordData)?.net_pnl,4)}</span>},{key:'timeframe',label:'TF'},{key:'started_at',label:'开始时间',render:time},{key:'acceptance_kind',label:'DATA BASIS',render:v=><Badge value={v}/>},{key:'source',label:'SOURCE'}]}/>:<Empty title="没有可读取的 Backtest 报告" detail="仅读取现有验收报告；无报告时不生成示例收益。"/>}<SourceFoot response={list.response}/>
    </Panel><Notice state={detail}/>
    {!data?<Panel title="运行详情" subtitle="选择上方 case / run"><Empty title="等待选择" detail="独立的 BTC / ETH 多空 case 不会拼接成净值曲线。"/></Panel>:<>
      {data.acceptance_kind==='FIXTURE_DRIVEN_ACCEPTANCE'&&<div className="fixture-banner">FIXTURE_DRIVEN_ACCEPTANCE · 合成数据 · STRATEGY EDGE: {data.strategy_edge_status??'NOT_RECORDED'}</div>}
      <div className="run-header"><div><strong>{text(data.canonical_symbol)} / {text(data.side)}</strong><span>{text(data.strategy)}</span></div><Badge value={data.status}/><small>RUN {data.run_id}</small></div>
      <div className="metrics backtest-metrics">{metrics.map(([key,label])=><Metric key={key} id={key} label={label} value={data.metrics[key]} integer={key==='trade_count'} unit={['net_pnl','initial_equity','final_equity','fees','funding_cash'].includes(key)?'USDT':undefined}/>)}</div>
      <div className="two-cols"><Panel title="Equity Curve" subtitle="已记录的时间序列">{data.equity_curve.length?<Suspense fallback={chartLoading}><SeriesChart title="Equity Curve" points={data.equity_curve} valueKey="equity"/></Suspense>:<Empty title="净值曲线未记录" detail="NO DATA · 当前报告没有时间序列净值；单个终值 PnL 不能生成曲线。"/>}</Panel><Panel title="Drawdown" subtitle="依据采样净值计算，不等于逐 tick 最大回撤">{data.drawdown_curve.length?<Suspense fallback={chartLoading}><SeriesChart title="Drawdown" points={data.drawdown_curve} valueKey="drawdown_pct"/></Suspense>:<Empty title="回撤曲线未记录" detail="NO DATA · 缺少实际净值时间序列。"/>}</Panel></div>
      <div className="two-cols"><Panel title="PnL / Fees / Funding" subtitle="原始报告记录的带符号金额"><Suspense fallback={chartLoading}><CostChart metrics={data.metrics}/></Suspense></Panel><Panel title="数据与成本口径"><Fields data={data} fields={[["started_at","开始 UTC"],["ended_at","结束 UTC"],["nautilus_version","Nautilus version"],["policy_scope","Policy scope"],["cost_assumption","成本假设"],["fee_assumption","手续费假设"],["strategy_edge_status","Strategy edge"],["report_digest","Report digest"]]}/></Panel></div>
      <Panel title="Trades" subtitle="只读逐笔记录">{data.trades.length?<Table rows={data.trades} columns={[{key:'trade_id',label:'TRADE ID'},{key:'time',label:'UTC',render:time},{key:'side',label:'SIDE'},{key:'quantity',label:'数量'},{key:'price',label:'价格'},{key:'fees',label:'费用'},{key:'pnl',label:'PNL'}]}/>:<Empty title="逐笔成交未记录" detail="NO DATA · 当前验收报告只有独立 case 的聚合结果。"/>}</Panel>
      <JsonPanel value={data.record} label="查看该 case / run 的脱敏原始记录"/><SourceFoot response={detail.response}/>
    </>}</>
}
