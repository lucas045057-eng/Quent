"""V2 is the sole new-candidate producer; execution remains in Phase9/Paper."""
from strategies.providers import RESEARCH_PROVIDERS
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import time
import logging
from contextlib import contextmanager
from quant_execution.risk_config import RiskConfigLoader
from quant_phase1.stage1 import Stage1Result
from quant_phase1.contracts import DataStatus, RawReference
from .contracts import AnalysisRequest, ScreeningBatch
from .screener.batch_screener import screen_market, ScreeningPolicyV2
from .market_view import build_market_view
from .refresh import refresh_candidate
from .analysis.deep_analyzer import analyze_candidate, AnalysisPolicyV2
from .integration.phase9_bridge import StrategyExecutionInputs
from .execution.execution_policy import ExecutionPolicyV2

def sampled_instant(read_start, batch):
    """The instant a canonical read is asserted as-of.

    Freshness budgets are defined against *source* observation clocks, so the
    assertion instant is when the inputs were sampled. A read that takes
    seconds must not be charged to every input as additional age. The instant
    is raised only when a newer ticker clock landed while the read was in
    flight, so a fresh concurrent write is never mistaken for future data.
    """
    newest=read_start
    for ticker in getattr(batch,"tickers",()) or ():
        observed_at=getattr(ticker,"exchange_timestamp",None)
        if isinstance(observed_at,datetime) and observed_at>newest:newest=observed_at
    return newest

class StrategyRuntimeV2:
    def __init__(self, *, view_loader, risk_loader=None, position_manager=None, screening_policy=None):
        self.view_loader=view_loader; self.risk_loader=risk_loader
        self.position_manager=position_manager; self.screening_policy=screening_policy or ScreeningPolicyV2()
        self.risk_state=None; self.new_risk_allowed=False
    def evaluate_cycle(self, *, now):
        # Exits/protection happen before any entry/research dependency.
        if self.position_manager is not None:self.position_manager(now)
        if self.risk_loader is not None:
            self.risk_state=self.risk_loader.poll();self.new_risk_allowed=self.risk_state.new_risk_allowed
        view=self.view_loader(now)
        return screen_market(view,policy=self.screening_policy)

def stage1_results(batch):
    return [Stage1Result(symbol=c.symbol,category=c.category,reason=';'.join(c.reason_codes),
        status=DataStatus.NOT_AVAILABLE if c.category=='D' else DataStatus.AVAILABLE,
        inputs_used=tuple(n.observation.source_ref for n in c.evidence),indicators={},structure=c.structure,
        reason_codes=c.reason_codes,key_metrics={'screening_digest':batch.digest,'direction':c.direction or 'UNKNOWN'},
        data_snapshot_reference=RawReference(provider='QUANT_PAPER_V2',endpoint='screening',reason='IMMUTABLE_SCREENING'),
        timestamp=batch.generated_at,strategy_version='QUANT_PAPER_V2') for c in batch.candidates]

def screen_batch(batch, *, now=None, connection=None):
    now=now or batch.collected_at
    view=build_market_view(batch,as_of=now)
    if connection is not None:
        from .sources import enrich_view
        view=enrich_view(connection,view,batch=batch)
    return StrategyRuntimeV2(view_loader=lambda _:view).evaluate_cycle(now=now)

def persist_screening(connection, screening):
    from .persistence import persist_artifact
    return persist_artifact(connection,kind='SCREENING',record=screening,now=screening.generated_at)

class ResearchDeadlineExceeded(TimeoutError):
    def __init__(self,stage):
        self.stage=stage
        super().__init__('V2_RESEARCH_DEADLINE_'+stage)

@contextmanager
def research_stage(stage,symbol,deadline):
    start=time.monotonic()
    try:
        if start>=deadline:raise ResearchDeadlineExceeded(stage)
        yield
        if time.monotonic()>=deadline:raise ResearchDeadlineExceeded(stage)
    except TimeoutError as exc:
        raise ResearchDeadlineExceeded(stage) from exc
    finally:
        logging.getLogger('quant_phase9').info('v2_research_stage symbol=%s stage=%s elapsed_ms=%d',
            symbol,stage,int((time.monotonic()-start)*1000))

