import { useState } from 'react'
import { usePolling } from '../api'
import type { DecisionDetail, ItemList, RecordData } from '../types'
import { Badge, Empty, Fields, Notice, Panel, Raw, SourceFoot, Table, text, time } from '../components/Ui'

export default function Decisions() {
  const [id,setId] = useState('')
  const [filter,setFilter] = useState('')
  const [step,setStep] = useState('decision')
  const list = usePolling<ItemList>('/api/decisions')
  const detail = usePolling<DecisionDetail|null>(id?'/api/decisions/'+encodeURIComponent(id):null)
  const data = detail.response?.data
  const rows = (list.response?.data.items??[]).filter(item=>`${item.symbol} ${item.direction_bias} ${item.matched_pattern} ${item.decision_id}`.toLowerCase().includes(filter.toLowerCase()))
  const choose = (row:RecordData) => { setId(String(row.decision_id)); setStep('decision') }
  const decision = data?.decision
  const selectedValue = data?.[step]
  return <><div className="page-lead"><span className="eyebrow">DECISION INTELLIGENCE / 决策追踪</span><h1>Decision Inspector</h1><p>从原始证据到执行意图，检查交易与未交易的实际原因。</p></div>
    <div className="inspector"><Panel title="DecisionCandidate" subtitle={`最近记录 · ${rows.length} 条`} className="decision-list"><input aria-label="搜索决策" placeholder="搜索 symbol / pattern / ID" value={filter} onChange={e=>setFilter(e.target.value)}/><Notice state={list}/>
      {!rows.length?<Empty detail="没有可读取的决策记录。审计数据库未配置时不会生成示例信号。"/>:<div className="candidate-list">{rows.map(row=><button key={String(row.decision_id)} className={id===row.decision_id?'selected':''} onClick={()=>choose(row)} aria-label={`${row.symbol} ${row.timeframe} ${row.decision_id}`}><div><strong>{text(row.symbol)}</strong><span>{text(row.timeframe)}</span><Badge value={row.eligible===true?'PASS':'REJECT'}/></div><p>{text(row.matched_pattern)}</p><small>{time(row.created_at)}</small></button>)}</div>}<SourceFoot response={list.response}/></Panel>
      <div className="decision-detail"><Notice state={detail}/>{!data?<Panel title="证据链详情" subtitle="选择一个实际 DecisionCandidate"><Empty title="等待选择" detail="打开左侧记录，查看证据质量、匹配条件、Veto、Jev、Risk 和 ExecutionIntent。"/></Panel>:<>
        <Panel title={`${text(decision?.symbol)} · ${text(decision?.direction_bias)}`} subtitle={text(decision?.decision_id)} action={<Badge value={decision?.eligible===true?'PASS':'REJECT'}/>}><p className="decision-summary">{text(decision?.short_summary)}</p><div className="decision-facts"><span>{text(decision?.matched_pattern)}</span><Badge value={decision?.pattern_status}/><Badge value={decision?.confidence_band}/><span>TTL {time(decision?.valid_until)}</span></div><div className="reason-chips">{(Array.isArray(decision?.reason_codes)?decision.reason_codes:[]).map(reason=><span key={String(reason)}>{String(reason)}</span>)}</div></Panel>
        <Panel title="Decision → Execution" subtitle="点击每一步检查该阶段的记录"><div className="chain">{data.chain.map((item,index)=><button key={item.id} className={step===item.detail?'selected':''} onClick={()=>setStep(item.detail)} aria-label={`${item.label} ${item.status}`}><small>{String(index+1).padStart(2,'0')}</small><strong>{item.label}</strong><Badge value={item.status}/></button>)}</div></Panel>
        <Panel title={data.chain.find(item=>item.detail===step)?.label??step} subtitle="保留原始 UNKNOWN / PARTIAL / MISSING 语义">
          {step==='decision' && <><Fields data={data.decision} fields={[["decision_policy_version","Decision policy"],["pattern_policy_version","Pattern policy"],["code_version","Code version"],["input_snapshot_hash","Input digest"]]}/><Raw value={data.decision.veto_reasons} label="Veto reasons"/><Raw value={data.decision.missing_evidence} label="Missing evidence"/></>}
          {step==='evidence' && <Table rows={data.evidence} columns={[{key:'evidence_type',label:'TYPE'},{key:'semantic_code',label:'SEMANTIC'},{key:'direction',label:'方向'},{key:'availability_status',label:'AVAILABLE',render:v=><Badge value={v}/>},{key:'freshness_status',label:'FRESHNESS',render:v=><Badge value={v}/>},{key:'quality_status',label:'QUALITY',render:v=><Badge value={v}/>},{key:'interpretation',label:'解释'},{key:'source_ref',label:'SOURCE'}]}/ >}
          {step==='risk' && <><Badge value={data.risk.status}/><Fields data={data.risk} fields={[["basis","记录依据"],["risk_policy_version","Risk policy"],["risk_budget","Risk budget"],["rejection_reason","拒绝原因"]]}/><p className="inline-note">没有 Intent 不等于 Risk 拒绝。未持久化的 Risk 结果显示 NOT_RECORDED。</p></>}
          <Raw value={selectedValue} label="查看该阶段脱敏记录"/>
        </Panel><Panel title="Jev review / lifecycle" subtitle="真实审计记录"><Raw value={data.jev_reviews} label="Jev review"/><Raw value={data.lifecycle} label="Lifecycle events"/><Raw value={data.evidence_chain} label="Evidence chain / supporting / conflicting / degraded"/></Panel><SourceFoot response={detail.response}/>
      </>}</div></div>
  </>
}
