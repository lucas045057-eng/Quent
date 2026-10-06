import { expect, it } from 'vitest'
import { screen } from '@testing-library/react'
import { app, network, wrap } from './test-helpers'

it('shows canonical realtime readiness and never presents an unapproved strategy as trade-ready', async () => {
  const runtime = {mode:'REALTIME PAPER',data_source:'REAL_PUBLIC_DATA',live:'DISABLED',
    readiness:'PAPER NOT READY',final_action:'DO NOT TRADE',blockers:['candidate_validity','strategy'],
    uptime_seconds:120,last_market_event:'2026-09-29T01:00:00Z',last_decision:'2026-09-29T01:00:01Z',
    last_trade:null,reconciliation:'NOT_READY',
    health:{process_alive:{status:'ALIVE'},collector_heartbeat:{status:'AVAILABLE'},fresh_market_data:{status:'STALE'}},
    checks:[{component:'strategy',status:'BLOCKED',reason:'PHASE9_DISABLED'}],
    symbols:[{symbol:'BTCUSDT',price:'65000',mark_price:'65001',data_age_seconds:1,
      last_update:'2026-09-29T01:00:00Z',feeds:[{source:'bitget_v3_ws',kind:'TICKER_MARK',status:'HEALTHY'}],
      funding:{rate:null,status:'NOT_AVAILABLE'},stage1:{category:'B',classification:'WAIT_TRIGGER',reason:'WAIT_FOR_TRIGGER',structure:'BULLISH'}}],
    counts:{orders:0,trades:0,risk_rejects:1,errors:0},strategy_version:'phase1-basic-v1'}
  network({'/api/realtime-paper':wrap({session:{session_id:'b'.repeat(24),config_hash:'c'.repeat(64)},
    runtime,decisions:[{symbol:'BTCUSDT',risk_decision:'REJECTED_BY_READINESS_GATE',
      risk_reason:['strategy'],final_action:'DO NOT TRADE'}],events:[],counts:runtime.counts,data_source:'REAL_PUBLIC_DATA'})})
  await app('realtime')
  expect(await screen.findByText('PAPER NOT READY')).toBeInTheDocument()
  expect(screen.getAllByText('DO NOT TRADE').length).toBeGreaterThan(0)
  expect(screen.getAllByText('bitget_v3_ws').length).toBeGreaterThan(0)
  expect(screen.getByText('Collector 心跳')).toBeInTheDocument()
  expect(screen.getByText('真实行情')).toBeInTheDocument()
  expect(screen.getByText('EVENT AGE / SEC')).toBeInTheDocument()
  expect(screen.getByText('OBSERVATION AGE / SEC')).toBeInTheDocument()
  expect(screen.getByText('WINDOW AGE / SEC')).toBeInTheDocument()
  expect(screen.getByText('INGEST LAG / SEC')).toBeInTheDocument()
  expect(screen.getByText('PROCESS LAG / SEC')).toBeInTheDocument()
  expect(screen.getAllByText(/PHASE9_DISABLED|strategy/).length).toBeGreaterThan(0)
})
