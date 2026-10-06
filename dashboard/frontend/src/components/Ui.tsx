import type { ReactNode } from 'react'
import type { ApiState, Envelope, RecordData } from '../types'

export function text(value:unknown):string { return value === null || value === undefined || value === '' ? 'N/A' : (typeof value === 'object' ? JSON.stringify(value) : String(value)) }
export function number(value:unknown,digits=2):string {
  if (value === null || value === undefined || value === '') return 'N/A'
  const converted = Number(value)
  return Number.isFinite(converted) ? converted.toLocaleString('en-US',{minimumFractionDigits:digits,maximumFractionDigits:digits}) : 'N/A'
}
export function time(value:unknown):string {
  if (typeof value !== 'string') return 'N/A'
  const date = new Date(value)
  return Number.isFinite(date.getTime()) ? date.toISOString().replace('T',' ').replace(/\.\d+Z$/,' UTC') : 'N/A'
}
export function Badge({value}:{value:unknown}) {
  const label = value == null ? 'NO_DATA' : text(value)
  const tone = /ERROR|FAILED|REJECT|INVALID|DISCONNECTED|CONFLICT/.test(label) ? 'bad'
    : /RUNNING|CONNECTED|APPROVED|CURRENT|PASS|MATCHED|RECONCILED|ACTIVE|AVAILABLE|VALID|FRESH|RECORDED$/.test(label) && !/NOT_|PARTIAL|STALE/.test(label) ? 'good'
    : /PARTIAL|STALE|DEGRADED|UNKNOWN|WAIT|NOT_RECORDED/.test(label) ? 'warn' : 'neutral'
  return <span className={`badge ${tone}`}><span className="badge-dot"/>{label}</span>
}
export function Panel({title,subtitle,action,children,className=''}:{title:string;subtitle?:string;action?:ReactNode;children:ReactNode;className?:string}) {
  return <section className={`panel ${className}`}><div className="panel-head"><div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div>{action}</div>{children}</section>
}
export function Empty({title='NO DATA',detail='当前数据源没有记录可显示的数据。'}:{title?:string;detail?:string}) {
  return <div className="empty"><span className="empty-icon">∅</span><strong>{title}</strong><p>{detail}</p></div>
}
export function Notice<T>({state}:{state:ApiState<T>}) {
  if (state.loading) return <div role="status" className="notice loading"><span className="spinner"/>正在读取数据…</div>
  if (state.error) return <div role="alert" className="notice error">{state.error}{state.stale && <span>保留上次数据 · STALE · {time(state.response?.observed_at)}</span>}</div>
  return null
}
export function SourceFoot<T>({response}:{response:Envelope<T>|null}) {
  if (!response) return null
  return <div className="source-foot"><Badge value={response.availability}/><span>观测 {time(response.observed_at)}</span>
    <details><summary>数据来源 · {response.sources.length}</summary><ul>{response.sources.map(source => <li key={source}>{source}</li>)}</ul>{response.warnings.map(warning => <p key={warning}>{warning}</p>)}</details></div>
}
export function Metric({id,label,value,unit,integer=false}:{id:string;label:string;value:unknown;unit?:string;integer?:boolean}) {
  const tone = id.includes('pnl') && value != null ? (Number(value)<0?'loss':'profit') : ''
  return <div className="metric" data-testid={`metric-${id}`}><div className="metric-label">{label}<span>↗</span></div>
    <div className={`metric-value ${tone}`} title={text(value)}>{number(value,integer?0:2)}{value != null && unit && <small>{unit}</small>}</div>
    <div className="metric-note">{value == null?'未记录 · NO DATA':'已记录 · 见数据来源'}</div></div>
}
export interface Column {key:string;label:string;render?:(value:unknown,row:RecordData)=>ReactNode}
export function Table({rows,columns,empty,select}:{rows:RecordData[];columns:Column[];empty?:string;select?:(row:RecordData)=>void}) {
  if (!rows.length) return <Empty detail={empty}/>
  return <div className="table-scroll"><table><thead><tr>{columns.map(column => <th key={column.key}>{column.label}</th>)}</tr></thead>
    <tbody>{rows.map((row,index) => <tr key={String(row.run_id??row.decision_id??row.client_order_id??row.event_id??index)}>{columns.map((column,i) => <td key={column.key} title={text(row[column.key])}>{select && i===0 ? <button className="table-link" onClick={()=>select(row)}>{column.render?column.render(row[column.key],row):text(row[column.key])}</button> : column.render?column.render(row[column.key],row):text(row[column.key])}</td>)}</tr>)}</tbody></table></div>
}
export function Fields({data,fields}:{data:RecordData;fields:[string,string][]}) {
  return <dl className="fields">{fields.map(([key,label]) => <div key={key}><dt>{label}</dt><dd title={text(data[key])}>{text(data[key])}</dd></div>)}</dl>
}
export function Raw({value,label='查看脱敏原始记录'}:{value:unknown;label?:string}) {
  return <details className="raw"><summary>{label}</summary><pre>{JSON.stringify(value,null,2)}</pre></details>
}
