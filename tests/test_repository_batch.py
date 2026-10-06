from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_phase1.contracts import Candle, DataStatus, Instrument, Observation, Ticker
from quant_phase1.repositories import Phase1Repository


class RecordingCursor:
    def __init__(self, calls: list[tuple[str, list[tuple[object, ...]]]]) -> None:
        self.calls = calls

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def executemany(self, statement: str, rows: list[tuple[object, ...]]) -> None:
        self.calls.append((statement, rows))


class RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[tuple[object, ...]]]] = []

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self.calls)


class ExecuteConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, statement: str, params: tuple[object, ...]) -> None:
        self.calls.append((statement, params))


def _candle(symbol: str, opened_at: datetime) -> Candle:
    return Candle(
        symbol=symbol,
        interval="5m",
        bar_open_timestamp=opened_at,
        open=Decimal("99"),
        high=Decimal("102"),
        low=Decimal("98"),
        close=Decimal("101"),
        volume=Decimal("10"),
        turnover=Decimal("1000"),
        exchange_timestamp=opened_at,
        fetched_at=opened_at,
        processed_at=opened_at,
        status=DataStatus.AVAILABLE,
        is_closed=True,
        raw_payload=["runtime"],
    )


def _instrument(symbol: str, fetched_at: datetime) -> Instrument:
    return Instrument(
        symbol=symbol,
        category="USDT-FUTURES",
        base_coin=symbol[:-4],
        quote_coin="USDT",
        symbol_type="PERPETUAL",
        contract_type="perpetual",
        status="online",
        price_precision=2,
        quantity_precision=3,
        min_order_qty=Decimal("0.001"),
        max_order_qty=None,
        fetched_at=fetched_at,
        raw_payload={},
        exchange_timestamp=fetched_at,
    )


def _ticker(symbol: str, observed_at: datetime) -> Ticker:
    return Ticker(
        symbol=symbol,
        last_price=Decimal("100"),
        bid_price=Decimal("99.9"),
        ask_price=Decimal("100.1"),
        bid_size=Decimal("1"),
        ask_size=Decimal("1"),
        volume24h=Decimal("10"),
        turnover24h=Decimal("1000"),
        index_price=None,
        mark_price=None,
        exchange_timestamp=observed_at,
        fetched_at=observed_at,
        processed_at=observed_at,
        status=DataStatus.AVAILABLE,
        raw_payload={},
    )


def test_upsert_candles_batches_rows_in_one_database_call() -> None:
    connection = RecordingConnection()
    repository = Phase1Repository(connection)
    opened_at = datetime(2026, 9, 20, 10, 25, tzinfo=timezone.utc)

    repository.upsert_candles(
        [
            _candle("BTCUSDT", opened_at),
            _candle("ETHUSDT", opened_at + timedelta(minutes=5)),
        ]
    )

    assert len(connection.calls) == 1
    statement, rows = connection.calls[0]
    assert "ON CONFLICT (symbol, interval, bar_open_timestamp)" in statement
    assert len(rows) == 2


def test_upsert_instruments_and_snapshots_batch_rows() -> None:
    connection = RecordingConnection()
    repository = Phase1Repository(connection)
    observed_at = datetime(2026, 9, 20, 10, 25, tzinfo=timezone.utc)

    repository.upsert_instruments(
        [_instrument("BTCUSDT", observed_at), _instrument("ETHUSDT", observed_at)]
    )
    repository.insert_market_snapshots(
        [_ticker("BTCUSDT", observed_at), _ticker("ETHUSDT", observed_at)], observed_at
    )

    assert [len(rows) for _, rows in connection.calls] == [2, 2]


def test_insert_market_observations_batches_canonical_rows() -> None:
    connection = RecordingConnection()
    repository = Phase1Repository(connection)
    observed_at = datetime(2026, 9, 20, 10, 25, tzinfo=timezone.utc)
    observations = [
        Observation("BTCUSDT", Decimal("100"), "bitget_v3_rest", "bitget", observed_at, observed_at, observed_at, DataStatus.AVAILABLE, {}),
        Observation("ETHUSDT", Decimal("200"), "bitget_v3_rest", "bitget", observed_at, observed_at, observed_at, DataStatus.AVAILABLE, {}),
    ]

    repository.insert_market_observations("last_price", observations)

    assert len(connection.calls) == 1
    assert len(connection.calls[0][1]) == 2
    assert "market_observations" in connection.calls[0][0]


def test_upsert_system_health_writes_a_heartbeat() -> None:
    connection = ExecuteConnection()
    repository = Phase1Repository(connection)
    checked_at = datetime(2026, 9, 20, 10, 25, tzinfo=timezone.utc)

    repository.upsert_system_health("quant-engine", DataStatus.AVAILABLE, checked_at, {"heartbeat": True})

    assert len(connection.calls) == 1
    assert "system_health" in connection.calls[0][0]
