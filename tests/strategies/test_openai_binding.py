import json
import pytest
from decimal import Decimal as D
from strategies.analysis.deep_analyzer import analyze_candidate
from tests.strategies.test_deep_analyzer import complete_snapshot

def response(snap,status='completed',extra=None):
    proposals=[dict(schema_version='STRATEGY_V2',horizon=h,bias='LONG',confidence='HIGH',
        market_structure=snap.candidate.structure,summary='SYNTHETIC_FIXTURE: verified facts agree',
        source_refs=[snap.observations[0].source_ref],fact_digests=[snap.observations[0].digest],contradictions=[])
        for h in ('1_3H','3_8H','8_24H')]
    body={'schema_version':'STRATEGY_V2','proposals':proposals}
    if extra:body.update(extra)
    return {'status':status,'model':'gpt-5.4-mini-2026-03-17',
        'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(body)}]}],
        'usage':{'input_tokens':100,'output_tokens':200,'total_tokens':300,'input_tokens_details':{'cached_tokens':20}}}

def binding(tmp_path,transport,key='SYNTHETIC_THREE',**options):
    from strategies.openai_provider import create_ai_binding
    path=tmp_path/'services.json'
    path.write_text(json.dumps({'openai':{'api_key':key,**options}}))
    return create_ai_binding(env={'QUANT_V2_SERVICES_CONFIG_PATH':str(path)},transport=transport)

def test_three_key_config_and_missing_ai_do_not_call(tmp_path):
    from strategies.service_config import ResearchServicesConfig
    config=ResearchServicesConfig()
    assert config.openai.api_key.get_secret_value()==''
    assert not hasattr(config,'xoomar') and not hasattr(config,'gnews') and not hasattr(config,'events')
    gateway,policy=binding(tmp_path,lambda *a:pytest.fail('blank key must not call'),key='')
    assert gateway is None and policy.provider_name=='UNCONFIGURED'

def test_openai_responses_binding_uses_existing_gateway_and_evidence_checks(tmp_path):
    snap=complete_snapshot();calls=[]
    def transport(url,body,headers,timeout):
        calls.append((url,body,headers,timeout));return response(snap)
    gateway,policy=binding(tmp_path,transport)
    theses=analyze_candidate(snap,provider=gateway,policy=policy)
    assert len(theses)==3 and all(t.bias=='LONG' for t in theses)
    url,body,headers,timeout=calls[0]
    assert url=='https://api.openai.com/v1/responses'
    assert headers['Authorization']=='Bearer SYNTHETIC_THREE'
    assert body['store'] is False and body['text']['format']['strict'] is True
    assert body['text']['format']['type']=='json_schema' and 'tools' not in body
    assert body['input'][0]['role']=='user'
    assert 'SYNTHETIC_THREE' not in json.dumps(body) and 0<timeout<=10
    schema=body['text']['format']['schema']
    assert schema['additionalProperties'] is False
    assert set(schema['required'])==set(schema['properties'])
    assert gateway.budget._records[0][1]==D('.0009615')
    analyze_candidate(snap,provider=gateway,policy=policy)
    assert len(calls)==1  # gateway cache shared within factory

@pytest.mark.parametrize('options',[{'status':'incomplete'},{'extra':{'execution_intent':{'quantity':'10'}}}])
def test_incomplete_or_executable_output_blocks_research_but_accounts_cost(tmp_path,options):
    snap=complete_snapshot()
    gateway,policy=binding(tmp_path,lambda *a:response(snap,**options))
    assert all(t.bias=='WAIT' for t in analyze_candidate(snap,provider=gateway,policy=policy))
    assert len(gateway.budget._records)==1

def test_invalid_ai_file_cannot_fallback_to_env(tmp_path):
    from strategies.openai_provider import create_ai_binding
    path=tmp_path/'bad.json';path.write_text('{')
    gateway,policy=create_ai_binding(env={'QUANT_V2_SERVICES_CONFIG_PATH':str(path),'OPENAI_API_KEY':'DO_NOT_PRINT'})
    assert gateway is None and policy.provider_name=='UNCONFIGURED'

def test_ai_transport_errors_are_redacted(tmp_path):
    from quant_phase6.ai import ProviderError,AIErrorCode
    snap=complete_snapshot()
    def transport(*args):raise RuntimeError('SYNTHETIC_THREE')
    gateway,policy=binding(tmp_path,transport)
    theses=analyze_candidate(snap,provider=gateway,policy=policy)
    assert all(t.bias=='WAIT' for t in theses)
    assert 'SYNTHETIC_THREE' not in repr(theses)

