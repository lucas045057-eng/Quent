"""Bitget UTA v3 public derivative adapter.

This adapter deliberately does not fall back to Classic v2. The v2 historical
funding endpoint is documented in the audit only and is not consumed here.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from dataclasses import replace
from typing import Any, Mapping

from ..contracts import ContractType, DataStatus, FundingObservation, InstrumentMetadata, OIObservation
from ..funding import normalize_funding_to_8h
from ..normalization import normalize_open_interest
from ..symbols import SymbolRegistry
from .base import AdapterSchemaError, PublicHTTPAdapter, decimal, success_payload, timestamp_ms


class BitgetClockSkewError(AdapterSchemaError):
    """The source cannot be admitted without a future clock or excessive wait."""


class BitgetUTAAdapter(PublicHTTPAdapter):
    INSTRUMENTS = "/api/v3/market/instruments"
    TICKERS = "/api/v3/market/tickers"
    CURRENT_FUNDING = "/api/v3/market/current-fund-rate"
    OPEN_INTEREST = "/api/v3/market/open-interest"
    RATE_LIMIT_PER_SECOND = 20.0
    JITTER_SECONDS = 0.02
    MAX_CLOCK_QUARANTINE_SECONDS = 1.0

    def __init__(self, *, session: Any = None, registry: SymbolRegistry | None = None, base_url: str = "https://api.bitget.com", oi_unit_contract=None) -> None:
        super().__init__(base_url, session=session, rate_limit_per_second=self.RATE_LIMIT_PER_SECOND, jitter_seconds=self.JITTER_SECONDS)
        self.registry = registry
        self.oi_unit_contract = oi_unit_contract

    @staticmethod
    def parse_instruments(payload: Mapping[str, Any], fetched_at: datetime) -> list[InstrumentMetadata]:
        data = success_payload(payload)
        if not isinstance(data, list):
            raise AdapterSchemaError("Bitget instruments data must be a list")
        result: list[InstrumentMetadata] = []
        for item in data:
            if not isinstance(item, Mapping):
                raise AdapterSchemaError("Bitget instrument item must be an object")
            symbol = str(item["symbol"])
            if item.get("fundInterval") not in (None, ""):
                # Bitget's live UTA instruments response exposes fundInterval
                # as an integer number of hours.
                interval_seconds = int(item["fundInterval"]) * 3600
            else:
                funding_interval = item.get("fundingInterval") or item.get("fundingIntervalMinutes")
                interval_seconds = BitgetUTAAdapter._interval_seconds(funding_interval)
            result.append(
                InstrumentMetadata(
                    exchange="bitget", exchange_symbol=symbol,
                    canonical_symbol=None, base_asset=str(item.get("baseCoin", "")) or None,
                    quote_asset=str(item.get("quoteCoin", "")) or None,
                    settle_asset=str(item.get("settleCoin", "")) or None,
                    contract_type=ContractType.PERPETUAL,
                    margin_asset=str(item.get("marginCoin", "")) or None,
                    contract_multiplier=decimal(item["contractSize"], "contractSize") if item.get("contractSize") is not None else None,
                    contract_size=decimal(item["sizeMultiplier"], "sizeMultiplier") if item.get("sizeMultiplier") is not None else (
                        decimal(item["quantityMultiplier"], "quantityMultiplier") if item.get("quantityMultiplier") is not None else None
                    ),
                    funding_interval_seconds=interval_seconds,
                    mark_price=None, source_endpoint=BitgetUTAAdapter.INSTRUMENTS,
                    fetched_at=fetched_at, exchange_timestamp=None, raw_payload=dict(item),
                )
            )
        return result

    def _canonical(self, symbol: str) -> str | None:
        return self.registry.canonical_for("bitget", symbol) if self.registry else None

    @staticmethod
    def _interval_seconds(value: Any) -> int | None:
        if value in (None, ""):
            return None
        text = str(value).strip().upper()
        if text.endswith("H"):
            return int(text[:-1]) * 3600
        if text.endswith("M"):
            return int(text[:-1]) * 60
        # The live UTA current-fund-rate response returns "8" for an
        # eight-hour funding interval. It is not a duration in seconds.
        return int(value) * 3600

    def parse_current_funding(self, payload: Mapping[str, Any], fetched_at: datetime, *, symbol: str) -> FundingObservation:
        data = success_payload(payload)
        if isinstance(data, list):
            if len(data) != 1 or not isinstance(data[0], Mapping):
                raise AdapterSchemaError("Bitget current funding data must contain one object")
            item = data[0]
        elif isinstance(data, Mapping):
            item = data
        else:
            raise AdapterSchemaError("Bitget current funding data must be an object or one-item list")
        if item.get("symbol") not in (None, symbol):
            raise AdapterSchemaError("Bitget current funding symbol mismatch")
        rate = decimal(item["fundingRate"], "fundingRate")
        interval = self._interval_seconds(item.get("fundingRateInterval"))
        normalized = normalize_funding_to_8h(rate, interval)
        next_time = timestamp_ms(item["nextUpdate"], "nextUpdate") if item.get("nextUpdate") not in (None, "") else None
        return FundingObservation(
            symbol=symbol, canonical_symbol=self._canonical(symbol), exchange="bitget",
            contract_type=ContractType.PERPETUAL, funding_rate=rate,
            funding_interval_seconds=interval, normalized_8h_rate=normalized.value,
            predicted_funding_rate=None, realized_funding_rate=None, next_funding_time=next_time,
            # The endpoint exposes the next scheduled update, not the event
            # timestamp of the current observation. Keep that value only in
            # next_funding_time; do not mislabel it as exchange_timestamp.
            exchange_timestamp=None, fetched_at=fetched_at, processed_at=fetched_at,
            status=DataStatus.AVAILABLE, source_endpoint=self.CURRENT_FUNDING,
            raw_payload=dict(item),
        )

    def parse_tickers(self, payload: Mapping[str, Any], fetched_at: datetime) -> tuple[list[OIObservation], list[FundingObservation]]:
        data = success_payload(payload)
        if not isinstance(data, list):
            raise AdapterSchemaError("Bitget tickers data must be a list")
        oi_rows: list[OIObservation] = []
        funding_rows: list[FundingObservation] = []
        for item in data:
            if not isinstance(item, Mapping):
                raise AdapterSchemaError("Bitget ticker item must be an object")
            symbol = str(item["symbol"])
            exchange_ts = timestamp_ms(item["ts"], "Bitget ticker ts") if item.get("ts") is not None else None
            raw_oi = decimal(item["openInterest"], "openInterest") if item.get("openInterest") not in (None, "") else None
            # The UTA ticker page exposes openInterest but does not define its
            # unit. Preserve the raw value, but fail closed for normalization.
            raw_unit = str(item.get("openInterestUnit") or "UNCONFIRMED")
            normalized = normalize_open_interest(
                raw_value=raw_oi, raw_unit=raw_unit,
                mark_price=decimal(item["markPrice"], "markPrice") if item.get("markPrice") not in (None, "") else None,
            )
            oi_rows.append(
                OIObservation(
                    symbol=symbol, canonical_symbol=self._canonical(symbol), exchange="bitget",
                    contract_type=ContractType.PERPETUAL, margin_asset=None, settle_asset="USDT",
                    raw_open_interest=raw_oi, raw_unit=raw_unit,
                    open_interest_base=normalized.open_interest_base,
                    open_interest_quote=normalized.open_interest_quote,
                    open_interest_usd=normalized.open_interest_usd,
                    mark_price=decimal(item["markPrice"], "markPrice") if item.get("markPrice") not in (None, "") else None,
                    normalization_method=normalized.method,
                    exchange_timestamp=exchange_ts, fetched_at=fetched_at, processed_at=fetched_at,
                    status=DataStatus.AVAILABLE if raw_oi is not None else DataStatus.NOT_AVAILABLE,
                    source_endpoint=BitgetUTAAdapter.TICKERS, raw_payload=dict(item),
                )
            )
            rate = decimal(item["fundingRate"], "fundingRate") if item.get("fundingRate") not in (None, "") else None
            interval = int(item["fundingInterval"]) * 60 if item.get("fundingInterval") is not None else None
            normalized_funding = normalize_funding_to_8h(rate, interval)
            next_time = timestamp_ms(item["nextFundingTime"], "nextFundingTime") if item.get("nextFundingTime") not in (None, "") else None
            funding_rows.append(
                FundingObservation(
                    symbol=symbol, canonical_symbol=self._canonical(symbol), exchange="bitget",
                    contract_type=ContractType.PERPETUAL, funding_rate=rate,
                    funding_interval_seconds=interval, normalized_8h_rate=normalized_funding.value,
                    predicted_funding_rate=None, realized_funding_rate=None, next_funding_time=next_time,
                    exchange_timestamp=exchange_ts, fetched_at=fetched_at, processed_at=fetched_at,
                    status=DataStatus.AVAILABLE if rate is not None else DataStatus.NOT_AVAILABLE,
                    source_endpoint=BitgetUTAAdapter.TICKERS, raw_payload=dict(item),
                )
            )
        return oi_rows, funding_rows

    async def fetch_instruments(self, fetched_at: datetime) -> list[InstrumentMetadata]:
        payload = await self.get_json(self.INSTRUMENTS, {"category": "USDT-FUTURES"})
        return self.parse_instruments(payload, fetched_at)

    async def fetch_tickers(self, fetched_at: datetime, symbol: str | None = None) -> tuple[list[OIObservation], list[FundingObservation]]:
        params = {"category": "USDT-FUTURES"}
        if symbol:
            params["symbol"] = symbol
        payload = await self.get_json(self.TICKERS, params)
        # The caller's clock is request start, not actual payload receipt.
        return self.parse_tickers(payload, datetime.now(timezone.utc))

    async def fetch_tickers_batch(self, symbols):
        payload = await self.get_json(self.TICKERS, {'category':'USDT-FUTURES'})
        received = datetime.now(timezone.utc)
        data = success_payload(payload)
        if not isinstance(data, list):
            raise AdapterSchemaError('Bitget tickers data must be a list')
        grouped = {}
        for item in data:
            if isinstance(item, Mapping):
                grouped.setdefault(item.get('symbol'), []).append(item)
        oi, funding, errors = [], [], []
        for symbol in symbols:
            selected = grouped.get(symbol, [])
            if len(selected) != 1:
                errors.append(f'bitget:{symbol}:tickers:SCOPE_MISSING_OR_DUPLICATE')
                continue
            try:
                rows, rates = self.parse_tickers({**payload, 'data':selected}, received)
                oi.extend(rows);funding.extend(rates)
            except (AdapterSchemaError, KeyError, TypeError, ValueError) as exc:
                errors.append(f'bitget:{symbol}:tickers:{type(exc).__name__}')
        return oi, funding, errors

    def parse_open_interest(self, payload: Mapping[str, Any], fetched_at: datetime, *, symbol: str,
                            admitted_at: datetime | None = None) -> OIObservation:
        """Use the dedicated UTA source clock and preserve its undeclared unit.

        The catalog, ticker mark price and Classic API cannot attest the unit of
        this field. A raw observation is retained even when normalization fails.
        """
        data = success_payload(payload)
        if not isinstance(data, Mapping) or not isinstance(data.get('list'), list):
            raise AdapterSchemaError('Bitget open interest data must contain list')
        items = data['list']
        if len(items) != 1 or not isinstance(items[0], Mapping) or items[0].get('symbol') != symbol:
            raise AdapterSchemaError('Bitget open interest symbol scope mismatch')
        item = items[0]
        observed = timestamp_ms(data['ts'], 'Bitget open interest ts')
        processed_at = admitted_at if admitted_at is not None else fetched_at
        if (processed_at < fetched_at or observed > processed_at
            or (observed-fetched_at).total_seconds() > self.MAX_CLOCK_QUARANTINE_SECONDS
            or (processed_at-fetched_at).total_seconds() > self.MAX_CLOCK_QUARANTINE_SECONDS + .1):
            raise BitgetClockSkewError('Bitget open interest source clock is in the future')
        raw = decimal(item['openInterest'], 'openInterest')
        if not raw.is_finite() or raw < 0:
            raise AdapterSchemaError('Bitget open interest must be finite and nonnegative')
        unit = str(item.get('openInterestUnit') or 'UNCONFIRMED')
        contract=self.oi_unit_contract
        proof=None
        if contract is not None and symbol in contract.symbols:
            if unit not in ('UNCONFIRMED','USDT','QUOTE_NOTIONAL'):
                raise AdapterSchemaError('Bitget OI source unit contradicts configured contract')
            unit='QUOTE_NOTIONAL'
            proof={'contract':contract.model_dump(mode='json'),'digest':contract.digest}
        normalized = normalize_open_interest(raw_value=raw, raw_unit=unit, mark_price=None)
        return OIObservation(symbol=symbol, canonical_symbol=self._canonical(symbol), exchange='bitget',
            contract_type=ContractType.PERPETUAL, margin_asset=None, settle_asset='USDT',
            raw_open_interest=raw, raw_unit=unit, open_interest_base=normalized.open_interest_base,
            open_interest_quote=normalized.open_interest_quote, open_interest_usd=normalized.open_interest_usd,
            mark_price=None, normalization_method=normalized.method, exchange_timestamp=observed,
            fetched_at=fetched_at, processed_at=processed_at, status=DataStatus.AVAILABLE,
            source_endpoint=self.OPEN_INTEREST,
            raw_payload={'category':'USDT-FUTURES','data':dict(data),
                **({'unit_contract_proof':proof} if proof else {}),
                **({'source_clock_admission':{
                    'rule':'WAIT_UNTIL_SOURCE_CLOCK_NOT_FUTURE_MAX_1_SECOND',
                    'received_at':fetched_at.isoformat(), 'admitted_at':processed_at.isoformat(),
                    'source_event_at':observed.isoformat()}} if observed > fetched_at else {}),
                'unit_contract':'USER_CONFIRMED' if proof else 'SOURCE_DECLARED' if unit!='UNCONFIRMED' else 'NOT_PROVIDED_BY_SOURCE'})

    async def _oi_receipt_clocks(self, payload):
        """Quarantine small clock skew; preserve receipt and source clocks verbatim.

        Nothing is usable until the actual local clock catches up. Large skew
        fails closed; this is neither a rewritten receipt nor clock tolerance
        in the strategy's as-of predicate.
        """
        received = datetime.now(timezone.utc)
        data = success_payload(payload)
        if not isinstance(data, Mapping):
            raise AdapterSchemaError('Bitget open interest data must be an object')
        event = timestamp_ms(data.get('ts'), 'Bitget open interest ts')
        ahead = (event-received).total_seconds()
        if ahead > self.MAX_CLOCK_QUARANTINE_SECONDS:
            raise BitgetClockSkewError('Bitget open interest clock skew exceeds quarantine bound')
        if ahead > 0:
            await asyncio.sleep(ahead + .001)
        return received, datetime.now(timezone.utc)

    async def fetch_open_interest(self, symbol: str) -> OIObservation:
        payload = await self.get_json(self.OPEN_INTEREST, {'category':'USDT-FUTURES','symbol':symbol})
        received, admitted = await self._oi_receipt_clocks(payload)
        return self.parse_open_interest(payload, received, symbol=symbol, admitted_at=admitted)

    async def fetch_open_interest_batch(self, symbols) -> tuple[list[OIObservation], list[str]]:
        """One documented category request; isolate invalid/missing symbols."""
        payload = await self.get_json(self.OPEN_INTEREST, {'category':'USDT-FUTURES'})
        received, admitted = await self._oi_receipt_clocks(payload)
        data = success_payload(payload)
        items = data.get('list')
        if not isinstance(items, list):
            raise AdapterSchemaError('Bitget open interest data must contain list')
        grouped = {}
        for item in items:
            if isinstance(item, Mapping):
                grouped.setdefault(item.get('symbol'), []).append(item)
        rows, errors = [], []
        for symbol in symbols:
            selected = grouped.get(symbol, [])
            if len(selected) != 1:
                errors.append(f'bitget:{symbol}:open_interest:SCOPE_MISSING_OR_DUPLICATE')
                continue
            scoped = {**payload, 'data':{**data, 'list':selected}}
            try:
                rows.append(self.parse_open_interest(scoped, received, symbol=symbol, admitted_at=admitted))
            except (AdapterSchemaError, KeyError, TypeError, ValueError) as exc:
                errors.append(f'bitget:{symbol}:open_interest:{type(exc).__name__}')
        return rows, errors

    async def fetch_current_funding_batch(self, symbols) -> tuple[list[FundingObservation], list[str]]:
        payload = await self.get_json(self.CURRENT_FUNDING, {'category':'USDT-FUTURES'})
        received = datetime.now(timezone.utc)
        data = success_payload(payload)
        if not isinstance(data, list):
            raise AdapterSchemaError('Bitget current funding batch must be a list')
        grouped = {}
        for item in data:
            if isinstance(item, Mapping):
                grouped.setdefault(item.get('symbol'), []).append(item)
        rows, errors = [], []
        for symbol in symbols:
            selected = grouped.get(symbol, [])
            if len(selected) != 1:
                errors.append(f'bitget:{symbol}:funding:SCOPE_MISSING_OR_DUPLICATE')
                continue
            try:
                rows.append(self.parse_current_funding({**payload,'data':selected}, received, symbol=symbol))
            except (AdapterSchemaError, KeyError, TypeError, ValueError) as exc:
                errors.append(f'bitget:{symbol}:funding:{type(exc).__name__}')
        return rows, errors

    @staticmethod
    def bind_oi_mark_price(current: OIObservation, ticker: OIObservation) -> OIObservation:
        """A separately timestamped price can convert a source-declared unit only."""
        if (current.exchange!='bitget' or ticker.exchange!='bitget' or current.symbol!=ticker.symbol
            or current.canonical_symbol!=ticker.canonical_symbol or current.source_endpoint!=BitgetUTAAdapter.OPEN_INTEREST
            or ticker.source_endpoint!=BitgetUTAAdapter.TICKERS or current.raw_unit in ('UNCONFIRMED','UNKNOWN','')
            or current.status is not DataStatus.AVAILABLE or ticker.status is not DataStatus.AVAILABLE
            or current.exchange_timestamp is None or ticker.exchange_timestamp is None
            or ticker.mark_price is None or not ticker.mark_price.is_finite() or ticker.mark_price<=0
            or not 0<=(current.fetched_at-ticker.fetched_at).total_seconds()<=30
            or not 0<=(current.processed_at-ticker.exchange_timestamp).total_seconds()<=30
            or not 0<=(current.processed_at-current.exchange_timestamp).total_seconds()<=30
            or abs((current.exchange_timestamp-ticker.exchange_timestamp).total_seconds())>30):
            return current
        normalized=normalize_open_interest(raw_value=current.raw_open_interest,raw_unit=current.raw_unit,mark_price=ticker.mark_price)
        return replace(current,mark_price=ticker.mark_price,open_interest_base=normalized.open_interest_base,
            open_interest_quote=normalized.open_interest_quote,open_interest_usd=normalized.open_interest_usd,
            normalization_method='user_confirmed_quote_notional' if current.raw_payload.get('unit_contract_proof') else normalized.method,raw_payload={**current.raw_payload,
                'mark_price_binding':{'endpoint':ticker.source_endpoint,'exchange_timestamp':ticker.exchange_timestamp.isoformat(),
                    'fetched_at':ticker.fetched_at.isoformat(),'mark_price':str(ticker.mark_price),
                    'rule':'SAME_SYMBOL_AND_CLOCKS_WITHIN_30_SECONDS'}})

    async def fetch_current_funding(self, symbol: str, fetched_at: datetime) -> FundingObservation:
        payload = await self.get_json(self.CURRENT_FUNDING, {"category": "USDT-FUTURES", "symbol": symbol})
        return self.parse_current_funding(payload, datetime.now(timezone.utc), symbol=symbol)

    @staticmethod
    def bind_funding_clock(ticker: FundingObservation, current: FundingObservation) -> FundingObservation:
        """Use a timestamped rate only when the separately fetched native rate agrees.

        The current-fund-rate endpoint supplies the documented hours per period;
        its nextUpdate is never an event clock. A changed rate, mixed symbol or
        delayed pair retains the original unclocked observation and fails closed.
        """
        clock=ticker.exchange_timestamp
        if (ticker.exchange != 'bitget' or current.exchange != 'bitget'
            or ticker.symbol != current.symbol or ticker.canonical_symbol != current.canonical_symbol
            or ticker.source_endpoint != BitgetUTAAdapter.TICKERS or current.source_endpoint != BitgetUTAAdapter.CURRENT_FUNDING
            or ticker.status is not DataStatus.AVAILABLE or current.status is not DataStatus.AVAILABLE
            or clock is None or ticker.funding_rate is None or ticker.funding_rate != current.funding_rate
            or not current.funding_interval_seconds or current.normalized_8h_rate is None
            or not 0 <= (current.fetched_at-clock).total_seconds() <= 30
            or not 0 <= (current.fetched_at-ticker.fetched_at).total_seconds() <= 30):
            return current
        return replace(current,exchange_timestamp=clock,source_endpoint=BitgetUTAAdapter.TICKERS,
            raw_payload={'ticker':dict(ticker.raw_payload),'period':dict(current.raw_payload),
                'period_endpoint':BitgetUTAAdapter.CURRENT_FUNDING,'period_fetched_at':current.fetched_at.isoformat(),
                'clock_binding':'EXACT_RATE_AND_SYMBOL_MATCH_WITHIN_30_SECONDS'})