class CanonicalResearchFactory:
    """Bounded fresh read of the existing collector, never receipt-time relabeling."""
    data_source='REAL_PUBLIC_DATA'
    def __init__(self,dsn,*,policy,provider=None,analysis_policy=None,source_factory=None,environ=None):
        self.env=dict(os.environ if environ is None else environ)
        if provider is None and analysis_policy is None:
            from .openai_provider import create_ai_binding
            provider,analysis_policy=create_ai_binding(env=self.env)
        self.dsn=dsn;self.policy=policy;self.provider=provider
        self.analysis_policy=analysis_policy or AnalysisPolicyV2()
        self.source_factory=source_factory
    def view(self,now, *, symbols=None):
        import psycopg
        from quant_phase1.repositories import Phase1Repository
        from .sources import enrich_view
        with psycopg.connect(self.dsn,connect_timeout=2,options='-c default_transaction_read_only=on -c statement_timeout=2000') as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            kwargs={"limit":max(200,len(self.policy.allowed_symbols)),"candle_limit":100}
            if symbols is not None:
                kwargs.update(limit=len(symbols),requested_symbols=symbols)
            batch=Phase1Repository(conn).load_latest_market_batch(**kwargs)
            if batch is None:raise ValueError('CANONICAL_MARKET_UNAVAILABLE')
            view=enrich_view(conn,build_market_view(batch,as_of=now),batch=batch)
            from .canonical_validation import attest_canonical_read
            checked=datetime.now(timezone.utc)
            # This is a genuine repeatable-read receipt. Original HTTP fetch
            # clocks, closed-window clocks, values and source ages are unchanged.
            return view.model_copy(update={'as_of':checked,'symbols':tuple(m.model_copy(update={'observations':tuple(
                attest_canonical_read(o,checked_at=checked) for o in m.observations)}) for m in view.symbols)})
    def __call__(self,event,*,deadline):
        import psycopg
        projection=event.canonical_payload.get('stage1_projection',{})
        digest=projection.get('key_metrics',{}).get('screening_digest')
        with psycopg.connect(self.dsn,connect_timeout=2,options='-c default_transaction_read_only=on -c statement_timeout=2000') as conn:
            row=conn.execute("SELECT payload FROM strategy_v2_artifacts WHERE artifact_kind='SCREENING' AND canonical_digest=%s",(digest,)).fetchone()
        if row is None:raise ValueError('DURABLE_V2_SCREENING_UNAVAILABLE')
        screening=ScreeningBatch.model_validate(row[0])
        candidate=next((c for c in screening.top_candidates if c.symbol==event.symbol),None)
        if candidate is None or event.stage1_policy_version!='QUANT_PAPER_V2':raise ValueError('V2_STAGE_A_REQUIRED')
        requested=datetime.now(timezone.utc)
        seconds=max(.001,min(15,deadline-time.monotonic()))
        request=AnalysisRequest(candidate=candidate,requested_at=requested,deadline=requested+timedelta(seconds=seconds),screening_digest=screening.digest)
        source=self.source_factory(self) if self.source_factory else self
        with research_stage('SOURCE_REFRESH',event.symbol,deadline):
            analysis=refresh_candidate(request,source=source)
            if datetime.now(timezone.utc)>request.deadline:raise ResearchDeadlineExceeded('SOURCE_REFRESH')
        if time.monotonic()>=deadline:raise TimeoutError('V2 research deadline reached')
        budget=max(.001,min(self.analysis_policy.timeout_seconds,deadline-time.monotonic()-1))
        with research_stage('AI_ANALYSIS',event.symbol,deadline):
            theses=analyze_candidate(analysis,provider=self.provider,policy=self.analysis_policy.model_copy(update={'timeout_seconds':budget}))
        if time.monotonic()>=deadline:raise TimeoutError("V2 research deadline reached after analysis")
        with research_stage('FINAL_CANONICAL_READ',event.symbol,deadline):
            now=datetime.now(timezone.utc);view=self.view(now,symbols=self.research_symbols(analysis.symbol))
        if time.monotonic()>=deadline:raise TimeoutError("V2 research deadline reached after final canonical read")
        external=tuple(o for o in analysis.observations if o.provider in RESEARCH_PROVIDERS)
        view=view.model_copy(update={'analysis_snapshots':(analysis,), 'symbols':tuple(m.model_copy(update={'observations':(*m.observations,*external)}) if m.symbol==analysis.symbol else m for m in view.symbols)})
        return StrategyExecutionInputs(analysis=analysis,theses=theses,view=view,policy=self.policy)
    @staticmethod
    def research_symbols(symbol):
        # The candidate and its own benchmark context; Stage1 keeps the full universe.
        return tuple(dict.fromkeys((symbol,"BTCUSDT","ETHUSDT")))

    def refresh(self,request):
        # Wait only for a new collector receipt; old source clocks are preserved.
        stop=time.monotonic()+min(3,max(0,(request.deadline-datetime.now(timezone.utc)).total_seconds()))
        view=None
        while time.monotonic()<stop:
            view=self.view(datetime.now(timezone.utc),symbols=self.research_symbols(request.candidate.symbol))
            market=next((m for m in view.symbols if m.symbol==request.candidate.symbol),None)
            if market and any(o.kind=='PRICE' and o.fetched_at and o.fetched_at>=request.requested_at for o in market.observations):break
            time.sleep(.25)
        if view is None:return ()
        market=next((m for m in view.symbols if m.symbol==request.candidate.symbol),None)
        external=[]
        from .providers import PublicResearchSources, receipt_observation
        for receipt in PublicResearchSources(env=self.env).fetch(request.candidate,deadline=request.deadline):
            external.append(receipt_observation(receipt))
        # External lookups can take seconds. Re-read the original collector
        # after them, so price/geometry do not come from the pre-lookup view.
        latest=self.view(datetime.now(timezone.utc),symbols=self.research_symbols(request.candidate.symbol))
        market=next((m for m in latest.symbols if m.symbol==request.candidate.symbol),None)
        return (*(market.observations if market else ()),*external)


def execution_policy_from_env(env=None):
    env=os.environ if env is None else env
    raw=env.get('QUANT_V2_EXECUTION_POLICY_PATH')
    path=Path(raw) if raw else Path(__file__).resolve().parents[2]/'config/strategy_policy_v2.json'
    return ExecutionPolicyV2.model_validate_json(path.read_text())


def main():
    import argparse
    from .integration.policy_manifest import build_v2_manifest
    parser=argparse.ArgumentParser(description='Generate V2 manifest from explicit research execution policy; no approval is created.')
    parser.add_argument('--policy',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();policy=ExecutionPolicyV2.model_validate_json(args.policy.read_text())
    manifest=build_v2_manifest(policy,created_at=datetime.now(timezone.utc))
    args.output.write_text(manifest.model_dump_json(indent=2,by_alias=True)+'\n')

if __name__=='__main__':main()
