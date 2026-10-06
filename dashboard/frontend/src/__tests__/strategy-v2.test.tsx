import { screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { app, network, wrap } from './test-helpers'
afterEach(()=>vi.unstubAllGlobals())
it('shows research horizons separately from bar timeframes and unknown sources',async()=>{
 network({'/api/strategy-v2':wrap({strategy_version:'QUANT_PAPER_V2',horizons:['1_3H','3_8H','8_24H'],timeframes:['15m','1H','4H'],data_source:'UNKNOWN',items:[{symbol:'SOLUSDT',category:'B',structure:'RANGE_BOUNDARY',reason_codes:['WAITING_FOR_TRIGGER']}],waiting_triggers:[{symbol:'SOLUSDT',status:'WAITING',trigger:{price:'150',operator:'GTE'},generated_at:'2026-10-03T00:00:00Z'}],theses:[],analyses:[],execution_results:[],scope:'RECORDED_RESEARCH'})})
 await app('strategy')
 expect(await screen.findByRole('heading',{name:'Strategy V2'})).toBeInTheDocument()
 expect((await screen.findAllByText('SOLUSDT')).length).toBe(2)
 for(const h of ['1_3H','3_8H','8_24H'])expect(screen.getAllByText(h).length).toBeGreaterThan(0)
 expect(screen.getAllByText('UNKNOWN').length).toBeGreaterThan(0)
 expect(screen.getByText('WAITING_FOR_TRIGGER')).toBeInTheDocument()
})
it('shows the edited risk revision, error and new-intent-only scope',async()=>{
 network({'/api/risk-policy':wrap({status:'ERROR',new_risk_allowed:false,reason:'RISK_CONFIG_INVALID_OR_UNAVAILABLE',scope:'NEW_INTENTS_ONLY',quote_safety_seconds:5,config_digest:'a'.repeat(64),config:{revision:2,risk_per_trade_equity_ratio:'0.005',max_position_notional_equity_ratio:'0.2',max_total_exposure_equity_ratio:'0.3',max_hold_seconds_by_horizon:{'1_3H':12000,'3_8H':15000,'8_24H':20000}}})})
 await app('risk')
 expect(await screen.findByRole('heading',{name:'Risk Policy'})).toBeInTheDocument()
 expect(await screen.findByText('RISK_CONFIG_INVALID_OR_UNAVAILABLE')).toBeInTheDocument()
 expect(screen.getByText('0.50%')).toBeInTheDocument()
 expect(screen.getByText('NEW_INTENTS_ONLY')).toBeInTheDocument()
 expect(screen.queryByRole('button',{name:/保存/})).not.toBeInTheDocument()
})
