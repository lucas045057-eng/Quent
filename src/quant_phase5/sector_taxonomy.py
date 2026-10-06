"""Version-controlled, deterministic sector taxonomy loader."""

from __future__ import annotations

from csv import DictReader
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from quant_phase1.time import ensure_utc

from .contracts import MappingStatus, SectorMembership


REQUIRED_COLUMNS = (
    "mapping_version",
    "symbol",
    "sector",
    "source_reference",
    "effective_from",
    "effective_to",
)


def _parse_timestamp(value: str, field_name: str) -> datetime:
    if not value.strip():
        raise ValueError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return ensure_utc(parsed)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be UTC ISO-8601") from exc


def load_sector_taxonomy(path: str | Path, *, known_symbols: set[str] | None = None) -> tuple[SectorMembership, ...]:
    """Load and validate a static taxonomy without network or inference."""
    path = Path(path)
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = DictReader(handle)
        if reader.fieldnames is None or tuple(reader.fieldnames) != REQUIRED_COLUMNS:
            raise ValueError(f"required columns must be exactly {','.join(REQUIRED_COLUMNS)}")
        rows: list[SectorMembership] = []
        for line_number, row in enumerate(reader, start=2):
            if any(row.get(column) is None for column in REQUIRED_COLUMNS):
                raise ValueError(f"missing taxonomy field at line {line_number}")
            mapping_version = row["mapping_version"].strip()
            symbol = row["symbol"].strip().upper()
            sector = row["sector"].strip()
            source_reference = row["source_reference"].strip()
            if not mapping_version or not symbol or not sector or not source_reference:
                raise ValueError(f"empty taxonomy field at line {line_number}")
            if known_symbols is not None and symbol not in {item.upper() for item in known_symbols}:
                raise ValueError(f"unknown symbol at line {line_number}: {symbol}")
            effective_from = _parse_timestamp(row["effective_from"], "effective_from")
            effective_to = _parse_timestamp(row["effective_to"], "effective_to") if row["effective_to"].strip() else None
            rows.append(
                SectorMembership(
                    mapping_version=mapping_version,
                    symbol=symbol,
                    sector=sector,
                    source_reference=source_reference,
                    effective_from=effective_from,
                    effective_to=effective_to,
                    status=MappingStatus.AVAILABLE,
                    processed_at=effective_from,
                )
            )
    grouped: dict[tuple[str, str], list[SectorMembership]] = {}
    for row in rows:
        grouped.setdefault((row.mapping_version, row.symbol), []).append(row)
    for identity, members in grouped.items():
        ordered = sorted(members, key=lambda item: item.effective_from)
        for previous, current in zip(ordered, ordered[1:]):
            if previous.effective_to is None or current.effective_from < previous.effective_to:
                raise ValueError(f"taxonomy effective interval overlap: {identity[0]}/{identity[1]}")
    return tuple(sorted(rows, key=lambda item: (item.mapping_version, item.symbol, item.effective_from)))


def resolve_sector(
    symbol: str,
    memberships: Iterable[SectorMembership],
    at: datetime,
    *,
    mapping_version: str | None = None,
) -> SectorMembership:
    """Resolve a static mapping at a UTC point; unknown symbols stay explicit."""
    at = ensure_utc(at)
    symbol = symbol.strip().upper()
    memberships = tuple(memberships)
    symbol_versions = {row.mapping_version for row in memberships if row.symbol == symbol}
    if mapping_version is None and len(symbol_versions) > 1:
        raise ValueError("mapping_version is required when multiple taxonomy versions exist")
    candidates = [
        row
        for row in memberships
        if row.symbol == symbol
        and (mapping_version is None or row.mapping_version == mapping_version)
        and row.status is MappingStatus.AVAILABLE
        and row.effective_from <= at
        and (row.effective_to is None or at < row.effective_to)
    ]
    if candidates:
        return max(candidates, key=lambda item: item.effective_from)
    version = mapping_version or "phase5-unknown"
    return SectorMembership(
        mapping_version=version,
        symbol=symbol,
        sector="UNKNOWN",
        source_reference="static-taxonomy:unknown-fallback",
        effective_from=at,
        effective_to=None,
        status=MappingStatus.NOT_AVAILABLE,
        processed_at=at,
    )
