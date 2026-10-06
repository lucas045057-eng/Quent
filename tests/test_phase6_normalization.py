from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_phase6.contracts import EventStatus
from quant_phase6.normalization import (
    DedupIndex,
    FreshnessPolicy,
    NormalizationError,
    event_fingerprint,
    normalize_macro,
    normalize_news,
    normalize_unlock,
    freshness_status,
)
from quant_phase6.sources import SourceDefinition, SourceRegistry, SourceType


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def _registry(source_id="fixture.news", source_type=SourceType.RSS):
    return SourceRegistry(
        [
            SourceDefinition(
                source_id=source_id,
                source_type=source_type,
                base_url="https://news.example.test/feed",
                allowed_hosts=("news.example.test",),
                allowed_paths=("/feed", "/article"),
                parser_version="fixture-v1",
                policy_version="policy-v1",
            )
        ]
    )


def test_news_normalization_is_utc_and_does_not_invent_missing_published_time():
    event = normalize_news(
        _registry(),
        {
            "id": "n-1",
            "url": "https://news.example.test/article/1?utm_source=x",
            "headline": "A bounded headline",
            "summary": "A bounded summary",
            "published_at": None,
            "event_type": "SECURITY",
            "symbols": ["BTCUSDT"],
        },
        source_id="fixture.news",
        observed_at=NOW,
        fetched_at=NOW,
    )

    assert event.published_at is None
    assert event.status is EventStatus.PARTIAL
    assert event.reason_code == "MISSING_PUBLISHED_AT"
    assert event.event_at is None
    assert event.content_hash == event.provenance.content_hash
    assert "utm_source" not in event.url


def test_normalizer_rejects_unapproved_source_url_and_missing_source_id():
    with pytest.raises(NormalizationError):
        normalize_news(
            _registry(),
            {"url": "https://evil.example.test/article", "headline": "x"},
            source_id="fixture.news",
            observed_at=NOW,
            fetched_at=NOW,
        )
    with pytest.raises(NormalizationError):
        normalize_news(
            _registry(),
            {"url": "https://news.example.test/article/1"},
            source_id="fixture.news",
            observed_at=NOW,
            fetched_at=NOW,
        )


def test_macro_surprise_is_not_computed_for_incompatible_units():
    event = normalize_macro(
        _registry("fixture.macro", SourceType.PUBLIC_API),
        {
            "id": "m-1",
            "event_type": "CPI",
            "region": "US",
            "scheduled_at": "2026-09-22T11:00:00Z",
            "released_at": "2026-09-22T11:30:00Z",
            "actual": "105",
            "forecast": "100",
            "previous": "99",
            "unit": "USD",
            "forecast_unit": "EUR",
        },
        source_id="fixture.macro",
        observed_at=NOW,
        fetched_at=NOW,
    )
    assert event.surprise is None
    assert event.status is EventStatus.PARTIAL
    assert event.reason_code == "INCOMPATIBLE_UNITS"


def test_unlock_pct_requires_supply_reference_and_never_uses_zero_fallback():
    event = normalize_unlock(
        _registry("fixture.unlock", SourceType.PROJECT),
        {
            "id": "u-1",
            "symbol": "ABCUSDT",
            "asset": "ABC",
            "event_at": "2026-09-23T00:00:00Z",
            "amount": "100",
            "amount_unit": "ABC",
            "circulating_supply": None,
            "circulating_supply_unit": "ABC",
        },
        source_id="fixture.unlock",
        observed_at=NOW,
        fetched_at=NOW,
    )
    assert event.unlock_pct is None
    assert event.status is EventStatus.PARTIAL
    assert event.reason_code == "MISSING_CIRCULATING_SUPPLY"


def test_fingerprint_is_stable_and_title_alone_does_not_deduplicate():
    first = normalize_news(
        _registry(),
        {"id": "n-1", "url": "https://news.example.test/article/1", "headline": "same"},
        source_id="fixture.news",
        observed_at=NOW,
        fetched_at=NOW,
    )
    second = normalize_news(
        _registry(),
        {"id": "n-2", "url": "https://news.example.test/article/2", "headline": "same"},
        source_id="fixture.news",
        observed_at=NOW,
        fetched_at=NOW,
    )
    assert event_fingerprint(first) == event_fingerprint(first)
    assert event_fingerprint(first) != event_fingerprint(second)
    index = DedupIndex()
    assert index.accept(first)
    assert not index.accept(first)
    assert index.accept(second)


def test_cross_source_events_keep_distinct_fingerprints():
    one = normalize_news(
        _registry(),
        {"id": "n-1", "url": "https://news.example.test/article/1", "headline": "same"},
        source_id="fixture.news",
        observed_at=NOW,
        fetched_at=NOW,
    )
    other = normalize_news(
        _registry("fixture.other", SourceType.THIRD_PARTY),
        {"id": "n-1", "url": "https://news.example.test/article/1", "headline": "same"},
        source_id="fixture.other",
        observed_at=NOW,
        fetched_at=NOW,
    )
    assert event_fingerprint(one) != event_fingerprint(other)


def test_freshness_is_type_specific_and_future_macro_is_not_stale():
    policy = FreshnessPolicy(news_max_age=timedelta(hours=1), macro_max_age=timedelta(days=7), unlock_max_age=timedelta(days=30))
    news = normalize_news(
        _registry(),
        {"id": "n-1", "url": "https://news.example.test/article/1", "headline": "same", "event_type": "SECURITY", "published_at": "2026-09-22T09:00:00Z"},
        source_id="fixture.news",
        observed_at=NOW,
        fetched_at=NOW,
    )
    assert freshness_status(news, now=NOW, policy=policy, kind="news") is EventStatus.STALE
    macro = normalize_macro(
        _registry("fixture.macro", SourceType.PUBLIC_API),
        {"id": "m-1", "event_type": "CPI", "region": "US", "scheduled_at": "2026-09-23T00:00:00Z"},
        source_id="fixture.macro",
        observed_at=NOW,
        fetched_at=NOW,
    )
    assert freshness_status(macro, now=NOW, policy=policy, kind="macro") is EventStatus.AVAILABLE
