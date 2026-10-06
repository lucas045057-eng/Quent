import { act, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const wrap = (data:unknown) => ({schema:'DASHBOARD_API_V1',data,availability:'AVAILABLE',observed_at:'2026-09-29T01:00:00Z',sources:['artifacts/phase9/backtest.json'],warnings:[]})
const run = {run_id:'a'.repeat(24),canonical_symbol:'BTC-USDT-PERP',side:'LONG',status:'PASSED',
  strategy:'TREND_CONTINUATION',acceptance_kind:'FIXTURE_DRIVEN_ACCEPTANCE',strategy_edge_status:'NOT_VALIDATED',
  metrics:{net_pnl:'-2.39366142',fees:'0.34166142',funding_cash:'-0.095',sharpe:null,initial_equity:null,final_equity:null}}
const detail = {...run,source:'artifacts/phase9/backtest.json',equity_curve:[],drawdown_curve:[],trades:[],
  record:{reason:'<script>window.secret=true</script>'},curve_basis:'NOT_RECORDED'}

function network(responses:Record<string,unknown>) {
  vi.stubGlobal('fetch',vi.fn(async (url:string) => new Response(JSON.stringify(
    responses[url] ?? wrap(url==='/api/overview'?{mode:'IDLE',metrics:{}}:{items:[]})
  ),{status:200})))
}
async function app(route:string) {
  window.location.hash='#/'+route
  const entry = '../App.tsx'
  const {default:App} = await import(/* @vite-ignore */ entry)
  return render(<App/>)
}
beforeEach(()=>vi.unstubAllGlobals())
afterEach(()=>vi.useRealTimers())

it('selects an actual backtest with signed metrics and honest missing curve and trades',async()=>{
  network({'/api/backtests':wrap({items:[run]}),['/api/backtests/'+run.run_id]:wrap(detail)})
  await app('backtests')
  await userEvent.click(await screen.findByRole('button',{name:/BTC-USDT-PERP/}))
  expect(await screen.findByText('净值曲线未记录')).toBeInTheDocument()
  expect(screen.getByText('逐笔成交未记录')).toBeInTheDocument()
  expect(within(screen.getByTestId('metric-net_pnl')).getByText('-2.39')).toBeInTheDocument()
  expect(within(screen.getByTestId('metric-sharpe')).getByText('N/A')).toBeInTheDocument()
  expect(screen.getAllByText(/NOT_VALIDATED/).length).toBeGreaterThan(0)
})

it('renders charts only for real timestamped points supplied by the API',async()=>{
  const series = [{time:'2026-09-01T00:00:00Z',equity:'1000'},{time:'2026-09-01T01:00:00Z',equity:'900'}]
  network({'/api/backtests':wrap({items:[run]}),['/api/backtests/'+run.run_id]:wrap({...detail,
    equity_curve:series,drawdown_curve:series.map((point,index)=>({time:point.time,drawdown_pct:index?'-10':'0'}))})})
  await app('backtests')
  await userEvent.click(await screen.findByRole('button',{name:/BTC-USDT-PERP/}))
  expect(await screen.findByRole('img',{name:'Equity Curve · 2 recorded points'},{timeout:10000})).toBeInTheDocument()
  expect(screen.getByRole('img',{name:'Drawdown · 2 recorded points'})).toBeInTheDocument()
})

it('shows observed health components and unavailable monitor fields',async()=>{
  network({'/api/health':wrap({components:[{component:'quant_core',status:'STALE',source:'runtime_health_events'},
    {component:'database',status:'NOT_CONFIGURED',source:'readonly monitoring'},
    {component:'jev',status:'NOT_CONFIGURED',source:'baseline'},
    {component:'live',status:'DISABLED',source:'policy'}],resources:{dashboard_rss_bytes:33554432,cpu_percent:null},
    database_missing_tables:[],recorded_health_events:[],controls:'READ_ONLY'})})
  await app('health')
  expect(await screen.findByText('Quant Core')).toBeInTheDocument()
  expect(screen.getByText('STALE')).toBeInTheDocument()
  expect(screen.getByText('32.00')).toBeInTheDocument()
  expect(within(screen.getByTestId('metric-cpu_percent')).getByText('N/A')).toBeInTheDocument()
})

it('filters real events by level and search and offers safe detail',async()=>{
  const event={event_id:'event1',time:'2026-09-29T01:00:00Z',level:'ERROR',module:'funding',
    message:'provider failed',details:{authorization:'[REDACTED]',reason:'<script>window.secret=true</script>'},source:'paper.log'}
  network({'/api/logs':wrap({items:[event],modules:['funding']}),
    '/api/logs?level=ERROR':wrap({items:[event],modules:['funding']})})
  await app('logs')
  expect(await screen.findByText('provider failed')).toBeInTheDocument()
  await userEvent.selectOptions(screen.getByLabelText('日志级别'),'ERROR')
  await waitFor(()=>expect(vi.mocked(fetch).mock.calls.some(([url])=>String(url).includes('level=ERROR'))).toBe(true))
  await userEvent.click(screen.getByRole('button',{name:'查看事件 event1'}))
  expect(screen.getByText(/window.secret=true/)).toBeInTheDocument()
  expect(document.querySelector('script')).toBeNull()
})

it('shows no backtest rows when actual report files are absent',async()=>{
  network({'/api/backtests':wrap({items:[]})})
  await app('backtests')
  expect(await screen.findByText('没有可读取的 Backtest 报告')).toBeInTheDocument()
})

it('polling keeps last successful data explicitly stale after a failed refresh',async()=>{
  vi.useFakeTimers()
  const {usePolling} = await import('../api')
  let calls=0
  vi.stubGlobal('fetch',vi.fn(async()=>{ if (++calls>1) throw new Error('offline'); return new Response(JSON.stringify(wrap({equity:'42'}))) }))
  const view = renderHook(()=>usePolling<{equity:string}>('/api/state'))
  await act(async()=>{await Promise.resolve();await Promise.resolve()})
  expect(view.result.current.response?.data.equity).toBe('42')
  await act(async()=>{await vi.advanceTimersByTimeAsync(3000)})
  expect(view.result.current.stale).toBe(true)
  expect(view.result.current.error).toBeTruthy()
  expect(view.result.current.response?.data.equity).toBe('42')
})

it('polling never overlaps pending requests and cancels on unmount',async()=>{
  vi.useFakeTimers()
  const {usePolling} = await import('../api')
  let signal:AbortSignal|undefined
  vi.stubGlobal('fetch',vi.fn((_url:string,options:RequestInit)=>{signal=options.signal as AbortSignal;return new Promise(()=>{})}))
  const view = renderHook(()=>usePolling('/api/state'))
  await act(async()=>{await vi.advanceTimersByTimeAsync(4000)})
  expect(fetch).toHaveBeenCalledTimes(1)
  view.unmount()
  expect(signal?.aborted).toBe(true)
})
