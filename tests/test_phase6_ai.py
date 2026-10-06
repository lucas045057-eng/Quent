from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_phase6.ai import (
    AIErrorCode,
    AIRequest,
    AIResponse,
    AIService,
    AIUsage,
    BoundedAIQueue,
    BudgetConfig,
    BudgetLedger,
    ProviderError,
    RateLimiter,
    StrictSchema,
    TTLCache,
)
from quant_phase6.prompts import PromptDefinition, PromptRegistry
from quant_phase6.security import AISafeContext
from quant_phase1.config import Settings


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def context(text="external data"):
    return AISafeContext(
        task_id="test",
        allowed_fields={"summary": text, "status": "AVAILABLE"},
        context_hash="a" * 64,
    )


class FakeProvider:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response


def request(provider="fake"):
    prompts = PromptRegistry(
        [
            PromptDefinition(
                prompt_id="news.classify",
                prompt_version="v1",
                schema_version="news-output-v1",
                model_policy_version="policy-v1",
                system_instructions="Classify only the supplied external data.",
            )
        ]
    )
    envelope = prompts.render("news.classify", context())
    return AIRequest(
        purpose="news_classification",
        prompt_id="news.classify",
        prompt_version="v1",
        schema_version="news-output-v1",
        model_policy_version="policy-v1",
        provider=provider,
        model="fake-model",
        envelope=envelope,
        context_hash="a" * 64,
        timeout_seconds=2.0,
        max_output_bytes=4096,
    )


def schema():
    return StrictSchema(
        required=("event_type", "summary", "evidence_refs"),
        allowed=("event_type", "summary", "evidence_refs"),
        enums={"event_type": ("SECURITY", "GENERAL_MARKET_CONTEXT")},
    )


def test_strict_schema_rejects_unknown_missing_and_invalid_values():
    valid = schema().validate({"event_type": "SECURITY", "summary": "x", "evidence_refs": ["h"]})
    assert valid["event_type"] == "SECURITY"
    with pytest.raises(ValueError):
        schema().validate({"event_type": "SECURITY", "summary": "x", "evidence_refs": [], "extra": 1})
    with pytest.raises(ValueError):
        schema().validate({"event_type": "SECURITY", "summary": "x"})
    with pytest.raises(ValueError):
        schema().validate({"event_type": "BUY", "summary": "x", "evidence_refs": []})


def test_queue_rate_limit_and_cache_are_bounded():
    queue = BoundedAIQueue(max_size=1)
    queue.put("one")
    with pytest.raises(ProviderError) as exc:
        queue.put("two")
    assert exc.value.code is AIErrorCode.QUEUE_FULL
    assert queue.get() == "one"

    limiter = RateLimiter(max_concurrency=1, min_interval=timedelta(seconds=1))
    assert limiter.acquire(NOW)
    assert not limiter.acquire(NOW)
    limiter.release()
    assert not limiter.acquire(NOW + timedelta(milliseconds=500))
    assert limiter.acquire(NOW + timedelta(seconds=1))

    cache = TTLCache(max_entries=1, ttl=timedelta(seconds=10), max_bytes=100)
    cache.put("k", {"value": 1}, NOW)
    assert cache.get("k", NOW) == {"value": 1}
    cache.put("too-large", "x" * 200, NOW)
    assert cache.get("too-large", NOW) is None


def test_budget_soft_and_hard_limits_are_config_driven():
    ledger = BudgetLedger(BudgetConfig(
        daily_soft=Decimal("1"), daily_hard=Decimal("2"),
        monthly_soft=Decimal("3"), monthly_hard=Decimal("4"),
    ))
    assert ledger.decision(Decimal("0.5"), NOW).value == "ALLOW"
    ledger.record(Decimal("1.2"), NOW)
    assert ledger.decision(Decimal("0.1"), NOW).value == "SOFT_DEGRADE"
    ledger.record(Decimal("1"), NOW)
    assert ledger.decision(Decimal("0.1"), NOW).value == "HARD_REJECT"


def test_gateway_validates_response_and_uses_cache():
    provider = FakeProvider(
        AIResponse(
            provider="fake",
            model="fake-model",
            structured_output={"event_type": "SECURITY", "summary": "ok", "evidence_refs": ["h"]},
            usage=AIUsage(input_tokens=10, output_tokens=5, total_tokens=15, estimated_cost=Decimal("0.1")),
            latency_ms=2,
        )
    )
    service = AIService({"fake": provider}, budget=BudgetLedger(BudgetConfig()), now=lambda: NOW)
    first = service.complete(request(), schema())
    second = service.complete(request(), schema())
    assert first.status.value == "AVAILABLE"
    assert second.cache_hit is True
    assert provider.calls == 1
    assert first.provider == "fake"


