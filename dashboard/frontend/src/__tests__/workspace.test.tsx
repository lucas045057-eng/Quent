import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const wrap = (data: unknown, availability='AVAILABLE') => ({ schema:'DASHBOARD_API_V1', data, availability,
  observed_at:'2026-09-29T01:00:00Z', sources:['actual-source'], warnings:[] })
const metrics = { equity:null,available_balance:null,unrealized_pnl:null,realized_pnl:null,total_pnl:null,
  position_count:null,orders_today:null,trades_today:null }
const overview = { system:'NO_DATA',mode:'IDLE',paper:'STOPPED',live:'DISABLED',jev:'NOT_CONFIGURED',
  funding:'ADAPTER_IMPLEMENTED',funding_source:'NO_DATA',database:'NOT_CONFIGURED',reconciliation:null, metrics }
const decision = { decision_id:'00000000-0000-4000-8000-000000000001', evaluation_id:'eval-1',symbol:'BTCUSDT',
  timeframe:'1H',eligible:false,direction_bias:'BULLISH',confidence_band:'LOW',matched_pattern:'TREND_CONTINUATION',
  pattern_status:'PARTIAL_MATCH',reason_codes:['MISSING_REQUIRED_EVIDENCE'],short_summary:'Funding unavailable',
  veto_reasons:['DATA_PARTIAL'],missing_evidence:[{reason:'NOT_AVAILABLE'}],created_at:'2026-09-29T00:00:00Z',
  valid_until:'2026-09-29T00:15:00Z',decision_policy_version:'decision-1',code_version:'a'.repeat(40) }

function network(overrides: Record<string,unknown> = {}) {
  const responses: Record<string,unknown> = {
    '/api/overview':wrap(overview), '/api/decisions':wrap({items:[],database:'NOT_CONFIGURED'},'NO_DATA'),
    '/api/logs?limit=5':wrap({items:[]},'NO_DATA'), '/api/paper':wrap({state:'STOPPED',current:null,currents:[],sessions:[]},'NO_DATA'),
    '/api/positions':wrap({items:[]},'NO_DATA'), '/api/orders':wrap({items:[],execution_audit:[]},'NO_DATA'),
    '/api/trades':wrap({items:[],explanation:'NO_EXACT_PER_FILL_TRADE_RECORDS'},'NO_DATA'),
    ...overrides,
  }
  vi.stubGlobal('fetch',vi.fn(async (url: string) => {
    const body = responses[url] ?? responses[url.split('?')[0]] ?? wrap({items:[]},'NO_DATA')
    return new Response(JSON.stringify(body),{status:200,headers:{'Content-Type':'application/json'}})
  }))
}
async function app() {
  const entry = '../App.tsx'
  const { default: App } = await import(/* @vite-ignore */ entry)
  return render(<App />)
}

beforeEach(() => { window.location.hash='#/overview'; vi.unstubAllGlobals(); network() })

