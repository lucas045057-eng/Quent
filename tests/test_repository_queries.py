from datetime import datetime, timezone

from quant_phase1.repositories import Phase1Repository


NOW = datetime(2026, 9, 21, 4, 30, tzinfo=timezone.utc)


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class RecordingConnection:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if len(self.calls) == 1:
            return Result([
                (
                    "BTCUSDT", NOW, "bitget_v3_rest", "bitget", NOW, NOW, NOW, "AVAILABLE",
                    {
                        "last_price": "100", "bid_price": "99.9", "ask_price": "100.1",
                        "bid_size": "1", "ask_size": "1", "volume24h": "10",
                        "turnover24h": "1000", "index_price": "100", "mark_price": "100",
                    },
                )
            ])
        return Result([])


def test_latest_market_batch_uses_symbol_index_lookups():
    connection = RecordingConnection()

    batch = Phase1Repository(connection).load_latest_market_batch(limit=1)

    assert batch is not None
    snapshot_sql = connection.calls[0][0].lower()
    assert "cross join lateral" in snapshot_sql
    assert "distinct on" not in snapshot_sql


def test_latest_market_batch_limits_candles_per_symbol_and_interval():
    connection = RecordingConnection()

    Phase1Repository(connection).load_latest_market_batch(limit=1, candle_limit=10)

    candle_sql, candle_params = connection.calls[1]
    assert "row_number() over" in candle_sql.lower()
    assert "partition by symbol, interval" in candle_sql.lower()
    assert "candle_rank <=" in candle_sql.lower()
    assert candle_params[1] == 10


def test_phase2_universe_requires_canonical_perpetual_identity():
    connection = RecordingConnection()

    Phase1Repository(connection).load_phase2_universe(limit=2)

    query = connection.calls[0][0].lower()
    assert "upper(s.symbol_type) = 'perpetual'" in query
    assert "lower(s.symbol_type) = 'crypto'" not in query
    assert "lower(s.raw_payload->>'symboltype') = 'crypto'" in query


def test_research_scope_filters_before_loading_candles_and_retains_crypto_identity():
    connection = RecordingConnection()
    Phase1Repository(connection).load_latest_market_batch(
        limit=3, requested_symbols=("BTCUSDT", "ETHUSDT", "DOGEUSDT"))
    query, params = connection.calls[0]
    assert "s.symbol = ANY(%s)" in query
    assert params == (["BTCUSDT", "ETHUSDT", "DOGEUSDT"],)
    assert "symbolType" in query and "'crypto'" in query
    assert "'online'" in query and "'PERPETUAL'" in query
    assert connection.calls[1][1][0] == ["BTCUSDT"]


def test_empty_research_scope_cannot_fall_back_to_full_market():
    connection = RecordingConnection()
    assert Phase1Repository(connection).load_latest_market_batch(requested_symbols=()) is None
    assert connection.calls == []


def test_full_market_candles_are_streamed_in_a_transaction_without_fetchall():
    from contextlib import contextmanager
    class StreamingConnection(RecordingConnection):
        def __init__(self):
            super().__init__()
            self.transactions=[]
            self.server_cursor=None
        @contextmanager
        def transaction(self):
            self.transactions.append('begin')
            try: yield
            finally: self.transactions.append('end')
        @contextmanager
        def cursor(self, *, name):
            assert self.transactions==['begin']
            assert name.startswith('phase1_candles_')
            outer=self
            class Cursor:
                def execute(self,query,params):outer.calls.append((query,params))
                def __iter__(self):return iter(())
                def fetchall(self):raise AssertionError('full candle window must not be buffered')
            self.server_cursor=Cursor()
            yield self.server_cursor
    connection=StreamingConnection()
    batch=Phase1Repository(connection).load_latest_market_batch(limit=1,candle_limit=100)
    assert batch is not None
    assert connection.transactions==['begin','end']
    assert connection.server_cursor.itersize==512
    assert connection.calls[1][1]==(['BTCUSDT'],100)
