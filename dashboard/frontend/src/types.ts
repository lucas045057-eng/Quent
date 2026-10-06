export type RecordData = Record<string, unknown>
export interface Envelope<T> { schema:'DASHBOARD_API_V1'; data:T; availability:string; observed_at:string; sources:string[]; warnings:string[] }
export interface ApiState<T> { response:Envelope<T>|null; loading:boolean; error:string|null; stale:boolean }
export interface OverviewData { system:string; mode:string; paper:string; live:string; jev:string; funding:string; funding_source:string; database:string; reconciliation:string|null; metrics:Record<string,string|number|null>; metric_scope:string|null; pnl_definition:string }
export interface ItemList { items:RecordData[]; database?:string; execution_audit?:RecordData[]; explanation?:string; modules?:string[] }
export interface PaperSession extends RecordData { session_id:string; source:string; freshness:string; state:string; position:RecordData; heartbeat_at:string|null; pid:number|null; acceptance_kind:string|null }
export interface PaperData { state:string; current:PaperSession|null; currents:PaperSession[]; sessions:PaperSession[] }
export interface ChainStep { id:string; label:string; status:string; detail:string }
export interface DecisionDetail extends RecordData { decision:RecordData; snapshot:RecordData|null; evidence:RecordData[]; evidence_chain:RecordData|null; patterns:RecordData[]; jev_reviews:RecordData[]; lifecycle:RecordData[]; risk:RecordData; intents:RecordData[]; execution_audit:RecordData[]; chain:ChainStep[] }
export interface BacktestDetail extends RecordData { run_id:string; metrics:RecordData; equity_curve:{time:string;equity:string}[]; drawdown_curve:{time:string;drawdown_pct:string}[]; trades:RecordData[]; source:string; acceptance_kind:string|null; strategy_edge_status:string|null }
export interface HealthData { components:{component:string;status:string;source:string}[]; resources:RecordData; database_missing_tables:string[]; recorded_health_events:RecordData[]; controls:string; process_pid:number }

export interface StrategyV2Data { strategy_version:string;horizons:string[];timeframes:string[];items:RecordData[];waiting_triggers:RecordData[];analyses:RecordData[];theses:RecordData[];execution_results:RecordData[];data_source:string;scope:string;generated_at:string|null }
export interface RiskPolicyData { status:string;new_risk_allowed:boolean;reason:string|null;config:RecordData|null;config_digest:string|null;scope:string;quote_safety_seconds:number }
