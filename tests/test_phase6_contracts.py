from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_phase6.contracts import (
    EventStatus,
    Importance,
    MacroEvent,
    NewsEvent,
    Provenance,
    RawReference,
    RecipientCategory,
    UnlockEvent,
    compute_macro_surprise,
)
from quant_phase6.sources import SourceType


NOW = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)


def _provenance(source_id: str = "fixture.news") -> Provenance:
    return Provenance(
        source_id=source_id,
        source_ref="n-1",
        url="https://news.example.test/feed/1",
        content_hash="a" * 64,
        observed_at=NOW,
        fetched_at=NOW,
        processed_at=NOW,
        parser_version="fixture-v1",
        schema_version="phase6-event-v1",
        normalization_version="normalizer-v1",
    )


def _common(source: str = "fixture.news", source_type: SourceType = SourceType.RSS):
    return dict(
        event_id="evt-1",
        source=source,
        source_type=source_type,
        source_ref="n-1",
        url="https://news.example.test/feed/1",
        published_at=NOW,
        observed_at=NOW,
        event_at=NOW,
        fetched_at=NOW,
        processed_at=NOW,
        event_type="SECURITY",
        entities=("Example",),
        symbols=("BTCUSDT",),
        summary="bounded summary",
        importance=Importance.HIGH,
        status=EventStatus.AVAILABLE,
        confidence=Decimal("0.90"),
        content_hash="a" * 64,
        parser_version="fixture-v1",
        raw_reference=RawReference(reference="sha256:a", content_hash="a" * 64, byte_size=32),
        provenance=_provenance(source),
    )


def test_news_contract_preserves_source_times_and_provenance():
    event = NewsEvent(**_common(), headline="Example headline")

    assert event.event_id == "evt-1"
    assert event.published_at == NOW
    assert event.observed_at == NOW
    assert event.processed_at == NOW
    assert event.provenance.content_hash == "a" * 64


def test_contracts_reject_naive_time_and_confidence_outside_bounds():
    values = _common()
    values["observed_at"] = datetime(2026, 9, 22)
    with pytest.raises(ValueError, match="UTC"):
        NewsEvent(**values, headline="bad")

    values = _common()
    values["confidence"] = Decimal("1.1")
    with pytest.raises(ValueError, match="confidence"):
        NewsEvent(**values, headline="bad")


def test_macro_surprise_requires_compatible_units_and_missing_is_not_zero():
    assert compute_macro_surprise(Decimal("105"), Decimal("100"), unit="USD", forecast_unit="USD") == Decimal("5")
    assert compute_macro_surprise(Decimal("105"), Decimal("100"), unit="USD", forecast_unit="EUR") is None

    event = MacroEvent(
        **_common("fixture.macro", SourceType.PUBLIC_API),
        macro_event_type="CPI",
        region="US",
        scheduled_at=NOW,
        released_at=NOW,
        actual=None,
        forecast=None,
        previous=None,
        unit=None,
        surprise=None,
    )
    assert event.actual is None
    assert event.surprise is None


def test_unlock_pct_requires_event_time_amount_and_compatible_supply():
    event = UnlockEvent(
        **_common("fixture.unlock", SourceType.PROJECT),
        symbol="ABCUSDT",
        asset="ABC",
        amount=Decimal("100"),
        amount_unit="ABC",
        value=None,
        value_currency=None,
        value_at=None,
        circulating_supply=Decimal("1000"),
        circulating_supply_unit="ABC",
        circulating_supply_ref="supply-1",
        unlock_pct=Decimal("0.1"),
        recipient_category=RecipientCategory.TEAM,
    )
    assert event.unlock_pct == Decimal("0.1")

    values = {
        **_common("fixture.unlock", SourceType.PROJECT),
        "event_at": None,
    }
    with pytest.raises(ValueError, match="unlock_pct"):
        UnlockEvent(
            **values,
            symbol="ABCUSDT",
            asset="ABC",
            amount=Decimal("100"),
            amount_unit="ABC",
            value=None,
            value_currency=None,
            value_at=None,
            circulating_supply=Decimal("1000"),
            circulating_supply_unit="ABC",
            circulating_supply_ref="supply-1",
            unlock_pct=Decimal("0.1"),
            recipient_category=RecipientCategory.UNKNOWN,
        )