def test_factory_automatically_binds_file_without_network(tmp_path,monkeypatch):
    from strategies.runtime import CanonicalResearchFactory
    from strategies.execution.execution_policy import ExecutionPolicyV2
    path=tmp_path/'services.json';path.write_text(json.dumps({'openai':{'api_key':'SYNTHETIC_THREE'}}))
    monkeypatch.setenv('QUANT_V2_SERVICES_CONFIG_PATH',str(path))
    factory=CanonicalResearchFactory('NOT_A_REAL_DSN',policy=ExecutionPolicyV2())
    assert factory.provider is not None and factory.analysis_policy.provider_name=='openai'


def test_openai_transport_rejects_redirects_and_redacts_auth_errors(monkeypatch):
    from urllib.request import Request
    from urllib.error import HTTPError
    from strategies.openai_provider import OpenAIRedirectHandler,openai_json_transport,ENDPOINT
    from quant_phase6.ai import ProviderError,AIErrorCode
    with pytest.raises(HTTPError):
        OpenAIRedirectHandler().redirect_request(Request(ENDPOINT,headers={'Authorization':'SECRET'}),
            None,302,'redirect',{},'https://untrusted.example/read')
    class Opener:
        def open(self,*args,**kwargs):raise HTTPError(ENDPOINT,401,'SYNTHETIC_THREE',{},None)
    monkeypatch.setattr('strategies.openai_provider.build_opener',lambda *a:Opener())
    with pytest.raises(ProviderError) as error:openai_json_transport(ENDPOINT,{}, {'Authorization':'SECRET'},1)
    assert error.value.code==AIErrorCode.AUTHENTICATION and 'SYNTHETIC_THREE' not in str(error.value)


def test_explicit_xfastapi_responses_binding_preserves_provenance_and_key(tmp_path):
    snap=complete_snapshot();calls=[]
    def transport(url,body,headers,timeout):
        calls.append((url,body,headers));raw=response(snap);raw['model']='gpt-5.6-sol';return raw
    gateway,policy=binding(tmp_path,transport,base_url='https://xfastapi.ai',model='gpt-5.6-sol')
    assert gateway is not None and policy.provider_name=='xfastapi'
    theses=analyze_candidate(snap,provider=gateway,policy=policy)
    assert all(t.bias=='LONG' for t in theses)
    url,body,headers=calls[0]
    assert url=='https://xfastapi.ai/responses' and body['model']=='gpt-5.6-sol'
    assert headers['Authorization']=='Bearer SYNTHETIC_THREE'
    assert 'SYNTHETIC_THREE' not in json.dumps(body)

def test_configured_ai_host_does_not_allow_unrelated_domains(tmp_path):
    gateway,policy=binding(tmp_path,lambda *a:pytest.fail('unapproved host'),base_url='https://untrusted.example/v1')
    assert gateway is None and policy.provider_name=='UNCONFIGURED'


def test_windows_proxy_transport_keeps_key_off_process_command(monkeypatch):
    import strategies.windows_transport as module
    from types import SimpleNamespace
    seen={}
    def run(command,**kwargs):
        seen.update(command=command,kwargs=kwargs)
        return SimpleNamespace(returncode=0,stdout=json.dumps({'http_status':200,'body':{'status':'completed'}}))
    monkeypatch.setattr(module.subprocess,'run',run)
    monkeypatch.setattr(module,'powershell_executable',lambda:'APPROVED_POWERSHELL')
    result=module.windows_system_transport('https://xfastapi.ai/responses',{'model':'gpt-5.6-sol'},
        {'Authorization':'Bearer SYNTHETIC_THREE'},5)
    assert result['status']=='completed'
    assert 'SYNTHETIC_THREE' not in repr(seen['command'])
    assert 'SYNTHETIC_THREE' in seen['kwargs']['input']
    assert seen['kwargs']['timeout']<=7

def test_windows_network_mode_is_explicit_and_keeps_existing_gateway(tmp_path,monkeypatch):
    from strategies.openai_provider import create_ai_binding
    import strategies.windows_transport as module
    snap=complete_snapshot();calls=[]
    def transport(*a):calls.append(a);raw=response(snap);raw['model']='gpt-5.6-sol';return raw
    monkeypatch.setattr(module,'windows_system_transport',transport)
    path=tmp_path/'services.json';path.write_text(json.dumps({'openai':{'api_key':'SYNTHETIC_THREE',
        'base_url':'https://xfastapi.ai','model':'gpt-5.6-sol','network_transport':'windows_system_proxy'}}))
    gateway,policy=create_ai_binding(env={'QUANT_V2_SERVICES_CONFIG_PATH':str(path)})
    assert gateway is not None
    assert all(t.bias=='LONG' for t in analyze_candidate(snap,provider=gateway,policy=policy))
    assert len(calls)==1


