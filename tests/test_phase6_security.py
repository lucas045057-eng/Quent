from __future__ import annotations

from datetime import datetime, timezone

import pytest

from quant_phase6.normalization import normalize_news
from quant_phase6.security import (
    SafeContextError,
    SecretLeakError,
    build_safe_context,
    redact_sensitive,
)
from quant_phase6.sources import SourceDefinition, SourceRegistry, SourceType


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def event():
    registry = SourceRegistry(
        [
            SourceDefinition(
                source_id="fixture.news",
                source_type=SourceType.RSS,
                base_url="https://news.example.test/feed",
                allowed_hosts=("news.example.test",),
                allowed_paths=("/feed", "/article"),
                parser_version="fixture-v1",
                policy_version="policy-v1",
            )
        ]
    )
    return normalize_news(
        registry,
        {
            "id": "n-1",
            "url": "https://news.example.test/article/1",
            "headline": "Safe headline",
            "summary": "Untrusted source text",
            "event_type": "GENERAL_MARKET_CONTEXT",
            "published_at": "2026-09-22T11:30:00Z",
            "symbols": ["BTCUSDT"],
        },
        source_id="fixture.news",
        observed_at=NOW,
        fetched_at=NOW,
        processed_at=NOW,
    )


def test_safe_context_is_explicit_and_hashable():
    context = build_safe_context(
        event(),
        task_id="news-summary",
        phase5_context={"direction_regime": "RANGE", "status": "AVAILABLE"},
    )
    data = context.to_dict()
    assert data["event_id"] == "fixture.news:n-1"
    assert data["symbols"] == ["BTCUSDT"]
    assert data["phase5_context"]["direction_regime"] == "RANGE"
    assert "raw_payload" not in data
    assert "secret" not in repr(data).lower()
    assert len(context.context_hash) == 64


def test_unknown_phase5_fields_and_signed_urls_are_rejected():
    with pytest.raises(SafeContextError):
        build_safe_context(event(), task_id="x", phase5_context={"whole_runtime": {}})

    signed = event()
    object.__setattr__(signed, "url", "https://news.example.test/article/1?token=secret")
    with pytest.raises(SafeContextError):
        build_safe_context(signed, task_id="x")


def test_secret_denylist_is_recursive_and_strict_mode_rejects():
    payload = {
        "api_key": "a",
        "nested": {"authorization": "Bearer x", "safe": "ok"},
        "private_key": "never",
        "database_password": "pw",
        "ssh_key": "key",
        "token": "tok",
        "password": "pw",
        "credential": "cred",
        "cookie": "c",
        "passphrase": "p",
        "secret": "s",
    }
    redacted, paths = redact_sensitive(payload)
    assert redacted == {"nested": {"safe": "ok"}}
    assert len(paths) == 11
    with pytest.raises(SecretLeakError):
        redact_sensitive(payload, strict=True)


def test_safe_context_has_bounded_serialized_size():
    large = event()
    object.__setattr__(large, "summary", "x" * 9000)
    with pytest.raises(SafeContextError):
        build_safe_context(large, task_id="x", max_bytes=1024)
