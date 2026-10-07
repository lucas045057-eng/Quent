"""Actual provider routes, source receipts and explicit capability gaps."""
from datetime import datetime, timezone
from decimal import Decimal as D
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.parse import urlencode, urlparse
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from hashlib import sha256
import json
import os
import re
from .contracts import Record, MarketObservation
from .service_config import ResearchServicesConfig, configured_services

class ProviderCapability(Record):
    provider: str
    role: str
    base_url: str
    key_env: str | None=None
    supported_kinds: tuple[str,...]
    unsupported_kinds: tuple[str,...]=()
    documentation_url: str

CAPABILITIES=(
    ProviderCapability(provider='coinalyze',role='DERIVATIVES',base_url='https://api.coinalyze.net/v1',
        key_env='COINALYZE_API_KEY',supported_kinds=('CROSS_OI','CROSS_FUNDING','LIQUIDATIONS'),
        unsupported_kinds=('HEATMAP','TOP_TRADER_RATIO','UNLOCK'),documentation_url='https://api.coinalyze.net/v1/doc/'),
    ProviderCapability(provider='public_cross_market',role='DERIVATIVES',base_url='https://fapi.binance.com',
        supported_kinds=('CROSS_OI','CROSS_FUNDING'),documentation_url='https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api-rest-api/market-data'),
)
CAPABILITY_BY_PROVIDER={capability.provider:capability for capability in CAPABILITIES}
RESEARCH_PROVIDERS=frozenset(CAPABILITY_BY_PROVIDER)


def normalize_provider_observations(payload, *, symbol, provider, required_kinds):
    rows=[]
    for raw in payload.get('observations',()):
        if raw.get('symbol',symbol)!=symbol:raise ValueError('provider symbol mismatch')
        fields=dict(raw);fields.setdefault('source_ref',provider+':'+fields['kind']);fields.setdefault('source_group',fields['kind'])
        fields.update(symbol=symbol,provider=provider)
        rows.append(MarketObservation.model_validate(fields))
    for kind in required_kinds:
        if not any(o.kind==kind for o in rows):
            rows.append(MarketObservation(symbol=symbol,kind=kind,provider=provider,source_ref=provider+':'+kind,reason='NO_COVERAGE_RECEIPT'))
    return tuple(rows)


class ProviderReceipt(Record):
    provider: str
    symbol: str
    kind: str
    endpoint: str
    fetched_at: datetime
    availability: str
    coverage: str='UNKNOWN'
    payload_json: str | None=None
    reason: str | None=None
    market_symbols: tuple[str,...]=()
    market_catalog_digest: str | None=None
    source_scope: str='UNKNOWN'
    request_parameters_json: str | None=None
    funding_periods_json: str | None=None
    coverage_checks_json: str | None=None
    calendar_validation_json: str | None=None


class ResearchRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        old,new=urlparse(req.full_url),urlparse(newurl)
        if (new.scheme,new.hostname,new.port)!=(old.scheme,old.hostname,old.port):
            raise HTTPError(req.full_url,code,'research provider redirect rejected',headers,fp)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def public_json_transport(url,params,headers,*,timeout_seconds=2):
    if not 0<timeout_seconds<=10:raise ValueError('invalid research timeout')
    parsed=urlparse(url)
    from .native_periods import PERIOD_ROUTES
    native_period_route=url in PERIOD_ROUTES.values() and not headers
    public_cross_routes={'fapi.binance.com':{'/futures/data/openInterestHist','/fapi/v1/premiumIndex','/fapi/v1/fundingRate'},
        'api.bybit.com':{'/v5/market/open-interest','/v5/market/mark-price-kline','/v5/market/tickers','/v5/market/instruments-info','/v5/market/funding/history'}}
    public_cross_route=not headers and parsed.path in public_cross_routes.get(parsed.hostname,set())
    if parsed.scheme!='https' or (parsed.hostname not in {urlparse(c.base_url).hostname for c in CAPABILITIES if c.provider != 'public_cross_market'} and not native_period_route and not public_cross_route):
        raise ValueError('unapproved research host')
    request=Request(url+('?' + urlencode(params) if params else ''),headers={**headers,'Accept':'application/json'})
    with build_opener(ResearchRedirectHandler()).open(request,timeout=timeout_seconds) as response:
        if urlparse(response.url).hostname!=parsed.hostname:raise ValueError('provider redirect rejected')
        data=response.read(4*1024*1024+1)
        if len(data)>4*1024*1024:raise ValueError('provider response too large')
        return json.loads(data)