describe('real-data workspace states', () => {
  it('offers six routes and a permanent Live lock with no enable action',async () => {
    await app()
    for (const label of ['Overview','Paper Trading','Decision Inspector','Backtest','System Health','Logs / Events']) {
      expect(screen.getByRole('link',{name:label})).toBeInTheDocument()
    }
    expect(screen.getByText('LIVE DISABLED')).toBeInTheDocument()
    expect(screen.queryByRole('button',{name:/enable live/i})).not.toBeInTheDocument()
  })

  it('shows null monetary facts as N/A, never fabricated zero equity',async () => {
    await app()
    const card = await screen.findByTestId('metric-equity')
    expect(within(card).getByText('N/A')).toBeInTheDocument()
    expect(within(card).queryByText('0.00')).not.toBeInTheDocument()
    expect(screen.getAllByText('NOT_CONFIGURED').length).toBeGreaterThan(0)
  })

  it('formats a recorded balance and preserves signed PnL',async () => {
    network({'/api/overview':wrap({...overview,metrics:{...metrics,equity:'9999.71',total_pnl:'-2.39366142'}})})
    await app()
    await waitFor(() => expect(within(screen.getByTestId('metric-equity')).getByText('9,999.71')).toBeInTheDocument())
    expect(within(screen.getByTestId('metric-total_pnl')).getByText('-2.39')).toBeInTheDocument()
  })

  it('shows a visible loading state while the API is pending',async () => {
    vi.stubGlobal('fetch',vi.fn(() => new Promise(() => {})))
    await app()
    expect(screen.getAllByText('正在读取数据…').length).toBeGreaterThan(0)
  })

  it('shows a disconnected state when the API cannot be reached',async () => {
    vi.stubGlobal('fetch',vi.fn(async () => { throw new Error('network unavailable') }))
    await app()
    expect(await screen.findByText('API DISCONNECTED')).toBeInTheDocument()
    expect(screen.getAllByText(/无法连接本地 API/).length).toBeGreaterThan(0)
  })

  it('keeps current Paper empty and lets the user inspect a marked historical session',async () => {
    window.location.hash='#/paper'
    const session = {session_id:'a'.repeat(24),source:'artifacts/phase9/ready.json',freshness:'STALE',state:'STOPPED',
      heartbeat_at:'2026-09-28T12:00:00Z',pid:2974,position:{quantity:'0.019'},acceptance_kind:'FIXTURE_DRIVEN_ACCEPTANCE'}
    network({'/api/paper':wrap({state:'STOPPED',current:null,currents:[],sessions:[session]},'STALE'),
      ['/api/positions?session='+session.session_id]:wrap({items:[{canonical_symbol:'BTC-USDT-PERP',quantity:'0.019',freshness:'STALE'}]},'STALE')})
    await app()
    expect(await screen.findByText('当前没有已验证的 Paper 进程')).toBeInTheDocument()
    await userEvent.selectOptions(screen.getByLabelText('Paper 会话'), session.session_id)
    expect(await screen.findByText('HISTORICAL SNAPSHOT')).toBeInTheDocument()
    expect(await screen.findByText('BTC-USDT-PERP')).toBeInTheDocument()
    expect(screen.queryByRole('button',{name:/启动 Paper/})).not.toBeInTheDocument()
  })

  it('selects a real decision and opens its evidence and Risk chain steps',async () => {
    window.location.hash='#/decisions'
    const chain = [{id:'market',label:'Market Data',status:'RECORDED',detail:'snapshot'},
      {id:'evidence',label:'Evidence',status:'RECORDED',detail:'evidence'},
      {id:'risk',label:'Risk',status:'NOT_RECORDED',detail:'risk'}]
    network({'/api/decisions':wrap({items:[decision]}),
      ['/api/decisions/'+decision.decision_id]:wrap({decision,chain,snapshot:{as_of:'2026-09-29T00:00:00Z'},
        evidence:[{evidence_id:'e1',evidence_type:'TRADE_FLOW',availability_status:'PARTIAL',quality_status:'UNKNOWN',
          freshness_status:'STALE',interpretation:'Original unknown flow',source_ref:'phase3:1'}],patterns:[],
        risk:{status:'NOT_RECORDED',basis:'NO_SEPARATE_PERSISTED_RISK_REJECTION'},intents:[],execution_audit:[],jev_reviews:[],lifecycle:[]})})
    await app()
    await userEvent.click(await screen.findByRole('button',{name:/BTCUSDT.*1H/}))
    expect(await screen.findByText('Funding unavailable')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button',{name:/Evidence RECORDED/}))
    expect(await screen.findByText('Original unknown flow')).toBeInTheDocument()
    expect(screen.getByText('UNKNOWN')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button',{name:/Risk NOT_RECORDED/}))
    expect(screen.getAllByText('NOT_RECORDED').length).toBeGreaterThan(0)
    expect(screen.queryByText('Risk REJECTED')).not.toBeInTheDocument()
  })
})
