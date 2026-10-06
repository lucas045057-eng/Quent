"""Bounded Phase 2 public-data polling runtime.

The runtime is intentionally embedded in the existing engine process. It does
not add Kafka, Redis, another PostgreSQL instance, or any private API path.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

import psycopg

from quant_phase1.db import assert_schema_ready
from quant_phase1.time import utc_now

from .adapters.bitget import BitgetUTAAdapter
from .adapters.bybit import BybitV5Adapter
from .adapters.hyperliquid import HyperliquidAdapter
from .contracts import CanonicalSymbol, DataStatus, FundingObservation, InstrumentMetadata, OIObservation
from .cross_exchange import build_cross_exchange_snapshot
from .normalization import compute_oi_change
from .normalization import apply_derivative_freshness, apply_funding_freshness
from quant_data_layer.freshness import FRESHNESS_POLICY
from .persistence import Phase2Repository
from .symbols import SymbolRegistry, registry_from_phase1_symbols
from quant_phase1.repositories import Phase1Repository


async def bounded_symbol_map(symbols, operation, *, concurrency, timeout_seconds=30):
    semaphore=asyncio.Semaphore(concurrency)
    async def call(symbol):
        async with semaphore:
            try:return await asyncio.wait_for(operation(symbol),timeout_seconds)
            except Exception as exc:return exc
    return await asyncio.gather(*(call(symbol) for symbol in symbols))


OI_CHANGE_WINDOWS = {"5m": 300, "15m": 900, "1H": 3600, "4H": 14400, "24H": 86400}


def default_registry() -> SymbolRegistry:
    return SymbolRegistry([
        CanonicalSymbol("BTC-USDT-PERP", "BTC", "USDT", "USDT", "PERPETUAL", {"bitget": "BTCUSDT", "bybit": "BTCUSDT", "hyperliquid": "BTC"}),
        CanonicalSymbol("ETH-USDT-PERP", "ETH", "USDT", "USDT", "PERPETUAL", {"bitget": "ETHUSDT", "bybit": "ETHUSDT", "hyperliquid": "ETH"}),
    ])


@dataclass(frozen=True, slots=True)
class Phase2CycleResult:
    started_at: datetime
    completed_at: datetime
    symbols: int
    oi_observations: int
    funding_observations: int
    snapshots: int
    errors: tuple[str, ...]


class Phase2DerivativeRuntime:
    def __init__(self, settings: Any, *, registry: SymbolRegistry | None = None) -> None:
        self.settings = settings
        self.registry = registry or default_registry()
        self.history: dict[tuple[str, str], deque[OIObservation]] = defaultdict(lambda: deque(maxlen=400))
        self._universe_error: str | None = None
        self._history_hydrated = False

    def _canonical_for_configured(self, symbol: str) -> str | None:
        for exchange in ("bitget", "bybit"):
            canonical = self.registry.canonical_for(exchange, symbol)
            if canonical:
                return canonical
        return None

    def _hyperliquid_symbols(self, configured: tuple[str, ...]) -> set[str]:
        return {
            self.registry.resolve("bitget", symbol).exchange_symbols["hyperliquid"]
            for symbol in configured
            if self.registry.canonical_for("bitget", symbol)
        }

    def _resolve_universe(self) -> tuple[str, ...]:
        """Resolve symbols from Phase 1 persistence unless explicitly overridden."""
        configured = tuple(self.settings.phase2_symbols)
        if configured:
            self._universe_error = None
            return configured
        try:
            with psycopg.connect(self.settings.postgres_dsn) as connection:
                rows = Phase1Repository(connection).load_phase2_universe(limit=self.settings.universe_limit)
            registry = registry_from_phase1_symbols(rows)
            self.registry = registry
            configured = tuple(str(row["symbol"]).upper() for row in rows)
            self._universe_error = None if configured else "phase2:universe:NOT_AVAILABLE"
            return configured
        except Exception as exc:
            # A missing DB must fail closed rather than silently fall back to a
            # partial hard-coded market. The caller records the error.
            self._universe_error = f"phase2:universe:{type(exc).__name__}"
            return ()

    async def _fetch(self, now: datetime, configured: tuple[str, ...]) -> tuple[list[InstrumentMetadata], list[OIObservation], list[FundingObservation], list[str]]:
        instruments: list[InstrumentMetadata] = []
        oi: list[OIObservation] = []
        funding: list[FundingObservation] = []
        errors: list[str] = []
        if not configured:
            return instruments, oi, funding, [self._universe_error or "phase2:universe:NOT_AVAILABLE"]
        from .unit_contracts import load_bitget_oi_contract
        contract=load_bitget_oi_contract(getattr(self.settings,'bitget_oi_unit_contract_path',None))
        configured_set = set(configured)
        async with BitgetUTAAdapter(registry=self.registry,oi_unit_contract=contract) as bitget:
            try:
                bitget_instruments = await bitget.fetch_instruments(now)
                instruments.extend(
                    replace(item, canonical_symbol=self.registry.canonical_for("bitget", item.exchange_symbol))
                    for item in bitget_instruments
                    if item.exchange_symbol in configured_set
                )
            except Exception as exc:
                errors.append(f"bitget:instruments:{type(exc).__name__}")
            # Category batch requests avoid 478*3 individual requests and keep
            # the dedicated OI and ticker price clocks close. Each operation
            # is bounded and independently fails closed; never retry via a
            # different, unconfirmed unit endpoint.
            try:
                ticker_rows,ticker_rates,issues=await asyncio.wait_for(bitget.fetch_tickers_batch(configured),20)
                errors.extend(issues)
            except Exception as exc:
                ticker_rows,ticker_rates=[],[]
                errors.append(f'bitget:tickers:{type(exc).__name__}')
            ticker_by_symbol={}
            for row in ticker_rows:
                if row.symbol in configured_set:ticker_by_symbol.setdefault(row.symbol,[]).append(row)
            try:
                dedicated,issues=await asyncio.wait_for(bitget.fetch_open_interest_batch(configured),20)
                errors.extend(issues)
                for row in dedicated:
                    matching=ticker_by_symbol.get(row.symbol,[])
                    if len(matching)==1:row=bitget.bind_oi_mark_price(row,matching[0])
                    if row.open_interest_base is None or row.mark_price is None:
                        errors.append(f'bitget:{row.symbol}:open_interest:MARK_PRICE_BINDING_UNAVAILABLE')
                    oi.append(row)
            except Exception as exc:
                errors.append(f'bitget:open_interest:{type(exc).__name__}')
            # Keep raw unconfirmed ticker evidence, but never let it stand in
            # for a missing dedicated endpoint during strategy source reads.
            dedicated_symbols={item.symbol for item in oi if item.exchange=='bitget'}
            oi.extend(row for row in ticker_rows if row.symbol in configured_set and row.symbol not in dedicated_symbols)
            try:
                current_rates,issues=await asyncio.wait_for(bitget.fetch_current_funding_batch(configured),20)
                errors.extend(issues)
                for rate in current_rates:
                    matching=[row for row in ticker_rates if row.symbol==rate.symbol]
                    if len(matching)==1:rate=bitget.bind_funding_clock(matching[0],rate)
                    funding.append(rate)
            except Exception as exc:
                errors.append(f'bitget:funding:{type(exc).__name__}')
        async with BybitV5Adapter(registry=self.registry) as bybit:
            try:
                bybit_instruments = await bybit.fetch_instruments(now)
                instruments_by_symbol = {item.exchange_symbol: item for item in bybit_instruments}
                instruments.extend(item for item in bybit_instruments if item.exchange_symbol in set(configured))
                async def fetch_bybit(symbol):
                    metadata=instruments_by_symbol.get(symbol)
                    if metadata is None:return None
                    return await bybit.fetch_ticker(symbol,utc_now(),metadata.funding_interval_seconds)
                responses=await bounded_symbol_map(configured,fetch_bybit,
                    concurrency=getattr(self.settings,'phase2_symbol_concurrency',1))
                for symbol,response in zip(configured,responses,strict=True):
                    if response is None:errors.append(f'bybit:{symbol}:instrument_missing')
                    elif isinstance(response,Exception):errors.append(f'bybit:{symbol}:{type(response).__name__}')
                    else:
                        row,rate=response;oi.append(row);funding.append(rate)
            except Exception as exc:
                errors.append(f"bybit:instruments:{type(exc).__name__}")
        async with HyperliquidAdapter(registry=self.registry) as hyperliquid:
            try:
                hyper_instruments, hyper_oi, hyper_funding = await hyperliquid.fetch_meta_and_contexts(now)
                hyper_symbols = self._hyperliquid_symbols(configured)
                instruments.extend(item for item in hyper_instruments if item.exchange_symbol in hyper_symbols)
                oi.extend(row for row in hyper_oi if row.symbol in hyper_symbols)
                funding.extend(row for row in hyper_funding if row.symbol in hyper_symbols)
            except Exception as exc:
                errors.append(f"hyperliquid:metaAndAssetCtxs:{type(exc).__name__}")
        # Source responses may arrive after cycle start. Compare their clocks
        # against completion time, without relaxing the existing freshness cap.
        freshness_at = utc_now()
        oi = apply_derivative_freshness(oi, freshness_at, max_age_seconds=min(
            self.settings.phase2_oi_freshness_seconds,
            int(FRESHNESS_POLICY["OPEN_INTEREST"].hard_seconds),
        ))
        funding = apply_funding_freshness(funding, freshness_at, max_age_seconds=min(
            self.settings.phase2_funding_freshness_seconds,
            int(FRESHNESS_POLICY["FUNDING"].hard_seconds),
        ))
        return instruments, oi, funding, errors

    def _changes(self, rows: list[OIObservation], canonical_symbol: str) -> tuple:
        changes = []
        for exchange in {row.exchange for row in rows if row.canonical_symbol == canonical_symbol}:
            history = list(self.history[(canonical_symbol, exchange)])
            for seconds in OI_CHANGE_WINDOWS.values():
                changes.append(compute_oi_change(history, requested_window_seconds=seconds, canonical_symbol=canonical_symbol))
        return tuple(changes)

    def _hydrate_history(self) -> None:
        if self._history_hydrated:
            return
        try:
            with psycopg.connect(self.settings.postgres_dsn) as connection:
                assert_schema_ready(connection, required_version="006_phase2_stage1_enrichment.sql")
                rows = Phase2Repository(connection).load_recent_oi_history()
            for row in rows:
                if row.canonical_symbol:
                    self.history[(row.canonical_symbol, row.exchange)].appendleft(row)
            for key in list(self.history):
                self.history[key] = deque(
                    sorted(self.history[key], key=lambda item: item.fetched_at), maxlen=400
                )
            self._history_hydrated = True
        except Exception:
            # The first cycle remains able to collect fresh data; no fabricated
            # historical values are substituted when the DB is unavailable.
            self._history_hydrated = True

    async def run_cycle(self) -> Phase2CycleResult:
        started = utc_now()
        self._hydrate_history()
        configured = self._resolve_universe()
        instruments, oi_rows, funding_rows, errors = await self._fetch(started, configured)
        for row in oi_rows:
            if row.canonical_symbol:
                self.history[(row.canonical_symbol, row.exchange)].append(row)
        snapshots = 0
        by_symbol_oi: dict[str, list[OIObservation]] = defaultdict(list)
        by_symbol_funding: dict[str, list[FundingObservation]] = defaultdict(list)
        for row in oi_rows:
            if row.canonical_symbol:
                by_symbol_oi[row.canonical_symbol].append(row)
        for row in funding_rows:
            if row.canonical_symbol:
                by_symbol_funding[row.canonical_symbol].append(row)
        snapshot_rows = []
        snapshot_at = utc_now()
        for canonical in sorted(set(by_symbol_oi) | set(by_symbol_funding)):
            snapshot = build_cross_exchange_snapshot(
                canonical, by_symbol_oi[canonical], by_symbol_funding[canonical], snapshot_at,
                min_oi_sources=self.settings.phase2_min_oi_sources,
                min_funding_sources=self.settings.phase2_min_funding_sources,
            )
            snapshot_rows.append(replace(snapshot, oi_changes=self._changes(oi_rows, canonical)))
        try:
            with psycopg.connect(self.settings.postgres_dsn) as connection:
                assert_schema_ready(connection, required_version="006_phase2_stage1_enrichment.sql")
                repository = Phase2Repository(connection)
                repository.upsert_instruments(instruments)
                repository.insert_open_interest(oi_rows)
                repository.insert_funding(funding_rows)
                for snapshot in snapshot_rows:
                    repository.insert_cross_exchange_snapshot(snapshot)
                    snapshots += 1
                repository.cleanup_derivatives(
                    oi_retention_days=self.settings.phase2_oi_retention_days,
                    funding_retention_days=self.settings.phase2_funding_retention_days,
                    snapshot_retention_days=self.settings.phase2_snapshot_retention_days,
                )
        except Exception as exc:
            errors.append(f"postgres:{type(exc).__name__}")
        return Phase2CycleResult(started, utc_now(), len(snapshot_rows), len(oi_rows), len(funding_rows), snapshots, tuple(errors))
