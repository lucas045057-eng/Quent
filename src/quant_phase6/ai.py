"""Provider-neutral, bounded AI Gateway contracts and local orchestration."""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
import json
import hashlib
import time
from typing import Any, Callable, Mapping, Protocol

from .contracts import EventStatus
from .prompts import PromptEnvelope
from .security import AISafeContext


class AIErrorCode(StrEnum):
    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    AUTHENTICATION = "AUTHENTICATION"
    TRANSPORT = "TRANSPORT"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    INVALID_JSON = "INVALID_JSON"
    SCHEMA_ERROR = "SCHEMA_ERROR"
    BUDGET = "BUDGET"
    POLICY_BLOCK = "POLICY_BLOCK"
    QUEUE_FULL = "QUEUE_FULL"
    EVIDENCE_ERROR = "EVIDENCE_ERROR"
    PERSISTENCE_ERROR = "PERSISTENCE_ERROR"


class BudgetDecision(StrEnum):
    ALLOW = "ALLOW"
    SOFT_DEGRADE = "SOFT_DEGRADE"
    HARD_REJECT = "HARD_REJECT"


@dataclass(frozen=True, slots=True)
class AIUsage:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost: Decimal | None = None
    latency_ms: int | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("input_tokens", self.input_tokens), ("cached_input_tokens", self.cached_input_tokens),
            ("output_tokens", self.output_tokens), ("total_tokens", self.total_tokens),
            ("latency_ms", self.latency_ms),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.estimated_cost is not None and self.estimated_cost < 0:
            raise ValueError("estimated_cost cannot be negative")


@dataclass(frozen=True, slots=True)
class AIResponse:
    provider: str
    model: str
    structured_output: Mapping[str, Any]
    usage: AIUsage
    latency_ms: int | None = None