def test_windows_transport_blocks_other_hosts_before_launch(monkeypatch):
    import strategies.windows_transport as module
    from quant_phase6.ai import ProviderError,AIErrorCode
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**kw:pytest.fail('must not launch'))
    with pytest.raises(ProviderError) as error:
        module.windows_system_transport('https://untrusted.example/responses',{},
            {'Authorization':'Bearer SYNTHETIC_THREE'},5)
    assert error.value.code==AIErrorCode.POLICY_BLOCK


@pytest.mark.parametrize('status,expected',[(302,'PROVIDER_REJECTED'),(401,'AUTHENTICATION'),(429,'RATE_LIMIT')])
def test_windows_transport_rejects_redirect_auth_and_rate_errors_without_details(monkeypatch,status,expected):
    import strategies.windows_transport as module
    from quant_phase6.ai import ProviderError
    from types import SimpleNamespace
    monkeypatch.setattr(module,'powershell_executable',lambda:'APPROVED_POWERSHELL')
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0,
        stdout=json.dumps({'http_status':status,'error':'SYNTHETIC_THREE'})))
    with pytest.raises(ProviderError) as error:
        module.windows_system_transport('https://xfastapi.ai/responses',{},
            {'Authorization':'Bearer SYNTHETIC_THREE'},5)
    assert error.value.code.value==expected and 'SYNTHETIC_THREE' not in str(error.value)


def test_real_prompt_supplies_exact_fact_ids_that_evidence_validator_requires():
    from strategies.analysis.prompts import analysis_prompt
    from strategies.analysis.deep_analyzer import AnalysisPolicyV2,validate_proposal
    snap=complete_snapshot();envelope=analysis_prompt(snap,AnalysisPolicyV2())
    payload=json.loads(envelope.untrusted_data)
    row=payload['observations'][0]
    assert row['fact_digest']==snap.observations[0].digest
    assert snap.observations[0].source_ref==row['source_ref']
    validate_proposal({'horizon':'1_3H','bias':'LONG','confidence':'HIGH','market_structure':snap.candidate.structure,
        'summary':'facts agree','source_refs':[row['source_ref']],'fact_digests':[row['fact_digest']]},snap)


def test_runtime_supplied_services_file_reaches_ai_and_external_sources(tmp_path,monkeypatch):
    from strategies.runtime import CanonicalResearchFactory
    from strategies.execution.execution_policy import ExecutionPolicyV2
    from strategies.contracts import AnalysisRequest
    from strategies.market_view import MarketView,SymbolMarket
    import strategies.providers as sources
    import strategies.runtime as runtime
    from tests.strategies.test_service_replacement import NOW,A,transport_fixture
    from datetime import datetime,timedelta
    class FrozenClock(datetime):
        @classmethod
        def now(cls,tz=None):return NOW
    monkeypatch.setattr(runtime,'datetime',FrozenClock)
    path=tmp_path/'services.json'
    path.write_text(json.dumps({'coinalyze':{'api_key':'SYNTHETIC_ONE'},
        'openai':{'api_key':'SYNTHETIC_THREE','base_url':'https://xfastapi.ai','model':'gpt-5.6-sol'}}))
    monkeypatch.delenv('QUANT_V2_SERVICES_CONFIG_PATH',raising=False)
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    env={'QUANT_V2_SERVICES_CONFIG_PATH':str(path)}
    factory=CanonicalResearchFactory('NOT_A_REAL_DSN',policy=ExecutionPolicyV2(),environ=env)
    assert factory.provider is not None and factory.analysis_policy.provider_name=='xfastapi'
    env.clear()  # runtime must retain its own configuration snapshot
    calls=[];original=sources.PublicResearchSources
    monkeypatch.setattr(sources,'PublicResearchSources',lambda **kw:original(
        **kw,transport=transport_fixture(calls),clock=lambda:NOW))
    quote=complete_snapshot().observations[0].model_copy(update={'kind':'PRICE','fetched_at':NOW})
    monkeypatch.setattr(factory,'view',lambda now, **kwargs:MarketView(as_of=now,symbols=(
        SymbolMarket(symbol=A.symbol,observations=(quote,)),)))
    rows=factory.refresh(AnalysisRequest(candidate=A,requested_at=NOW,deadline=NOW+timedelta(seconds=15),screening_digest='a'*64))
    assert next(r for r in rows if r.kind=='CROSS_OI').availability=='AVAILABLE'
    assert not any(r.kind in {'EVENT_COVERAGE','EXCHANGE_EVENT_COVERAGE','MACRO_COVERAGE'} for r in rows)
    assert not any(any(host in c[0] for host in ('gnews.io','xoomar.com','api.bitget.com')) for c in calls)
