from datetime import datetime, timezone

import pytest

from quant_phase5.contracts import MappingStatus
from quant_phase5.sector_taxonomy import load_sector_taxonomy, resolve_sector


UTC = timezone.utc


def write_map(tmp_path, content: str):
    path = tmp_path / "sector_map.csv"
    path.write_text(content, encoding="utf-8")
    return path


HEADER = "mapping_version,symbol,sector,source_reference,effective_from,effective_to\n"


def test_known_mapping_and_unknown_fallback_are_explicit(tmp_path):
    path = write_map(
        tmp_path,
        HEADER
        + "phase5-v1,BTCUSDT,MAJOR_CRYPTO,static-test,2020-01-01T00:00:00+00:00,\n",
    )
    rows = load_sector_taxonomy(path, known_symbols={"BTCUSDT", "ETHUSDT"})
    known = resolve_sector("BTCUSDT", rows, datetime(2026, 1, 1, tzinfo=UTC))
    unknown = resolve_sector("ETHUSDT", rows, datetime(2026, 1, 1, tzinfo=UTC))
    assert known.sector == "MAJOR_CRYPTO"
    assert known.status is MappingStatus.AVAILABLE
    assert unknown.sector == "UNKNOWN"
    assert unknown.status is MappingStatus.NOT_AVAILABLE


def test_required_columns_and_symbol_references_are_validated(tmp_path):
    with pytest.raises(ValueError, match="required columns"):
        load_sector_taxonomy(write_map(tmp_path, "symbol,sector\nBTCUSDT,X\n"))
    with pytest.raises(ValueError, match="unknown symbol"):
        load_sector_taxonomy(
            write_map(tmp_path, HEADER + "phase5-v1,ETHUSDT,X,ref,2020-01-01T00:00:00+00:00,\n"),
            known_symbols={"BTCUSDT"},
        )


def test_overlapping_effective_intervals_are_rejected(tmp_path):
    content = HEADER + (
        "phase5-v1,BTCUSDT,A,ref,2020-01-01T00:00:00+00:00,2025-01-01T00:00:00+00:00\n"
        "phase5-v1,BTCUSDT,B,ref,2024-01-01T00:00:00+00:00,\n"
    )
    with pytest.raises(ValueError, match="overlap"):
        load_sector_taxonomy(write_map(tmp_path, content))


def test_versions_can_change_without_runtime_inference_and_load_is_idempotent(tmp_path):
    content = HEADER + (
        "phase5-v1,BTCUSDT,A,ref,2020-01-01T00:00:00+00:00,2025-01-01T00:00:00+00:00\n"
        "phase5-v2,BTCUSDT,B,ref,2025-01-01T00:00:00+00:00,\n"
    )
    path = write_map(tmp_path, content)
    first = load_sector_taxonomy(path)
    second = load_sector_taxonomy(path)
    assert first == second
    assert resolve_sector("BTCUSDT", first, datetime(2024, 1, 1, tzinfo=UTC), mapping_version="phase5-v1").sector == "A"
    assert resolve_sector("BTCUSDT", first, datetime(2026, 1, 1, tzinfo=UTC), mapping_version="phase5-v2").sector == "B"
