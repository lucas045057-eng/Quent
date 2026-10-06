from __future__ import annotations

from datetime import datetime, timezone

from quant_phase6.contracts import EventStatus
from quant_phase6.ingestion import ExternalEventIngestor, FetchError
from quant_phase6.normalization import FreshnessPolicy
from quant_phase6.sources import SourceDefinition, SourceRegistry, SourceType


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def source(source_id="fixture.news", source_type=SourceType.RSS):
    return SourceDefinition(
        source_id=source_id,
        source_type=source_type,
        base_url="https://news.example.test/feed",
        allowed_hosts=("news.example.test",),
        allowed_paths=("/feed", "/article"),
        parser_version="fixture-v1",
        policy_version="policy-v1",
    )


class Fetcher:
    def __init__(self, payloads=None, error=None):
        self.payloads = payloads or []
        self.error = error

    def fetch(self, definition):
        if self.error:
            raise self.error
        return self.payloads


def test_ingestor_normalizes_and_deduplicates_bounded_feed():
    ingestor = ExternalEventIngestor(
        SourceRegistry([source()]),
        Fetcher([
            {"id": "n-1", "url": "https://news.example.test/article/1", "headline": "h", "summary": "s", "event_type": "SECURITY", "published_at": "2026-09-22T11:00:00Z"},
            {"id": "n-1", "url": "https://news.example.test/article/1", "headline": "h", "summary": "s", "event_type": "SECURITY", "published_at": "2026-09-22T11:00:00Z"},
        ]),
        freshness=FreshnessPolicy(),
        max_events=10,
    )
    result = ingestor.ingest("news", "fixture.news", observed_at=NOW, fetched_at=NOW)
    assert result.status is EventStatus.AVAILABLE
    assert len(result.events) == 1


def test_source_outage_is_typed_and_does_not_raise_to_engine():
    ingestor = ExternalEventIngestor(
        SourceRegistry([source()]),
        Fetcher(error=FetchError("timeout")),
        freshness=FreshnessPolicy(),
    )
    result = ingestor.ingest("news", "fixture.news", observed_at=NOW, fetched_at=NOW)
    assert result.status is EventStatus.ERROR
    assert result.events == ()
    assert result.reason_code == "SOURCE_FETCH_ERROR"


def test_unconfigured_source_is_not_available_and_payload_count_is_bounded():
    ingestor = ExternalEventIngestor(SourceRegistry(), Fetcher(), freshness=FreshnessPolicy(), max_events=1)
    missing = ingestor.ingest("news", "not-configured", observed_at=NOW, fetched_at=NOW)
    assert missing.status is EventStatus.NOT_AVAILABLE
    assert missing.reason_code == "SOURCE_NOT_CONFIGURED"

    bounded = ExternalEventIngestor(
        SourceRegistry([source()]),
        Fetcher([
            {"id": "1", "url": "https://news.example.test/article/1", "headline": "a", "event_type": "SECURITY"},
            {"id": "2", "url": "https://news.example.test/article/2", "headline": "b", "event_type": "SECURITY"},
        ]),
        freshness=FreshnessPolicy(), max_events=1,
    ).ingest("news", "fixture.news", observed_at=NOW, fetched_at=NOW)
    assert len(bounded.events) == 1


def test_ingestor_does_not_materialize_an_unbounded_source_iterator():
    consumed = 0

    def infinite_payloads():
        nonlocal consumed
        while True:
            consumed += 1
            yield {
                "id": str(consumed), "headline": "bounded", "event_type": "SECURITY",
                "published_at": "2026-09-22T11:00:00Z",
            }

    class InfiniteFetcher:
        def fetch(self, _definition):
            return infinite_payloads()

    result = ExternalEventIngestor(
        SourceRegistry([source()]), InfiniteFetcher(), freshness=FreshnessPolicy(), max_events=3
    ).ingest("news", "fixture.news", observed_at=NOW, fetched_at=NOW)

    assert len(result.events) == 3
    assert result.truncated is True
    assert consumed == 4
