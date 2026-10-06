"""Small PostgreSQL repository for Phase 1 persistence."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence
from uuid import uuid4

from psycopg.types.json import Jsonb

from .contracts import Candle, DataStatus, Instrument, Observation, Ticker
from .pipeline import MarketDataBatch
from .persistence import retention_delete_statements, stage1_result_json
from .stage1 import Stage1Result


class Phase1Repository:
    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.last_screening_run_id: int | None = None

    def upsert_instrument(self, instrument: Instrument) -> None:
        self.upsert_instruments([instrument])

    def upsert_instruments(self, instruments: Sequence[Instrument]) -> None:
        rows = [
            (
                instrument.symbol, instrument.category, instrument.base_coin, instrument.quote_coin,
                instrument.symbol_type, instrument.contract_type, instrument.status,
                instrument.price_precision, instrument.quantity_precision, instrument.min_order_qty,
                instrument.max_order_qty, instrument.source, instrument.exchange,
                instrument.exchange_timestamp, instrument.fetched_at, Jsonb(dict(instrument.raw_payload)),
            )
            for instrument in instruments
        ]
        if not rows:
            return
        with self.connection.cursor() as cursor:
            cursor.executemany(
                """
            INSERT INTO symbols (
                symbol, category, base_coin, quote_coin, symbol_type, contract_type, status,
                price_precision, quantity_precision, min_order_qty, max_order_qty,
                source, exchange, exchange_timestamp, fetched_at, raw_payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol) DO UPDATE SET
                category = EXCLUDED.category,
                base_coin = EXCLUDED.base_coin,
                quote_coin = EXCLUDED.quote_coin,
                symbol_type = EXCLUDED.symbol_type,
                contract_type = EXCLUDED.contract_type,
                status = EXCLUDED.status,
                price_precision = EXCLUDED.price_precision,
                quantity_precision = EXCLUDED.quantity_precision,
                min_order_qty = EXCLUDED.min_order_qty,
                max_order_qty = EXCLUDED.max_order_qty,
                fetched_at = EXCLUDED.fetched_at,
                raw_payload = EXCLUDED.raw_payload
                """,
                rows,
            )

    def upsert_candle(self, candle: Candle) -> None:
        self.upsert_candles([candle])

    def upsert_candles(self, candles: Sequence[Candle]) -> None:
        rows = [
            (
                candle.symbol, candle.interval, candle.bar_open_timestamp, candle.open, candle.high,
                candle.low, candle.close, candle.volume, candle.turnover, candle.exchange_timestamp,
                candle.fetched_at, candle.processed_at, candle.status.value, Jsonb(candle.raw_payload),
                candle.source, candle.exchange,
            )
            for candle in candles
        ]
        if not rows:
            return
        with self.connection.cursor() as cursor:
            cursor.executemany(
                """
            INSERT INTO klines (
                symbol, interval, bar_open_timestamp, open, high, low, close, volume, turnover,
                exchange_timestamp, fetched_at, processed_at, status, raw_payload, source, exchange
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol, interval, bar_open_timestamp) DO UPDATE SET
                open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                close = EXCLUDED.close, volume = EXCLUDED.volume, turnover = EXCLUDED.turnover,
                exchange_timestamp = EXCLUDED.exchange_timestamp, fetched_at = EXCLUDED.fetched_at,
                processed_at = EXCLUDED.processed_at, status = EXCLUDED.status,
                raw_payload = EXCLUDED.raw_payload, source = EXCLUDED.source, exchange = EXCLUDED.exchange
                """,
                rows,
            )

    def insert_market_snapshot(self, ticker: Any, snapshot_timestamp: datetime) -> None:
        self.insert_market_snapshots([ticker], snapshot_timestamp)

    def insert_market_snapshots(self, tickers: Sequence[Any], snapshot_timestamp: datetime) -> None:
        rows = [
            (
                ticker.symbol, snapshot_timestamp, ticker.source, ticker.exchange,
                ticker.exchange_timestamp, ticker.fetched_at, ticker.processed_at,
                ticker.status.value, Jsonb({
                    "last_price": str(ticker.last_price), "bid_price": str(ticker.bid_price),
                    "ask_price": str(ticker.ask_price), "bid_size": str(ticker.bid_size),
                    "ask_size": str(ticker.ask_size), "volume24h": str(ticker.volume24h),
                    "turnover24h": str(ticker.turnover24h), "index_price": str(ticker.index_price) if ticker.index_price is not None else None,
                    "mark_price": str(ticker.mark_price) if ticker.mark_price is not None else None,
                    "raw_reference": ticker.raw_payload,
                }),
            )
            for ticker in tickers
        ]
        if not rows:
            return
        with self.connection.cursor() as cursor:
            cursor.executemany(
                """
            INSERT INTO market_snapshots (
                symbol, snapshot_timestamp, source, exchange, exchange_timestamp, fetched_at,
                processed_at, status, snapshot
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol, snapshot_timestamp, source) DO UPDATE SET
                exchange_timestamp = EXCLUDED.exchange_timestamp,
                fetched_at = EXCLUDED.fetched_at,
                processed_at = EXCLUDED.processed_at,
                status = EXCLUDED.status,
                snapshot = EXCLUDED.snapshot
                """,
                rows,
            )

    def cleanup_market_snapshots(self, retention_days: int) -> None:
        if retention_days <= 0:
            raise ValueError("retention_days must be positive")
        self.connection.execute(
            "DELETE FROM market_snapshots WHERE snapshot_timestamp < now() - (%s * interval '1 day')",
            (retention_days,),
        )

    def insert_market_observations(self, metric: str, observations: Sequence[Observation]) -> None:
        rows = [
            (
                observation.symbol, metric, observation.value, observation.unit,
                observation.source, observation.exchange, observation.exchange_timestamp,
                observation.fetched_at, observation.processed_at, observation.status.value,
                Jsonb(observation.raw_payload),
                Jsonb(asdict(observation.raw_reference)) if observation.raw_reference else None,
            )
            for observation in observations
        ]
        if not rows:
            return
        with self.connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO market_observations (
                    symbol, metric, value, unit, source, exchange, exchange_timestamp,
                    fetched_at, processed_at, status, raw_payload, raw_reference
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, metric, exchange_timestamp, source) DO UPDATE SET
                    value = EXCLUDED.value, unit = EXCLUDED.unit,
                    fetched_at = EXCLUDED.fetched_at, processed_at = EXCLUDED.processed_at,
                    status = EXCLUDED.status, raw_payload = EXCLUDED.raw_payload,
                    raw_reference = EXCLUDED.raw_reference
                """,
                rows,
            )

    def upsert_system_health(
        self, component: str, status: DataStatus, checked_at: datetime, details: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO system_health (component, status, checked_at, details)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (component) DO UPDATE SET
                status = EXCLUDED.status, checked_at = EXCLUDED.checked_at, details = EXCLUDED.details
            """,
            (component, status.value, checked_at, Jsonb(dict(details))),
        )

    def create_screening_run(self, run_timestamp: datetime, rule_version: str = "phase1-basic-v1") -> int:
        row = self.connection.execute(
            "INSERT INTO screening_runs (run_timestamp, status, rule_version) VALUES (%s, %s, %s) RETURNING id",
            (run_timestamp, "AVAILABLE", rule_version),
        ).fetchone()
        self.last_screening_run_id = int(row[0])
        return self.last_screening_run_id

    def insert_stage1_result(self, run_id: int, result: Stage1Result) -> int:
        payload = stage1_result_json(result)
        row = self.connection.execute(
            """
            INSERT INTO screening_results (
                run_id, symbol, category, classification, reason, reason_codes, key_metrics,
                data_snapshot_reference, status, inputs_used, indicators, structure, processed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, symbol) DO UPDATE SET
                category = EXCLUDED.category, classification = EXCLUDED.classification,
                reason = EXCLUDED.reason, reason_codes = EXCLUDED.reason_codes,
                key_metrics = EXCLUDED.key_metrics, data_snapshot_reference = EXCLUDED.data_snapshot_reference,
                status = EXCLUDED.status, inputs_used = EXCLUDED.inputs_used,
                indicators = EXCLUDED.indicators, structure = EXCLUDED.structure,
                processed_at = EXCLUDED.processed_at
            RETURNING id
            """,
            (
                run_id, result.symbol, result.category, result.classification, result.reason,
                Jsonb(payload["reason_codes"]), Jsonb(payload["key_metrics"]),
                Jsonb(payload["data_snapshot_reference"]) if payload["data_snapshot_reference"] else None,
                result.status.value, Jsonb(payload["inputs_used"]), Jsonb(payload["indicators"]),
                payload["structure"], result.timestamp,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("Stage 1 result upsert did not return its identity")
        return int(row[0])

    def insert_runtime_health_event(
        self,
        component: str,
        state: str,
        *,
        outage_started_at: datetime | None,
        recovered_at: datetime | None,
        reason: str | None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        duration = None
        if outage_started_at is not None and recovered_at is not None:
            duration = (recovered_at - outage_started_at).total_seconds()
        self.connection.execute(
            """
            INSERT INTO runtime_health_events (
                component, state, outage_started_at, recovered_at, duration_seconds, reason, details
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (component, state, outage_started_at, recovered_at, duration, reason, Jsonb(dict(details or {}))),
        )

    def cleanup_klines(self, retention_days: Mapping[str, int]) -> None:
        for statement, params in retention_delete_statements(retention_days):
            self.connection.execute(statement, params)

    def count(self, table: str) -> int:
        if table not in {"symbols", "klines", "market_snapshots", "screening_results"}:
            raise ValueError("unsupported count table")
        return int(self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])

    def _stream_candle_rows(self, query, params):
        # Bound decoded rows and libpq result buffers while retaining the exact
        # configured window and source payloads. An explicit transaction also
        # supports the original autocommit Paper reader.
        cursor_factory = getattr(self.connection, "cursor", None)
        if cursor_factory is None:
            # Recording repository adapters used by offline callers.
            yield from self.connection.execute(query, params).fetchall()
            return
        with self.connection.transaction():
            with cursor_factory(name="phase1_candles_"+uuid4().hex) as cursor:
                cursor.itersize = 512
                cursor.execute(query, params)
                yield from cursor

    def load_latest_market_batch(
        self, *, limit: int = 200, candle_limit: int = 100,
        requested_symbols: tuple[str, ...] | None = None
    ) -> MarketDataBatch | None:
        """Rebuild a bounded Stage1 input batch from canonical data.

        The collector only fetches a configured closed-bar window.  Reusing
        that bound here is important: loading the entire retained Kline table
        would materialize historical candles that are not needed by Stage1 or
        its context hooks.
        """
        if limit <= 0 or candle_limit <= 0:
            raise ValueError("market batch and candle limits must be positive")
        if requested_symbols is not None and not requested_symbols:
            return None
        scope_clause = " AND s.symbol = ANY(%s)" if requested_symbols is not None else ""
        scope_params = (list(dict.fromkeys(requested_symbols)),) if requested_symbols is not None else ()
        ticker_rows = self.connection.execute(
            """
            SELECT s.symbol, latest.snapshot_timestamp, latest.source, latest.exchange,
                   latest.exchange_timestamp, latest.fetched_at, latest.processed_at,
                   latest.status, latest.snapshot
            FROM symbols AS s
            CROSS JOIN LATERAL (
                SELECT m.snapshot_timestamp, m.source, m.exchange,
                       m.exchange_timestamp, m.fetched_at, m.processed_at,
                       m.status, m.snapshot
                FROM market_snapshots AS m
                WHERE m.symbol = s.symbol AND m.status = 'AVAILABLE'
                ORDER BY m.snapshot_timestamp DESC
                LIMIT 1
            ) AS latest
            WHERE s.category = 'USDT-FUTURES'
              AND upper(s.quote_coin) = 'USDT'
              AND upper(s.symbol_type) = 'PERPETUAL'
              AND lower(s.contract_type) = 'perpetual'
              AND lower(s.raw_payload->>'symbolType') = 'crypto'
              AND lower(s.status) = 'online'
              AND upper(s.base_coin) <> 'RWA'
            """ + scope_clause, scope_params
        ).fetchall()
        if not ticker_rows:
            return None
        parsed_tickers: list[Ticker] = []
        for row in ticker_rows:
            symbol, _, source, exchange, exchange_timestamp, fetched_at, processed_at, status, snapshot = row
            if not isinstance(snapshot, Mapping):
                continue
            parsed_tickers.append(
                Ticker(
                    symbol=symbol,
                    last_price=Decimal(str(snapshot["last_price"])),
                    bid_price=Decimal(str(snapshot["bid_price"])),
                    ask_price=Decimal(str(snapshot["ask_price"])),
                    bid_size=Decimal(str(snapshot.get("bid_size", "0"))),
                    ask_size=Decimal(str(snapshot.get("ask_size", "0"))),
                    volume24h=Decimal(str(snapshot["volume24h"])),
                    turnover24h=Decimal(str(snapshot["turnover24h"])),
                    index_price=Decimal(str(snapshot["index_price"])) if snapshot.get("index_price") else None,
                    mark_price=Decimal(str(snapshot["mark_price"])) if snapshot.get("mark_price") else None,
                    exchange_timestamp=exchange_timestamp,
                    fetched_at=fetched_at,
                    processed_at=processed_at,
                    status=DataStatus(status),
                    raw_payload=snapshot.get("raw_reference", {}),
                    source=source,
                    exchange=exchange,
                )
            )
        parsed_tickers.sort(key=lambda item: item.turnover24h, reverse=True)
        parsed_tickers = parsed_tickers[:limit]
        symbols = tuple(item.symbol for item in parsed_tickers)
        if not symbols:
            return None
        candle_rows = self._stream_candle_rows(
            """
            SELECT symbol, interval, bar_open_timestamp, open, high, low, close, volume, turnover,
                   exchange_timestamp, fetched_at, processed_at, status, raw_payload, source, exchange
            FROM (
                SELECT symbol, interval, bar_open_timestamp, open, high, low, close, volume, turnover,
                       exchange_timestamp, fetched_at, processed_at, status, raw_payload, source, exchange,
                       ROW_NUMBER() OVER (
                           PARTITION BY symbol, interval
                           ORDER BY bar_open_timestamp DESC
                       ) AS candle_rank
                FROM klines
                WHERE symbol = ANY(%s) AND status = 'AVAILABLE'
            ) AS ranked_candles
            WHERE candle_rank <= %s
            ORDER BY symbol, interval, bar_open_timestamp DESC
            """,
            (list(symbols), candle_limit),
        )
        candles_by_symbol: dict[str, dict[str, list[Candle]]] = {symbol: {} for symbol in symbols}
        for row in candle_rows:
            symbol, interval, opened, opened_at, high, low, close, volume, turnover, exchange_timestamp, fetched_at, processed_at, status, raw_payload, source, exchange = row
            candles_by_symbol.setdefault(symbol, {}).setdefault(interval, []).append(
                Candle(
                    symbol=symbol, interval=interval, bar_open_timestamp=opened,
                    open=Decimal(str(opened_at)), high=Decimal(str(high)), low=Decimal(str(low)),
                    close=Decimal(str(close)), volume=Decimal(str(volume)), turnover=Decimal(str(turnover)),
                    exchange_timestamp=exchange_timestamp, fetched_at=fetched_at, processed_at=processed_at,
                    status=DataStatus(status), is_closed=True, raw_payload=raw_payload or [],
                    source=source, exchange=exchange,
                )
            )
        for intervals in candles_by_symbol.values():
            for interval in intervals:
                intervals[interval].sort(key=lambda candle: candle.bar_open_timestamp)
        instrument_rows = self.connection.execute(
            """
            SELECT symbol, category, base_coin, quote_coin, symbol_type, contract_type,
                   status, price_precision, quantity_precision, min_order_qty,
                   max_order_qty, fetched_at, raw_payload, exchange_timestamp,
                   source, exchange
            FROM symbols
            WHERE symbol = ANY(%s)
            ORDER BY symbol
            """,
            (list(symbols),),
        ).fetchall()
        instruments = [
            Instrument(
                symbol=row[0], category=row[1], base_coin=row[2], quote_coin=row[3],
                symbol_type=row[4], contract_type=row[5], status=row[6],
                price_precision=row[7], quantity_precision=row[8],
                min_order_qty=row[9], max_order_qty=row[10], fetched_at=row[11],
                raw_payload=row[12] or {}, exchange_timestamp=row[13],
                source=row[14], exchange=row[15],
            )
            for row in instrument_rows
        ]
        now = max(item.processed_at for item in parsed_tickers)
        return MarketDataBatch(now, instruments, parsed_tickers, symbols, candles_by_symbol)

    def load_phase2_universe(self, *, limit: int = 200) -> list[dict[str, str]]:
        """Return the currently ranked Phase 1 USDT perpetual Universe.

        The latest persisted market snapshot is used for turnover ordering;
        symbols without a current available snapshot are excluded. This keeps
        derivative enrichment aligned with the same persisted market view that
        feeds Stage1.
        """
        rows = self.connection.execute(
            """
            SELECT s.symbol, s.base_coin, s.quote_coin, s.contract_type, s.status
            FROM symbols AS s
            LEFT JOIN LATERAL (
                SELECT (m.snapshot->>'turnover24h')::numeric AS turnover
                FROM market_snapshots AS m
                WHERE m.symbol = s.symbol AND m.status = 'AVAILABLE'
                ORDER BY m.snapshot_timestamp DESC
                LIMIT 1
            ) AS latest ON TRUE
            WHERE s.category = 'USDT-FUTURES'
              AND upper(s.quote_coin) = 'USDT'
              AND upper(s.symbol_type) = 'PERPETUAL'
              AND lower(s.contract_type) = 'perpetual'
              AND lower(s.raw_payload->>'symbolType') = 'crypto'
              AND lower(s.status) = 'online'
              AND latest.turnover IS NOT NULL
            ORDER BY latest.turnover DESC, s.symbol
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "symbol": row[0], "base_coin": row[1], "quote_coin": row[2],
                "contract_type": row[3], "status": row[4],
            }
            for row in rows
        ]

    def load_latest_stage1_ab_symbols(self, *, limit: int = 200) -> tuple[str, ...]:
        """Return the latest persisted A/B symbols for bounded Phase 3 context."""
        rows = self.connection.execute(
            """
            SELECT symbol
            FROM screening_results
            WHERE run_id = (SELECT max(run_id) FROM screening_results)
              AND category IN ('A', 'B')
            ORDER BY category, symbol
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return tuple(str(row[0]).upper() for row in rows)
