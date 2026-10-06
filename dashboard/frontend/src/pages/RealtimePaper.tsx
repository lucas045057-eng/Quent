import { usePolling } from '../api'
import type { Envelope, RecordData } from '../types'
import { Badge, Empty, Notice, Panel, SourceFoot, Table, time } from '../components/Ui'

interface RealtimePaperData extends RecordData {
  session:RecordData|null
  runtime:RecordData|null
  decisions:RecordData[]
  events:RecordData[]
  data_source:string
  counts:RecordData
}
const list = (value:unknown):RecordData[] => Array.isArray(value)?value as RecordData[]:[]
const text = (value:unknown):string => value==null?'N/A':String(value)

export default function RealtimePaper() {
  const state = usePolling<RealtimePaperData>('/api/realtime-paper')
  const data = state.response?.data
  const runtime = (data?.runtime??{}) as RecordData
  const symbols = list(runtime.symbols)
  const checks = list(runtime.checks)
  const health = (runtime.health??{}) as RecordData
  const processHealth = (health.process_alive??{}) as RecordData
  const collectorHealth = (health.collector_heartbeat??{}) as RecordData
  const marketHealth = (health.fresh_market_data??{}) as RecordData
  const latest = data?.decisions?.slice(0,10)??[]
  const blockerText = list(runtime.blockers).map(String).join(', ')
  const feedRows = symbols.flatMap(row=>list(row.feeds).map(feed=>({...feed,symbol:row.symbol})))
  const executionFeed = runtime.execution_quote_freshness && typeof runtime.execution_quote_freshness==='object'
    ? [{symbol:'—',kind:'EXECUTION QUOTE',...(runtime.execution_quote_freshness as RecordData)}] : []
  const feeds = [...feedRows,...list(runtime.data_freshness),...executionFeed]
  return <>
    <div className="page-lead"><span className="eyebrow">CANONICAL DATA / REALTIME PAPER</span><h1>Realtime Paper Readiness</h1><p>只观察现有 Phase1–8 PostgreSQL 行情和 Stage1 结果；没有已批准策略时保持拒绝下单。</p></div>
    <Notice state={state}/>
    <div className="status-strip">{[['Readiness',runtime.readiness],['运行模式',runtime.mode],['Data Source',runtime.data_source??data?.data_source],['LIVE',runtime.live],['对账',runtime.reconciliation],['进程',processHealth.status],['Collector 心跳',collectorHealth.status],['真实行情',marketHealth.status]].map(([label,value])=><div key={String(label)}><span>{String(label)}</span><Badge value={value}/></div>)}</div>
    {runtime.final_action==='DO NOT TRADE' && <div className="notice warning"><strong>PAPER NOT READY · DO NOT TRADE</strong><span>{blockerText||'尚未取得所有就绪证明。'}</span></div>}
    <div className="metrics">
      {[['uptime_seconds','运行秒数'],['last_market_event','最后行情事件'],['last_decision','最后评估'],['last_trade','最后交易']].map(([key,label])=><div className="metric" key={key}><div className="metric-label">{label}</div><div className="metric-value">{runtime[key]==null?'N/A':key.endsWith('_seconds')?text(runtime[key]):time(runtime[key])}</div></div>)}
    </div>
    <div className="two-cols">
      <Panel title="BTC / ETH 公开行情" subtitle="数据来自现有 canonical PostgreSQL；价格、mark、Funding 保留源状态。">
        <Table rows={symbols} columns={[
          {key:'symbol',label:'SYMBOL'},
          {key:'price',label:'LAST'},
          {key:'mark_price',label:'MARK'},
          {key:'data_age_seconds',label:'AGE / SEC'},
          {key:'last_update',label:'RECEIVED (UTC)',render:time},
          {key:'data_source',label:'SOURCE',render:(_,row)=><span>{text(row.feeds&&list(row.feeds)[0]?.source)}</span>},
          {key:'funding',label:'FUNDING',render:v=><span>{text((v as RecordData|undefined)?.rate)} · {text((v as RecordData|undefined)?.status)}</span>},
        ]} empty="等待现有 collector 将真实公共行情写入 canonical PostgreSQL。"/>
      </Panel>
      <Panel title="Readiness Gate" subtitle="任何关键条件缺失都会停止新 Paper order。">
        {checks.length?<Table rows={checks} columns={[{key:'component',label:'检查项'},{key:'status',label:'状态',render:v=><Badge value={v}/>},{key:'reason',label:'原因'}]}/>:<Empty title="尚无就绪记录" detail="启动 realtime-paper monitor 后显示逐项检查。"/>}
      </Panel>
    </div>
    <Panel title="Strategy Inspector" subtitle="展示实际 Phase1 Stage1 输出；它是筛选分类，不是 LONG/SHORT 订单信号。">
      <Table rows={symbols.map(row=>({symbol:row.symbol,...(row.stage1 as RecordData??{}),phase9:'NOT_CONFIGURED'}))} columns={[
        {key:'symbol',label:'SYMBOL'},
        {key:'structure',label:'STRUCTURE'},
        {key:'category',label:'STAGE1'},
        {key:'classification',label:'分类'},
        {key:'reason',label:'原因'},
        {key:'phase9',label:'PHASE9'},
        {key:'final_action',label:'最终动作',render:()=> <Badge value="DO NOT TRADE"/>},
      ]} empty="当前没有 BTC/ETH Stage1 观察记录。"/>
    </Panel>
    <Panel title="Decision → Risk Gate" subtitle="每轮保存 canonical input digest 和真实 Stage1 输出；未就绪时不构造伪造 DecisionCandidate 或 ExecutionIntent。">
      <Table rows={latest} columns={[
        {key:'symbol',label:'SYMBOL'},
        {key:'created_at',label:'TIME',render:time},
        {key:'decision',label:'STAGE1 ACTION'},
        {key:'risk_decision',label:'RISK'},
        {key:'risk_reason',label:'BLOCK REASONS',render:v=>text(Array.isArray(v)?v.join(', '):v)},
        {key:'final_action',label:'FINAL'},
      ]} empty="没有 decision ledger 记录。"/>
    </Panel>
    <Panel title="Data Freshness" subtitle="分别记录 source event age、ingest lag、processing lag；事件时间缺失时不会伪装成源事件年龄。">
      <Table rows={feeds} columns={[
        {key:'symbol',label:'SYMBOL'},
        {key:'data_type',label:'DATA TYPE'},
        {key:'kind',label:'FEED'},
        {key:'timeframe',label:'INTERVAL'},
        {key:'source',label:'SOURCE'},
        {key:'source_event_age',label:'EVENT AGE / SEC'},
        {key:'observation_age',label:'OBSERVATION AGE / SEC'},
        {key:'window_age_seconds',label:'WINDOW AGE / SEC'},
        {key:'ingest_lag',label:'INGEST LAG / SEC'},
        {key:'processing_lag',label:'PROCESS LAG / SEC'},
        {key:'soft_threshold_seconds',label:'SOFT / SEC'},
        {key:'hard_threshold_seconds',label:'HARD / SEC'},
        {key:'status',label:'STATUS',render:v=><Badge value={v}/>},
      ]} empty="没有可用行情 freshness 记录。"/>
    </Panel>
    <div className="inline-note">Session {text(data?.session?.session_id)} · strategy {text(runtime.strategy_version)} · config {text(data?.session?.config_hash)} · orders {text(data?.counts?.orders)} · trades {text(data?.counts?.trades)} · risk rejects {text(data?.counts?.risk_rejects)} · errors {text(data?.counts?.errors)} · GPT DISABLED · JEV NOT_CONFIGURED</div>
    <SourceFoot response={state.response as Envelope<unknown>|null}/>
  </>
}
