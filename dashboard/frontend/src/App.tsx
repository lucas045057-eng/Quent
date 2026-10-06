import { Component, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { usePolling } from './api'
import type { OverviewData } from './types'
import { Badge, Empty, time } from './components/Ui'
import Overview from './pages/Overview'
import Paper from './pages/Paper'
import Decisions from './pages/Decisions'
import Backtests from './pages/Backtests'
import Health from './pages/Health'
import Logs from './pages/Logs'
import RealtimePaper from './pages/RealtimePaper'
import Strategy from './pages/Strategy'
import RiskPolicy from './pages/RiskPolicy'

const navigation = [{id:'overview',label:'Overview',subtitle:'总览',icon:'▦'},
  {id:'paper',label:'Paper Trading',subtitle:'模拟执行',icon:'◈'},
  {id:'realtime',label:'Realtime Readiness',subtitle:'真实行情就绪',icon:'⌁'},
  {id:'strategy',label:'Strategy V2',subtitle:'策略研究',icon:'◇'},
  {id:'risk',label:'Risk Policy',subtitle:'风险配置',icon:'▣'},
  {id:'decisions',label:'Decision Inspector',subtitle:'决策证据链',icon:'⌘'},
  {id:'backtests',label:'Backtest',subtitle:'回测研究',icon:'⌁'},
  {id:'health',label:'System Health',subtitle:'系统健康',icon:'⊕'},
  {id:'logs',label:'Logs / Events',subtitle:'日志与事件',icon:'≡'}]

class Guard extends Component<{children:ReactNode},{failed:boolean}> {
  state={failed:false}
  static getDerivedStateFromError(){return {failed:true}}
  render(){return this.state.failed?<Empty title="页面无法读取数据" detail="请刷新页面，并检查本地后台状态。"/>:this.props.children}
}

export default function App() {
  const route = () => window.location.hash.replace('#/','') || 'overview'
  const [page,setPage] = useState(route)
  const overview = usePolling<OverviewData>('/api/overview')
  useEffect(()=>{const change=()=>setPage(route()); window.addEventListener('hashchange',change); return()=>window.removeEventListener('hashchange',change)},[])
  const active = navigation.find(item=>item.id===page)??navigation[0]
  const connected = !!overview.response && !overview.error
  return <div className="shell"><aside className="sidebar"><div className="brand"><div className="brand-mark">Q<span>↗</span></div><div><strong>QUANT CORE</strong><small>LOCAL OPERATIONS</small></div></div><div className="nav-label">WORKSPACE</div><nav aria-label="主导航">{navigation.map(item=><a key={item.id} href={'#/'+item.id} aria-label={item.label} aria-current={active.id===item.id?'page':undefined} className={active.id===item.id?'active':''}><span className="nav-icon">{item.icon}</span><span>{item.label}<small>{item.subtitle}</small></span>{active.id===item.id && <i/>}</a>)}</nav>
    <div className="sidebar-bottom"><div className="local-card"><span className="eyebrow">EXECUTION MODE</span><strong>{overview.response?.data.mode??'N/A'}<span className="mode-dot"/></strong><p>127.0.0.1 · 本地只读</p></div><div className="sidebar-foot"><span>Dashboard V1</span><span>↗</span></div></div></aside>
    <div className="workspace"><header className="topbar"><div className="breadcrumb">Quant / <strong>{active.label}</strong></div><div className="topbar-right"><span className={`connection ${connected?'connected':''}`}><i/>{overview.error?'API DISCONNECTED':connected?'API CONNECTED':'CONNECTING'}</span><span className="live-lock"><span aria-hidden="true">▣ </span><span>LIVE DISABLED</span></span><span className="utc-clock">{overview.response?time(overview.response.observed_at):'UTC · N/A'}</span></div></header>
      <main><Guard key={active.id}>{active.id==='overview'?<Overview state={overview}/>:active.id==='paper'?<Paper/>:active.id==='realtime'?<RealtimePaper/>:active.id==='strategy'?<Strategy/>:active.id==='risk'?<RiskPolicy/>:active.id==='decisions'?<Decisions/>:active.id==='backtests'?<Backtests/>:active.id==='health'?<Health/>:<Logs/>}</Guard><footer><span>QUANT + NAUTILUS / LOCAL OBSERVABILITY</span><span>GET ONLY · POLLING 3s</span></footer></main></div>
  </div>
}
