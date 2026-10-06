import { usePolling } from '../api'
import type { StrategyV2Data, RecordData } from '../types'
import { Badge, Fields, Notice, Panel, SourceFoot, Table, time, text } from '../components/Ui'
import JsonPanel from '../components/JsonPanel'
export default function Strategy(){
 const state=usePolling<StrategyV2Data>('/api/strategy-v2');const data=state.response?.data
 const triggers=(data?.waiting_triggers??[]).map(row=>({...row,trigger_price:(row.trigger as RecordData)?.price,operator:(row.trigger as RecordData)?.operator}))
 const facts=(data?.analyses??[]).flatMap(a=>(Array.isArray(a.observations)?a.observations:[]).map(o=>({...o as RecordData})))
 return <><div className="page-lead"><span className="eyebrow">QUANT PAPER V2 / 研究状态</span><h1>Strategy V2</h1><p>先筛选 A/B/C/D，再刷新候选数据。研究窗口、K 线周期与执行结果分别记录。</p></div><Notice state={state}/>
 <Panel title="研究窗口"><div className="monitor-fields">{[['1_3H','15m'],['3_8H','1H'],['8_24H','4H']].map(([h,tf])=><div key={h}><strong>{h}</strong><span>K 线 {tf}</span></div>)}</div><Fields data={{strategy:data?.strategy_version,source:data?.data_source??'UNKNOWN',scope:data?.scope,generated:time(data?.generated_at)}} fields={[["strategy","策略"],["source","数据来源"],["scope","记录范围"],["generated","筛选时间 UTC"]]}/></Panel>
 <Panel title="批量筛选" subtitle="A：进入深度分析 · B：等待确认 · C：缺乏优势 · D：数据或条件不满足。最多 5 个 A，允许 0 个。"><Table rows={data?.items??[]} columns={[{key:'symbol',label:'合约'},{key:'category',label:'分类',render:v=><Badge value={v}/>},{key:'structure',label:'结构'},{key:'reason_codes',label:'原因',render:v=>Array.isArray(v)?v.join(' · '):text(v)}]}/></Panel>
 <Panel title="等待触发"><Table rows={triggers} columns={[{key:'symbol',label:'合约'},{key:'status',label:'状态',render:v=><Badge value={v}/>},{key:'trigger_price',label:'触发价格'},{key:'operator',label:'条件'},{key:'generated_at',label:'生成时间 UTC',render:time}]}/></Panel>
 <Panel title="三个窗口的判断"><Table rows={data?.theses??[]} columns={[{key:'symbol',label:'合约'},{key:'horizon',label:'研究窗口'},{key:'timeframe',label:'K 线周期'},{key:'bias',label:'判断'},{key:'confidence',label:'置信度'},{key:'no_trade_reason',label:'不交易原因'},{key:'generated_at',label:'分析时间 UTC',render:time}]}/></Panel>
 <Panel title="来源时间与覆盖"><Table rows={facts} columns={[{key:'kind',label:'证据'},{key:'provider',label:'来源'},{key:'observed_at',label:'来源时间 UTC',render:time},{key:'fetched_at',label:'获取时间 UTC',render:time},{key:'availability',label:'可用性',render:v=><Badge value={v}/>},{key:'freshness',label:'时效',render:v=><Badge value={v}/>},{key:'coverage',label:'覆盖',render:v=><Badge value={v}/>}]}/></Panel>
 <JsonPanel value={data?.execution_results??[]} label="确定性执行政策结果"/><SourceFoot response={state.response}/></>
}
