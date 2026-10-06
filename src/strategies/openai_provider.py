"""Approved OpenAI-compatible Responses endpoints behind the existing AIService."""
from decimal import Decimal as D
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, HTTPRedirectHandler, build_opener
from quant_phase6.ai import AIService, AIResponse, AIUsage, AIErrorCode, ProviderError
from .service_config import configured_services
from .analysis.deep_analyzer import AnalysisPolicyV2, ProposalResponse

ENDPOINT='https://api.openai.com/v1/responses'
APPROVED_ENDPOINTS=frozenset((ENDPOINT,'https://xfastapi.ai/responses'))

class OpenAIRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        raise HTTPError(req.full_url,code,'AI_REDIRECT_REJECTED',headers,fp)

def openai_json_transport(url,body,headers,timeout):
    if url not in APPROVED_ENDPOINTS:raise ProviderError(AIErrorCode.POLICY_BLOCK)
    request=Request(url,data=json.dumps(body,separators=(',',':'),allow_nan=False).encode(),
        headers={**headers,'Content-Type':'application/json','Accept':'application/json'},method='POST')
    try:
        with build_opener(OpenAIRedirectHandler()).open(request,timeout=timeout) as response:
            if response.url!=url:raise ProviderError(AIErrorCode.POLICY_BLOCK)
            raw=response.read(1024*1024+1)
            if len(raw)>1024*1024:raise ProviderError(AIErrorCode.SCHEMA_ERROR)
            return json.loads(raw)
    except HTTPError as exc:
        # Never expose API bodies, headers or the original exception text.
        code=AIErrorCode.AUTHENTICATION if exc.code in (401,403) else AIErrorCode.RATE_LIMIT if exc.code==429 else AIErrorCode.PROVIDER_REJECTED
        raise ProviderError(code) from None
    except (TimeoutError,URLError) as exc:
        code=AIErrorCode.TIMEOUT if isinstance(exc,TimeoutError) else AIErrorCode.TRANSPORT
        raise ProviderError(code) from None
    except (ValueError,OSError):raise ProviderError(AIErrorCode.TRANSPORT) from None

def proposal_json_schema():
    schema=ProposalResponse.model_json_schema()
    def strict(node):
        if isinstance(node,dict):
            node.pop('default',None)
            if node.get('type')=='object':
                node['additionalProperties']=False
                node['required']=list(node.get('properties',{}))
                if 'schema_version' in node.get('properties',{}):
                    node['properties']['schema_version']={'type':'string','enum':['STRATEGY_V2']}
            for child in node.values():strict(child)
        elif isinstance(node,list):
            for child in node:strict(child)
    strict(schema)
    schema['properties']['proposals'].update(minItems=3,maxItems=3)
    return schema

class OpenAIResponsesProvider:
    test_only=False
    def __init__(self,config,*,transport=openai_json_transport):
        self.config=config;self.transport=transport
        self.endpoint=config.base_url+'/responses'
        self.provider_name='xfastapi' if config.base_url=='https://xfastapi.ai' else 'openai'

    @property
    def maximum_cost(self):
        # One token per encoded request byte plus overhead is a conservative
        # bound, not a tokenizer-derived invoice. No tools or image input.
        schema_bytes=len(json.dumps(proposal_json_schema()).encode())
        return ((self.config.max_input_bytes+schema_bytes+4096)*max(self.config.input_usd_per_million,self.config.cached_input_usd_per_million)
            +self.config.max_output_tokens*self.config.output_usd_per_million)/D(1000000)

    def complete(self,request):
        key=self.config.api_key.get_secret_value().strip()
        if not key:raise ProviderError(AIErrorCode.AUTHENTICATION)
        if request.provider!=self.provider_name or request.model!=self.config.model:
            raise ProviderError(AIErrorCode.POLICY_BLOCK)
        text=request.envelope.untrusted_data;instructions=request.envelope.system_instructions
        if len(text.encode())+len(instructions.encode())>self.config.max_input_bytes:
            raise ProviderError(AIErrorCode.POLICY_BLOCK)
        body={'model':request.model,'store':False,'stream':False,'instructions':instructions,
            'input':[{'role':'user','content':[{'type':'input_text','text':text}]}],
            'max_output_tokens':self.config.max_output_tokens,
            'text':{'format':{'type':'json_schema','name':'quant_paper_v2_analysis','strict':True,'schema':proposal_json_schema()}}}
        started=time.monotonic()
        try:
            raw=self.transport(self.endpoint,body,{'Authorization':'Bearer '+key},min(request.timeout_seconds,self.config.timeout_seconds))
        except ProviderError:raise
        except Exception:raise ProviderError(AIErrorCode.TRANSPORT) from None
        usage=AIUsage(estimated_cost=self.maximum_cost)
        output={}
        try:
            u=raw['usage'];i=u['input_tokens'];o=u['output_tokens'];cached=u.get('input_tokens_details',{}).get('cached_tokens',0)
            if any(type(n)!=int or n<0 for n in (i,o,cached)) or cached>i:raise ValueError('usage')
            cost=((i-cached)*self.config.input_usd_per_million+cached*self.config.cached_input_usd_per_million+o*self.config.output_usd_per_million)/D(1000000)
            usage=AIUsage(input_tokens=i,cached_input_tokens=cached,output_tokens=o,total_tokens=i+o,
                estimated_cost=cost,latency_ms=int((time.monotonic()-started)*1000))
        except (ValueError,KeyError,TypeError):pass
        try:
            if raw.get('status')!='completed' or raw.get('error'):raise ValueError('incomplete')
            content=[c for item in raw['output'] if item.get('type')=='message' for c in item.get('content',[])]
            if len(content)!=1 or content[0].get('type')!='output_text':raise ValueError('refusal')
            text=content[0]['text']
            if not isinstance(text,str) or len(text.encode())>request.max_output_bytes:raise ValueError('size')
            output=json.loads(text)
            if not isinstance(output,dict):output={}
        except (AttributeError,ValueError,TypeError,KeyError):pass
        # Even refused/truncated/invalid paid responses reach gateway accounting
        # before the existing schema and cited-fact checks reject the output.
        return AIResponse(provider=self.provider_name,model=request.model,structured_output=output,usage=usage)

def create_ai_binding(*,env=None,settings=None,transport=openai_json_transport):
    env=os.environ if env is None else env
    try:config=configured_services(env).openai
    except ValueError:return None,AnalysisPolicyV2()
    if not config.enabled or not config.api_key.get_secret_value().strip():return None,AnalysisPolicyV2()
    if settings is None:
        from quant_phase1.config import Settings
        settings=Settings.from_env(env)
    if config.network_transport=='windows_system_proxy' and transport is openai_json_transport:
        from .windows_transport import windows_system_transport
        transport=windows_system_transport
    provider=OpenAIResponsesProvider(config,transport=transport)
    service=AIService.from_settings({provider.provider_name:provider},settings)
    return service,AnalysisPolicyV2(provider_name=provider.provider_name,model=config.model,
        timeout_seconds=config.timeout_seconds,estimated_cost=provider.maximum_cost)