def test_gateway_bounds_retry_and_fallback_records_actual_provider():
    primary = FakeProvider(error=ProviderError(AIErrorCode.TIMEOUT, retryable=True))
    fallback = FakeProvider(
        AIResponse(
            provider="fallback",
            model="fallback-model",
            structured_output={"event_type": "SECURITY", "summary": "fallback", "evidence_refs": ["h"]},
            usage=AIUsage(input_tokens=1, output_tokens=1, total_tokens=2, estimated_cost=Decimal("0.01")),
            latency_ms=1,
        )
    )
    service = AIService(
        {"primary": primary, "fallback": fallback},
        budget=BudgetLedger(BudgetConfig()),
        max_retries=1,
        now=lambda: NOW,
    )
    result = service.complete(request("primary"), schema(), fallback_provider="fallback")
    assert result.status.value == "AVAILABLE"
    assert result.provider == "fallback"
    assert result.retry_count == 2
    assert primary.calls == 2
    assert fallback.calls == 1


def test_invalid_provider_output_is_error_and_never_available():
    provider = FakeProvider(
        AIResponse(
            provider="fake",
            model="fake-model",
            structured_output={"event_type": "BUY", "summary": "hallucinated", "evidence_refs": []},
            usage=AIUsage(input_tokens=1, output_tokens=1, total_tokens=2, estimated_cost=Decimal("0")),
            latency_ms=1,
        )
    )
    service = AIService({"fake": provider}, budget=BudgetLedger(BudgetConfig()), now=lambda: NOW)
    result = service.complete(request(), schema())
    assert result.status.value == "ERROR"
    assert result.error_code is AIErrorCode.SCHEMA_ERROR


def test_soft_budget_degrades_to_deterministic_unavailable_without_provider_call():
    provider = FakeProvider(
        AIResponse(
            provider="fake", model="fake-model",
            structured_output={"event_type": "SECURITY", "summary": "x", "evidence_refs": []},
            usage=AIUsage(estimated_cost=Decimal("0.1")),
        )
    )
    ledger = BudgetLedger(BudgetConfig(daily_soft=Decimal("0"), daily_hard=Decimal("1")))
    service = AIService({"fake": provider}, budget=ledger, now=lambda: NOW)
    result = service.complete(request(), schema())
    assert result.status.value == "NOT_AVAILABLE"
    assert result.error_code is AIErrorCode.BUDGET
    assert provider.calls == 0


def test_gateway_maps_queue_full_and_preserves_provider_error_code():
    provider = FakeProvider(error=ProviderError(AIErrorCode.RATE_LIMIT, retryable=False))
    service = AIService({"fake": provider}, budget=BudgetLedger(BudgetConfig()), queue_size=1, now=lambda: NOW)
    service.queue.put("occupied")
    queued = service.complete(request(), schema())
    assert queued.status.value == "ERROR"
    assert queued.error_code is AIErrorCode.QUEUE_FULL

    service.queue.get()
    service.rate_limiter._last = None
    failed = service.complete(request(), schema())
    assert failed.error_code is AIErrorCode.RATE_LIMIT
    assert failed.status.value == "ERROR"


def test_gateway_from_settings_wires_all_runtime_bounds_without_keys():
    settings = Settings.from_env({
        "TRADING_MODE": "paper",
        "PHASE6_AI_QUEUE_CAPACITY": "7",
        "PHASE6_AI_CONCURRENCY": "3",
        "PHASE6_AI_RATE_INTERVAL_MS": "125",
        "PHASE6_AI_MAX_RETRIES": "2",
        "PHASE6_RAW_CACHE_TTL_HOURS": "4",
        "PHASE6_AI_DAILY_SOFT_BUDGET": "1.5",
        "PHASE6_AI_DAILY_HARD_BUDGET": "2.5",
    })
    service = AIService.from_settings({}, settings, now=lambda: NOW)
    assert service.queue._max_size == 7
    assert service.rate_limiter.max_concurrency == 3
    assert service.rate_limiter.min_interval == timedelta(milliseconds=125)
    assert service.max_retries == 2
    assert service.cache.ttl == timedelta(hours=4)
    assert service.budget.config.daily_soft == Decimal("1.5")


def test_corrupt_cache_entry_is_discarded_and_recomputed():
    provider = FakeProvider(
        AIResponse(
            provider="fake", model="fake-model",
            structured_output={"event_type": "SECURITY", "summary": "ok", "evidence_refs": []},
            usage=AIUsage(total_tokens=1, estimated_cost=Decimal("0")),
        )
    )
    service = AIService({"fake": provider}, budget=BudgetLedger(BudgetConfig()), now=lambda: NOW)
    service.cache.put(request().request_hash, {"corrupt": True}, NOW)
    result = service.complete(request(), schema())
    assert result.status.value == "AVAILABLE"
    assert result.cache_hit is False
    assert provider.calls == 1