class ProviderError(RuntimeError):
    def __init__(self, code: AIErrorCode, *, retryable: bool = False) -> None:
        super().__init__(code.value)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class AIRequest:
    purpose: str
    prompt_id: str
    prompt_version: str
    schema_version: str
    model_policy_version: str
    provider: str
    model: str
    envelope: PromptEnvelope
    context_hash: str
    timeout_seconds: float
    max_output_bytes: int
    estimated_cost: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        if not self.purpose.strip() or not self.provider.strip() or not self.model.strip():
            raise ValueError("AI request identity is required")
        if self.timeout_seconds <= 0 or self.max_output_bytes <= 0:
            raise ValueError("AI request limits must be positive")
        if len(self.context_hash) != 64:
            raise ValueError("context_hash must be SHA-256")
        if self.estimated_cost < 0:
            raise ValueError("estimated_cost cannot be negative")

    @property
    def request_hash(self) -> str:
        payload = {
            "purpose": self.purpose,
            "prompt_id": self.prompt_id,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "model_policy_version": self.model_policy_version,
            "provider": self.provider,
            "model": self.model,
            "context_hash": self.context_hash,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class AIResult:
    status: EventStatus
    output: Mapping[str, Any] | None
    provider: str | None
    model: str | None
    usage: AIUsage | None
    request_hash: str
    error_code: AIErrorCode | None = None
    retry_count: int = 0
    cache_hit: bool = False


@dataclass(frozen=True, slots=True)
class StrictSchema:
    required: tuple[str, ...]
    allowed: tuple[str, ...]
    enums: Mapping[str, tuple[str, ...]]

    def validate(self, value: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("structured output must be an object")
        unknown = set(value) - set(self.allowed)
        if unknown:
            raise ValueError(f"unknown structured fields: {sorted(unknown)}")
        missing = set(self.required) - set(value)
        if missing:
            raise ValueError(f"missing structured fields: {sorted(missing)}")
        for field, values in self.enums.items():
            if field in value and value[field] not in values:
                raise ValueError(f"invalid enum for {field}")
        return dict(value)


class AIProvider(Protocol):
    def complete(self, request: AIRequest) -> AIResponse:
        ...


class FakeAIProvider:
    """Explicit test-only provider; it performs no network, env, or file access."""

    test_only = True

    def __init__(self, responder: Callable[[AIRequest], AIResponse]) -> None:
        if not callable(responder):
            raise TypeError("FakeAIProvider requires an explicit test responder")
        self._responder = responder
        self.calls = 0

    def complete(self, request: AIRequest) -> AIResponse:
        self.calls += 1
        response = self._responder(request)
        if not isinstance(response, AIResponse):
            raise TypeError("FakeAIProvider responder must return AIResponse")
        return response


@dataclass(frozen=True, slots=True)
class BudgetConfig:
    daily_soft: Decimal = Decimal("10")
    daily_hard: Decimal = Decimal("20")
    monthly_soft: Decimal = Decimal("100")
    monthly_hard: Decimal = Decimal("200")

    def __post_init__(self) -> None:
        if not (Decimal("0") <= self.daily_soft <= self.daily_hard):
            raise ValueError("daily budgets must be ordered and non-negative")
        if not (Decimal("0") <= self.monthly_soft <= self.monthly_hard):
            raise ValueError("monthly budgets must be ordered and non-negative")


class BudgetLedger:
    def __init__(self, config: BudgetConfig) -> None:
        self.config = config
        self._records: list[tuple[datetime, Decimal]] = []

    def record(self, cost: Decimal, at: datetime) -> None:
        if cost < 0:
            raise ValueError("cost cannot be negative")
        self._records.append((_utc(at), cost))

    def decision(self, estimated_cost: Decimal, at: datetime) -> BudgetDecision:
        if estimated_cost < 0:
            raise ValueError("estimated_cost cannot be negative")
        at = _utc(at)
        daily = sum(cost for timestamp, cost in self._records if timestamp.date() == at.date())
        monthly = sum(cost for timestamp, cost in self._records if timestamp.year == at.year and timestamp.month == at.month)
        if daily + estimated_cost > self.config.daily_hard or monthly + estimated_cost > self.config.monthly_hard:
            return BudgetDecision.HARD_REJECT
        if daily + estimated_cost > self.config.daily_soft or monthly + estimated_cost > self.config.monthly_soft:
            return BudgetDecision.SOFT_DEGRADE
        return BudgetDecision.ALLOW


class BoundedAIQueue:
    def __init__(self, max_size: int) -> None:
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        self._items: deque[Any] = deque(maxlen=max_size)
        self._max_size = max_size

    def put(self, item: Any) -> None:
        if len(self._items) >= self._max_size:
            raise ProviderError(AIErrorCode.QUEUE_FULL)
        self._items.append(item)

    def get(self) -> Any:
        if not self._items:
            raise IndexError("AI queue is empty")
        return self._items.popleft()


class RateLimiter:
    def __init__(self, max_concurrency: int, min_interval: timedelta) -> None:
        if max_concurrency <= 0 or min_interval < timedelta(0):
            raise ValueError("invalid rate limits")
        self.max_concurrency = max_concurrency
        self.min_interval = min_interval
        self._active = 0
        self._last: datetime | None = None

    def acquire(self, now: datetime) -> bool:
        now = _utc(now)
        if self._active >= self.max_concurrency:
            return False
        if self._last is not None and now - self._last < self.min_interval:
            return False
        self._active += 1
        self._last = now
        return True

    def release(self) -> None:
        if self._active:
            self._active -= 1


class TTLCache:
    def __init__(self, max_entries: int, ttl: timedelta, max_bytes: int) -> None:
        if max_entries <= 0 or ttl <= timedelta(0) or max_bytes <= 0:
            raise ValueError("invalid cache bounds")
        self.max_entries = max_entries
        self.ttl = ttl
        self.max_bytes = max_bytes
        self._items: OrderedDict[str, tuple[datetime, Any, int]] = OrderedDict()

    def put(self, key: str, value: Any, now: datetime) -> None:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        if len(encoded) > self.max_bytes:
            return
        self._items.pop(key, None)
        self._items[key] = (_utc(now) + self.ttl, value, len(encoded))
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)

    def get(self, key: str, now: datetime) -> Any | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires, value, _ = item
        if _utc(now) >= expires:
            self._items.pop(key, None)
            return None
        self._items.move_to_end(key)
        return value

    def invalidate(self, key: str) -> None:
        self._items.pop(key, None)


class AIService:
    @classmethod
    def from_settings(
        cls,
        providers: Mapping[str, AIProvider],
        settings: Any,
        *,
        budget: BudgetLedger | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> "AIService":
        """Construct the bounded gateway from the central Phase 6 settings."""
        ledger = budget or BudgetLedger(
            BudgetConfig(
                daily_soft=Decimal(str(settings.phase6_ai_daily_soft_budget)),
                daily_hard=Decimal(str(settings.phase6_ai_daily_hard_budget)),
                monthly_soft=Decimal(str(settings.phase6_ai_monthly_soft_budget)),
                monthly_hard=Decimal(str(settings.phase6_ai_monthly_hard_budget)),
            )
        )
        return cls(
            providers,
            budget=ledger,
            max_retries=settings.phase6_ai_max_retries,
            queue_size=settings.phase6_ai_queue_capacity,
            cache_ttl=timedelta(hours=settings.phase6_raw_cache_ttl_hours),
            cache_bytes=settings.phase6_max_ai_output_bytes,
            max_concurrency=settings.phase6_ai_concurrency,
            min_interval=timedelta(milliseconds=settings.phase6_ai_rate_interval_ms),
            default_timeout_seconds=settings.phase6_ai_timeout_seconds,
            now=now,
        )

    def __init__(
        self,
        providers: Mapping[str, AIProvider],
        *,
        budget: BudgetLedger,
        max_retries: int = 1,
        queue_size: int = 32,
        cache_ttl: timedelta = timedelta(hours=24),
        cache_entries: int = 256,
        cache_bytes: int = 65_536,
        max_concurrency: int = 2,
        min_interval: timedelta = timedelta(milliseconds=50),
        default_timeout_seconds: float = 10.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if max_retries < 0 or max_retries > 3:
            raise ValueError("max_retries must be between 0 and 3")
        if default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be positive")
        self.providers = dict(providers)
        self.budget = budget
        self.max_retries = max_retries
        self.queue = BoundedAIQueue(queue_size)
        self.cache = TTLCache(cache_entries, cache_ttl, cache_bytes)
        self.rate_limiter = RateLimiter(max_concurrency, min_interval)
        self.default_timeout_seconds = default_timeout_seconds
        self.now = now or (lambda: datetime.now(timezone.utc))

    def complete(
        self,
        request: AIRequest,
        schema: Any,
        *,
        fallback_provider: str | None = None,
        evidence_validator: Callable[[Mapping[str, Any]], Any] | None = None,
        persist_result: Callable[[AIResult], None] | None = None,
    ) -> AIResult:
        now = _utc(self.now())
        cache_started = time.monotonic()
        cached = self.cache.get(request.request_hash, now)
        if cached is not None:
            try:
                output = schema.validate(cached.output)
                if evidence_validator is not None:
                    evidence_validator(output)
                elapsed_ms = max(0, int((time.monotonic() - cache_started) * 1000))
                cached_result = replace(
                    cached,
                    output=output,
                    usage=AIUsage(latency_ms=elapsed_ms),
                    retry_count=0,
                    cache_hit=True,
                )
                if persist_result is not None:
                    try:
                        persist_result(cached_result)
                    except Exception:
                        return AIResult(
                            EventStatus.ERROR, None, cached.provider, cached.model, None,
                            request.request_hash, AIErrorCode.PERSISTENCE_ERROR,
                        )
                return cached_result
            except (AttributeError, TypeError, ValueError, KeyError):
                self.cache.invalidate(request.request_hash)
        budget_decision = self.budget.decision(request.estimated_cost, now)
        if budget_decision is not BudgetDecision.ALLOW:
            return _persist_terminal(
                AIResult(EventStatus.NOT_AVAILABLE, None, request.provider, request.model, None,
                         request.request_hash, AIErrorCode.BUDGET),
                persist_result,
            )
        if not self.rate_limiter.acquire(now):
            return _persist_terminal(
                AIResult(EventStatus.ERROR, None, request.provider, request.model, None,
                         request.request_hash, AIErrorCode.RATE_LIMIT),
                persist_result,
            )
        try:
            self.queue.put(request)
        except ProviderError as exc:
            self.rate_limiter.release()
            return _persist_terminal(
                AIResult(EventStatus.ERROR, None, request.provider, request.model, None,
                         request.request_hash, exc.code),
                persist_result,
            )
        self.queue.get()
        attempts = 0
        last_error_code = AIErrorCode.TRANSPORT
        last_provider: str | None = None
        last_model: str | None = None
        last_usage: AIUsage | None = None
        providers = [request.provider]
        if fallback_provider and fallback_provider not in providers:
            providers.append(fallback_provider)
        try:
            for provider_name in providers:
                provider = self.providers.get(provider_name)
                if provider is None:
                    continue
                for attempt in range(self.max_retries + 1):
                    try:
                        provider_request = (
                            request if provider_name == request.provider
                            else replace(request, provider=provider_name)
                        )
                        response = provider.complete(provider_request)
                        last_provider = response.provider
                        last_model = response.model
                        last_usage = response.usage
                        if response.usage.estimated_cost is not None:
                            self.budget.record(response.usage.estimated_cost, now)
                        encoded = json.dumps(response.structured_output, separators=(",", ":"), default=str).encode()
                        if len(encoded) > request.max_output_bytes:
                            raise ProviderError(AIErrorCode.SCHEMA_ERROR)
                        output = schema.validate(response.structured_output)
                        if evidence_validator is not None:
                            try:
                                evidence_validator(output)
                            except (TypeError, ValueError, KeyError):
                                return _persist_terminal(AIResult(
                                    EventStatus.ERROR, None, response.provider, response.model, response.usage,
                                    request.request_hash, AIErrorCode.EVIDENCE_ERROR, attempts,
                                ), persist_result)
                        usage = response.usage
                        result = AIResult(EventStatus.AVAILABLE, output, response.provider, response.model, usage, request.request_hash, retry_count=attempts)
                        if persist_result is not None:
                            try:
                                persist_result(result)
                            except Exception:
                                return AIResult(
                                    EventStatus.ERROR, None, response.provider, response.model, None,
                                    request.request_hash, AIErrorCode.PERSISTENCE_ERROR, attempts,
                                )
                        self.cache.put(request.request_hash, result, now)
                        return result
                    except ProviderError as exc:
                        last_error_code = exc.code
                        last_provider = provider_name
                        attempts += 1
                        if not exc.retryable or attempt >= self.max_retries:
                            break
                    except (ValueError, TypeError, json.JSONDecodeError):
                        return _persist_terminal(
                            AIResult(EventStatus.ERROR, None, provider_name, last_model, last_usage,
                                     request.request_hash, AIErrorCode.SCHEMA_ERROR, attempts),
                            persist_result,
                        )
            return _persist_terminal(AIResult(
                EventStatus.ERROR,
                None,
                last_provider,
                last_model,
                last_usage,
                request.request_hash,
                last_error_code,
                attempts,
            ), persist_result)
        finally:
            self.rate_limiter.release()


def _persist_terminal(
    result: AIResult,
    callback: Callable[[AIResult], None] | None,
) -> AIResult:
    if callback is None:
        return result
    try:
        callback(result)
        return result
    except Exception:
        return AIResult(
            EventStatus.ERROR, None, result.provider, result.model, None,
            result.request_hash, AIErrorCode.PERSISTENCE_ERROR, result.retry_count,
        )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def complete_strategy_analysis(service: AIService, request: AIRequest, schema: Any, *, evidence_validator):
    """V2 research task through the existing budget, queue and validation gateway."""
    if request.purpose != "V2_DEEP_ANALYSIS" or request.schema_version != "STRATEGY_V2":
        raise ValueError("invalid V2 research task")
    if not isinstance(service, AIService) or not callable(evidence_validator):
        raise TypeError("bounded gateway and evidence validation required")
    return service.complete(request, schema, evidence_validator=evidence_validator)
