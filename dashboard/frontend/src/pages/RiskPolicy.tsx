import { usePolling } from '../api'
import type { RiskPolicyData, RecordData } from '../types'
import { Badge, Fields, Notice, Panel, SourceFoot, Table } from '../components/Ui'
import JsonPanel from '../components/JsonPanel'
const percent=(v:unknown)=>v==null?'N/A':Number.isFinite(Number(v))?(Number(v)*100).toFixed(2)+'%':'N/A'
export default function RiskPolicy(){
 const state=usePolling<RiskPolicyData>('/api/risk-policy');const data=state.response?.data;const config=data?.config??{}
 const holds=config.max_hold_seconds_by_horizon as RecordData|undefined
 return <><div className="page-lead"><span className="eyebrow">RISK CONFIG V2 / 只读配置</span><h1>Risk Policy</h1><p>在本地配置文件修改参数并提高 revision。新版本只影响后续 intent，已有仓位继续遵循创建时的计划。</p></div><Notice state={state}/>
 <Panel title="生效状态"><div className="monitor-fields"><Badge value={data?.status}/><Badge value={data?.new_risk_allowed?'NEW_RISK_ALLOWED':'NEW_RISK_BLOCKED'}/></div><Fields data={{reason:data?.reason,revision:config.revision,digest:data?.config_digest,scope:data?.scope,safety:data?.quote_safety_seconds}} fields={[["reason","错误原因"],["revision","最后有效版本"],["digest","配置摘要"],["scope","生效范围"],["safety","盘口安全时效（秒）"]]}/></Panel>
 <Panel title="风险与仓位预算" subtitle="比例基于 Paper 账户净值；可选绝对上限仍会限制最终数量。"><Fields data={{risk:percent(config.risk_per_trade_equity_ratio),position:percent(config.max_position_notional_equity_ratio),total:percent(config.max_total_exposure_equity_ratio),reserved:percent(config.max_reserved_risk_equity_ratio),leverage:config.max_leverage,positions:config.max_open_positions,intents:config.max_open_intents,sizing:config.sizing_mode,cooldown:config.cooldown_seconds}} fields={[["risk","单笔风险"],["position","单仓名义金额"],["total","总敞口"],["reserved","总预留风险"],["leverage","最大杠杆"],["positions","最大仓位数"],["intents","最大待执行数"],["sizing","仓位计算方式"],["cooldown","冷却时间（秒）"]]}/></Panel>
 <Panel title="持仓期限"><Table rows={['1_3H','3_8H','8_24H'].map(h=>({horizon:h,seconds:holds?.[h]}))} columns={[{key:'horizon',label:'研究窗口'},{key:'seconds',label:'最大持仓秒数'}]}/></Panel><JsonPanel value={config} label="完整有效配置"/><SourceFoot response={state.response}/></>
}
