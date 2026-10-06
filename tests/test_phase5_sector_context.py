from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_phase1.config import Settings
from quant_phase5.contracts import ContextStatus, DirectionState, MarketLeaderContext
from quant_phase5.sector_context import compute_sector_context
from quant_phase5.sector_taxonomy import load_sector_taxonomy


NOW = datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc)
HEADER = "mapping_version,symbol,sector,source_reference,effective_from,effective_to\n"


def taxonomy(tmp_path, symbols):
    path = tmp_path / "map.csv"
    path.write_text(
        HEADER + "".join(
            f"phase5-v1,{symbol},DEFI,static,2020-01-01T00:00:00+00:00,\n" for symbol in symbols
        ),
        encoding="utf-8",
    )
    return load_sector_taxonomy(path)


def context(symbol: str, value: str = "1", status: ContextStatus = ContextStatus.AVAILABLE):
    unavailable = status is not ContextStatus.AVAILABLE
    return MarketLeaderContext(
        symbol=symbol,
        timeframe="15m",
        context_timestamp=NOW,
        input_window_start=NOW,
        input_window_end=NOW,
        return_pct=None if unavailable else Decimal(value),
        trend_state="NOT_AVAILABLE" if unavailable else DirectionState.RANGE,
        structure_state="NOT_AVAILABLE" if unavailable else "RANGE",
        volatility_state="NOT_AVAILABLE" if unavailable else "NORMAL",
        volume_state="NOT_AVAILABLE" if unavailable else "NORMAL",
        volatility_value=None if unavailable else Decimal("0.001"),
        volume_ratio=None if unavailable else Decimal("1"),
        freshness_status=status,
        data_quality={"test": True},
        source_count=1 if not unavailable else 0,
        missing_count=0 if not unavailable else 1,
        status=status,
        processed_at=NOW,
        reason_code="MISSING_INPUT" if unavailable else None,
        missing_evidence=("missing",) if unavailable else (),
    )


def test_sector_arithmetic_and_member_counts(tmp_path):
    symbols = [f"S{index:03d}USDT" for index in range(5)]
    rows = compute_sector_context(
        "DEFI", "phase5-v1", symbols, taxonomy(tmp_path, symbols),
        {symbol: context(symbol, str(index + 1)) for index, symbol in enumerate(symbols)},
        timeframe="15m", context_timestamp=NOW, universe_run_id=7,
        settings=Settings.from_env({}), processed_at=NOW,
    )
    assert rows.sector == "DEFI"
    assert rows.member_count == 5
    assert rows.available_count == 5
    assert rows.missing_count == 0
    assert rows.sector_return_pct == Decimal("3")
    assert rows.sector_positive_ratio == Decimal("1")
    assert rows.status is ContextStatus.AVAILABLE


def test_sector_missing_member_at_coverage_boundary_is_partial(tmp_path):
    symbols = [f"S{index:03d}USDT" for index in range(5)]
    rows = compute_sector_context(
        "DEFI", "phase5-v1", symbols, taxonomy(tmp_path, symbols),
        {symbol: context(symbol) for symbol in symbols[:4]},
        timeframe="15m", context_timestamp=NOW, universe_run_id=7,
        settings=Settings.from_env({}), processed_at=NOW,
    )
    assert rows.status is ContextStatus.PARTIAL
    assert rows.coverage_ratio == Decimal("0.8")
    assert rows.sector_return_pct == Decimal("1")
    assert rows.missing_evidence


def test_minimum_members_and_unknown_sector_are_not_available(tmp_path):
    symbols = [f"S{index:03d}USDT" for index in range(4)]
    rows = compute_sector_context(
        "DEFI", "phase5-v1", symbols, taxonomy(tmp_path, symbols),
        {symbol: context(symbol) for symbol in symbols},
        timeframe="15m", context_timestamp=NOW, universe_run_id=7,
        settings=Settings.from_env({}), processed_at=NOW,
    )
    assert rows.status is ContextStatus.NOT_AVAILABLE
    assert rows.reason_code.value == "INSUFFICIENT_COVERAGE"
    assert rows.sector_return_pct is None

    unknown = compute_sector_context(
        "UNKNOWN", "phase5-v1", symbols, taxonomy(tmp_path, symbols),
        {}, timeframe="15m", context_timestamp=NOW, universe_run_id=7,
        settings=Settings.from_env({}), processed_at=NOW,
    )
    assert unknown.status is ContextStatus.NOT_AVAILABLE
    assert unknown.reason_code.value == "UNKNOWN_SECTOR"


def test_sector_context_requires_exact_timestamp_and_symbol_identity(tmp_path):
    symbols = [f"S{index:03d}USDT" for index in range(5)]
    with pytest.raises(ValueError, match="context identity"):
        compute_sector_context(
            "DEFI", "phase5-v1", symbols, taxonomy(tmp_path, symbols),
            {symbols[0]: context(symbols[0]), symbols[1]: context(symbols[1])},
            timeframe="15m", context_timestamp=NOW.replace(minute=2), universe_run_id=7,
            settings=Settings.from_env({}), processed_at=NOW,
        )
