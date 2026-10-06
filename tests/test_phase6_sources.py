from __future__ import annotations

import pytest

from quant_phase6.sources import (
    BoundedFetchPolicy,
    SourceDefinition,
    SourceRegistry,
    SourceType,
)


def _source(**overrides):
    values = {
        "source_id": "fixture.news",
        "source_type": SourceType.RSS,
        "base_url": "https://news.example.test/feed",
        "allowed_hosts": ("news.example.test",),
        "allowed_paths": ("/feed",),
        "parser_version": "fixture-news-v1",
        "policy_version": "source-policy-v1",
    }
    values.update(overrides)
    return SourceDefinition(**values)


def test_registry_accepts_only_audited_https_host_and_path():
    registry = SourceRegistry([_source()])

    assert registry.require("fixture.news").source_type is SourceType.RSS
    assert registry.validate_url("fixture.news", "https://news.example.test/feed?id=1")
    assert not registry.validate_url("fixture.news", "http://news.example.test/feed")
    assert not registry.validate_url("fixture.news", "https://other.example.test/feed")
    assert not registry.validate_url("fixture.news", "https://news.example.test/article")


def test_unknown_source_and_unapproved_redirect_are_rejected():
    registry = SourceRegistry([_source()])

    with pytest.raises(KeyError):
        registry.require("not-approved")
    assert registry.validate_redirect("fixture.news", "https://news.example.test/feed")
    assert not registry.validate_redirect("fixture.news", "https://cdn.example.test/feed")


def test_source_policy_is_bounded_and_rejects_invalid_limits():
    policy = BoundedFetchPolicy(max_bytes=4096, timeout_seconds=2.0, max_redirects=0, max_concurrency=1)
    assert policy.max_bytes == 4096
    with pytest.raises(ValueError):
        BoundedFetchPolicy(max_bytes=0)
    with pytest.raises(ValueError):
        _source(fetch_policy=BoundedFetchPolicy(max_bytes=70 * 1024 * 1024))


def test_source_definition_has_no_secret_or_signed_url_surface():
    source = _source()
    assert source.base_url == "https://news.example.test/feed"
    assert "Authorization" not in source.__annotations__
    assert "token" not in source.__annotations__