class PublicResearchSources:
    """Candidate-only derivative research; retired event sources are not queried."""
    _catalog_cache=OrderedDict()
    _cache_lock=Lock()
    _cache_key_locks={}
    _rate_lock=Lock()
    _rate_calls={}

    def __init__(self, *, env=None, transport=public_json_transport, clock=None):
        self.env=dict(os.environ if env is None else env);self.transport=transport
        self.clock=clock or (lambda:datetime.now(timezone.utc));self.config_error=False
        try:self.config=configured_services(self.env)
        except ValueError:self.config=ResearchServicesConfig();self.config_error=True
        self.keys={
            'coinalyze':self.config.coinalyze.api_key.get_secret_value().strip(),
            'public_cross_market':'',
        }
        self._market_bindings={}

    def _enabled(self,capability):return getattr(self.config,capability.provider).enabled

    def _admit(self,key,weight):
        # Coinalyze charges each requested market as one call (40/minute).
        identity=(sha256(key.encode()).hexdigest(),self.transport)
        with self._rate_lock:
            # Read the clock while owning the allowance: a concurrent request
            # may otherwise enqueue a later reading ahead of this one.
            now=self.clock()
            calls=self._rate_calls.setdefault(identity,deque())
            while calls and (now-calls[0][0]).total_seconds()>=60:calls.popleft()
            if calls and now<calls[-1][0]:raise ValueError('RESEARCH_CLOCK_REGRESSION')
            if sum(w for _,w in calls)+weight>40:raise ValueError('PROVIDER_LOCAL_RATE_LIMIT')
            calls.append((now,weight))
            if len(self._rate_calls)>64:
                # Do not forget an active key's allowance to accommodate more keys.
                expired=[k for k,v in self._rate_calls.items() if not v or (now-v[-1][0]).total_seconds()>=60]
                for old in expired:self._rate_calls.pop(old,None)

    def _call(self,capability,path,params, *, deadline=None,weight=1,cache_seconds=0):
        now=self.clock();key=self.keys[capability.provider];endpoint=capability.base_url+path
        cache=self._catalog_cache
        identity=(endpoint,tuple(sorted(params.items())),sha256(key.encode()).hexdigest(),self.transport)
        def fetch():
            timeout_seconds=10 if deadline is None or path in ('/future-markets','/exchanges') else 5
            if deadline is not None:
                remaining=(deadline-self.clock()).total_seconds()-.1
                if remaining<=0:raise TimeoutError('RESEARCH_DEADLINE_REACHED')
                timeout_seconds=min(timeout_seconds,remaining)
                if self.transport is not public_json_transport and remaining<2:
                    raise TimeoutError('RESEARCH_DEADLINE_REACHED')
            if capability.provider=='coinalyze':self._admit(key,weight)
            headers={'api_key':key} if capability.provider=='coinalyze' else {}
            payload=(self.transport(endpoint,params,headers,timeout_seconds=timeout_seconds)
                if self.transport is public_json_transport else self.transport(endpoint,params,headers))
            if capability.provider=='coinalyze' and not isinstance(payload,list):raise ValueError('invalid Coinalyze response')
            return payload,self.clock()
        if not cache_seconds:return fetch()
        with self._cache_lock:
            gate=self._cache_key_locks.get(identity)
            if gate is None:
                if len(self._cache_key_locks)>=128:
                    idle=next((k for k,v in self._cache_key_locks.items() if v['users']==0),None)
                    if idle is None:raise ValueError('PROVIDER_CACHE_LOCK_CAP')
                    self._cache_key_locks.pop(idle)
                gate={'lock':Lock(),'users':0};self._cache_key_locks[identity]=gate
            gate['users']+=1
        acquired=False
        try:
            wait=10 if deadline is None else max(0,(deadline-self.clock()).total_seconds())
            acquired=gate['lock'].acquire(timeout=wait)
            if not acquired:raise TimeoutError('RESEARCH_CACHE_DEADLINE')
            with self._cache_lock:
                cached=cache.get(identity)
                if cached is not None and 0<=(self.clock()-cached[1]).total_seconds()<cache_seconds:
                    cache.move_to_end(identity);return cached
            result=fetch()
            with self._cache_lock:
                cache[identity]=result;cache.move_to_end(identity)
                while len(cache)>64:cache.popitem(last=False)
            return result
        finally:
            if acquired:gate['lock'].release()
            with self._cache_lock:gate['users']-=1

    def _markets(self,coin,deadline):
        capability=CAPABILITY_BY_PROVIDER['coinalyze']
        exchanges,_=self._call(capability,'/exchanges',{},deadline=deadline,cache_seconds=86400)
        markets,_=self._call(capability,'/future-markets',{},deadline=deadline,cache_seconds=86400)
        names={r['code']:r['name'] for r in exchanges if isinstance(r,dict) and isinstance(r.get('code'),str) and isinstance(r.get('name'),str)}
        eligible=[r for r in markets if isinstance(r,dict) and r.get('base_asset')==coin and r.get('quote_asset')=='USDT'
            and r.get('is_perpetual') is True and r.get('margined')=='STABLE' and r.get('exchange') in names
            and 'bitget' not in names[r['exchange']].lower() and isinstance(r.get('symbol'),str)
            and re.fullmatch(r'[A-Za-z0-9_.:/-]{2,64}',r['symbol'])]
        chosen={}
        # Prefer venues whose current native funding periods can be verified.
        # Other catalog mappings stay usable for OI but funding remains PARTIAL.
        for row in sorted(eligible,key=lambda r:(0 if names[r['exchange']].lower() in ('binance','bybit') else 1,r['exchange'],r['symbol'])):
            chosen.setdefault(row['exchange'],row)
        selected=tuple(sorted(r['symbol'] for r in list(chosen.values())[:self.config.coinalyze.max_markets]))
        if len(selected)<2:raise ValueError('CROSS_EXCHANGE_MARKET_SCOPE_UNAVAILABLE')
        catalog_digest=sha256(json.dumps({'exchanges':exchanges,'markets':markets},sort_keys=True,separators=(',',':')).encode()).hexdigest()
        selected_set=set(selected)
        self._market_bindings={r['symbol']:{'catalog_row':r,'exchange_name':names[r['exchange']]} for r in chosen.values() if r['symbol'] in selected_set}
        return selected,catalog_digest

    def fetch(self,candidate, *, deadline=None):
        if candidate.category!='A':raise ValueError('expensive sources require Stage A admission')
        if not re.fullmatch(r'[A-Z0-9]{2,24}USDT',candidate.symbol):raise ValueError('invalid USDT symbol')
        result=[];coin=candidate.symbol[:-4];market_symbols=();catalog_digest=None
        now=self.clock();to=int(now.timestamp());start=to-3*3600
        query={'symbols':'','interval':'1hour','from':start,'to':to,'convert_to_usd':'true'}
        coinalyze=CAPABILITY_BY_PROVIDER['coinalyze']
        jobs=(
            (coinalyze,'CROSS_OI','/open-interest-history',query),
            (coinalyze,'CROSS_FUNDING','/funding-rate',{'symbols':''}),
            (coinalyze,'LIQUIDATIONS','/liquidation-history',query),
        )
        discovery_lock=Lock();discovered=False;coinalyze_reason=None
        def discover():
            nonlocal discovered,market_symbols,catalog_digest,coinalyze_reason
            remaining=10 if deadline is None else max(0,(deadline-self.clock()).total_seconds())
            if not discovery_lock.acquire(timeout=remaining):
                raise TimeoutError('RESEARCH_DISCOVERY_DEADLINE')
            try:
                if not discovered:
                    try:market_symbols,catalog_digest=self._markets(coin,deadline)
                    except Exception as exc:coinalyze_reason='PROVIDER_MARKET_DISCOVERY_FAILED:'+type(exc).__name__
                    discovered=True
                return coinalyze_reason
            finally:discovery_lock.release()
        def fetch_job(job):
            capability,kind,path,params=job
            endpoint=capability.base_url+path;key=self.keys[capability.provider]
            fields=dict(provider=capability.provider,symbol=candidate.symbol,kind=kind,endpoint=endpoint,fetched_at=self.clock())
            if self.config_error:
                return ProviderReceipt(**fields,availability='ERROR',reason='RESEARCH_SERVICES_CONFIG_INVALID')
            if not self._enabled(capability):
                return ProviderReceipt(**fields,availability='UNAVAILABLE',reason='SOURCE_DISABLED')
            if capability.key_env and not key:
                return ProviderReceipt(**fields,availability='NOT_CONFIGURED',reason='MISSING_LOCAL_API_KEY')
            try:
                reason=discover()
                if reason:return ProviderReceipt(**fields,availability='UNAVAILABLE',reason=reason)
                params={**params,'symbols':','.join(market_symbols)}
                payload,receipt_time=self._call(capability,path,params,deadline=deadline,
                    weight=len(market_symbols) if capability.provider=='coinalyze' else 1)
                fields['fetched_at']=receipt_time
                periods=None
                if capability.provider=='coinalyze' and kind=='CROSS_FUNDING' and self.transport is public_json_transport:
                    from .native_periods import fetch_period_bindings
                    periods=fetch_period_bindings(self._market_bindings,catalog_digest=catalog_digest,
                        transport=self.transport,clock=self.clock,deadline=deadline)
                    fields['fetched_at']=self.clock()
                return ProviderReceipt(**fields,availability='AVAILABLE',coverage='UNKNOWN',
                    payload_json=json.dumps(payload,sort_keys=True,separators=(',',':')),
                    market_symbols=market_symbols,market_catalog_digest=catalog_digest,
                    source_scope='SELECTED_NON_BITGET_USDT_PERPETUALS',
                    request_parameters_json=json.dumps(params,sort_keys=True,separators=(',',':')),
                    funding_periods_json=periods,reason='NORMALIZATION_AND_COVERAGE_REQUIRED')
            except Exception as exc:
                return ProviderReceipt(**fields,availability='ERROR',reason='PROVIDER_REQUEST_FAILED:'+type(exc).__name__)
        # Independent derivative receipts still share the bounded candidate deadline.
        with ThreadPoolExecutor(max_workers=len(jobs),thread_name_prefix='v2-public-research') as pool:
            result.extend(pool.map(fetch_job,jobs))
        public_rows=[]
        if self.config.public_cross_market.enabled and not self.config_error:
            fields=dict(provider='public_cross_market',symbol=candidate.symbol,
                endpoint='https://fapi.binance.com+https://api.bybit.com',kind='CROSS_OI',fetched_at=self.clock())
            try:
                from .public_cross_market_transport import public_cross_market_payload
                remaining=self.config.public_cross_market.timeout_seconds if deadline is None else min(
                    self.config.public_cross_market.timeout_seconds,(deadline-self.clock()).total_seconds()-.1)
                if remaining<=0:raise TimeoutError('RESEARCH_DEADLINE_REACHED')
                payload=public_cross_market_payload(candidate.symbol,now=self.clock(),timeout_seconds=remaining,
                    network_transport=self.config.public_cross_market.network_transport,transport=self.transport)
                fetched_at=self.clock()
                shared=dict(**{**fields,'fetched_at':fetched_at},availability='AVAILABLE',coverage='PARTIAL',
                    payload_json=json.dumps(payload,sort_keys=True,separators=(',',':')),
                    source_scope='BINANCE_BYBIT_PUBLIC_TWO_VENUE_SUBSET',
                    request_parameters_json=json.dumps({'symbol':candidate.symbol,'open_interest_interval':'15m',
                        'funding_interval':'verified_native_8h','mark_price_interval':'1m'},sort_keys=True,separators=(',',':')),
                    reason='NORMALIZATION_AND_TWO_VENUE_SCOPE_REQUIRED')
                public_rows=[ProviderReceipt(**{**shared,'kind':kind}) for kind in ('CROSS_OI','CROSS_FUNDING')]
            except Exception as exc:
                public_rows=[ProviderReceipt(**{**fields,'kind':kind},availability='ERROR',reason='PUBLIC_CROSS_MARKET_REQUEST_FAILED:'+type(exc).__name__)
                    for kind in ('CROSS_OI','CROSS_FUNDING')]
            selected=[]
            for public_receipt in public_rows:
                coinalyze_receipt=next((r for r in result if r.provider=='coinalyze' and r.kind==public_receipt.kind),None)
                preferred=False
                if coinalyze_receipt is not None and coinalyze_receipt.availability=='AVAILABLE':
                    try:preferred=receipt_observation(coinalyze_receipt).usable
                    except Exception:preferred=False
                if not preferred:selected.append(public_receipt)
            result.extend(selected)
        for kind in coinalyze.unsupported_kinds:
            result.append(ProviderReceipt(provider='coinalyze',symbol=candidate.symbol,kind=kind,endpoint=coinalyze.base_url,
                fetched_at=self.clock(),availability='ERROR' if self.config_error else 'UNAVAILABLE',
                reason='RESEARCH_SERVICES_CONFIG_INVALID' if self.config_error else 'SOURCE_CAPABILITY_UNSUPPORTED'))
        return tuple(result)


def receipt_observation(receipt):
    """Normalize active derivative receipts; retired receipts stay valueless."""
    if receipt.availability=='AVAILABLE' and receipt.payload_json is not None:
        if receipt.provider=='public_cross_market' and receipt.kind in {'CROSS_OI','CROSS_FUNDING'}:
            from .public_cross_market import normalize_public_cross_oi, normalize_public_cross_funding
            payload=json.loads(receipt.payload_json)
            normalizer=normalize_public_cross_oi if receipt.kind=='CROSS_OI' else normalize_public_cross_funding
            return normalizer(payload,symbol=receipt.symbol,fetched_at=receipt.fetched_at)
        if receipt.provider=='coinalyze' and receipt.kind=='CROSS_OI':
            from quant_phase2.adapters.aggregator import normalize_coinalyze_cross_oi
            return normalize_coinalyze_cross_oi(receipt)
        if receipt.provider=='coinalyze' and receipt.kind=='CROSS_FUNDING':
            from quant_phase2.adapters.aggregator import normalize_coinalyze_funding
            return normalize_coinalyze_funding(receipt)
    retired=receipt.kind in {'EVENT_COVERAGE','EXCHANGE_EVENT_COVERAGE','MACRO_COVERAGE'}
    return MarketObservation(symbol=receipt.symbol,kind=receipt.kind,provider=receipt.provider,source_ref=receipt.endpoint,
        source_group=receipt.provider+':'+receipt.kind,source_digest=receipt.digest,fetched_at=receipt.fetched_at,
        availability=receipt.availability,coverage=receipt.coverage,quality='PARTIAL' if receipt.availability=='AVAILABLE' else 'UNKNOWN',
        freshness='UNKNOWN',reason='LEGACY_EVENT_SOURCE_RETIRED' if retired else receipt.reason or 'NORMALIZATION_AND_COVERAGE_REQUIRED')
