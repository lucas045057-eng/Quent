from __future__ import annotations

from datetime import datetime, timezone

from quant_phase1.stage1 import Stage1Result
from quant_phase6.contracts import EventStatus
from quant_phase6.enrichment import Phase6ContextRuntime, build_stage1_context
from quant_phase1.config import Settings
from quant_phase1.entrypoints.engine import run_phase6_context_hook


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def stage1(symbol="BTCUSDT"):
    return Stage1Result(
        symbol=symbol,
        category="A",
        reason="deterministic stage1",
        reason_codes=("A_RULE",),
        inputs_used=("ticker", "kline"),
        indicators={},
        structure="BULLISH",
        key_metrics={"return_pct": 1.0},
        status="AVAILABLE",
    )


def ref(event_id, status="AVAILABLE"):
    return {"event_id": event_id, "status": status, "source": "fixture"}


def test_phase6_enrichment_is_additive_and_context_only():
    original = stage1()
    enriched = build_stage1_context(
        original,
        screening_run_id=1,
        news_refs=(ref("news-1"),),
        macro_refs=(),
        unlock_refs=(),
        ai_results=(),
        processed_at=NOW,
    )
    assert original == stage1()
    assert enriched.symbol == "BTCUSDT"
    assert enriched.status is EventStatus.AVAILABLE
    assert enriched.context_only is True
    assert enriched.news_refs[0]["event_id"] == "news-1"


def test_unavailable_phase6_never_removes_stage1_candidate():
    original = stage1("ETHUSDT")
    enriched = build_stage1_context(
        original,
        screening_run_id=2,
        news_refs=(ref("news-1", "NOT_AVAILABLE"),),
        macro_refs=(ref("macro-1", "ERROR"),),
        unlock_refs=(),
        ai_results=(),
        processed_at=NOW,
    )
    assert enriched.status is EventStatus.ERROR
    assert original.category == "A"
    assert original.symbol == "ETHUSDT"


def test_runtime_preserves_input_order_and_never_rewrites_stage1():
    results = (stage1("BTCUSDT"), stage1("ETHUSDT"))
    runtime = Phase6ContextRuntime()
    output = runtime.build(
        results,
        screening_run_id=3,
        refs_by_symbol={"BTCUSDT": {"news": (ref("n"),)}, "ETHUSDT": {}},
        processed_at=NOW,
    )
    assert tuple(item.symbol for item in output) == ("BTCUSDT", "ETHUSDT")
    assert tuple(item.result for item in output) == results
    assert all(item.context_only for item in output)


def test_engine_hook_is_disabled_without_phase6_and_does_not_construct_runtime():
    class ExplodingRuntime:
        def build(self, *args, **kwargs):
            raise AssertionError("disabled Phase 6 must not run")

    outcome = run_phase6_context_hook(
        Settings.from_env({}), NOW, results=(stage1(),), screening_run_id=1,
        runtime=ExplodingRuntime(),
    )
    assert outcome == {"contexts": 0, "available": 0, "errors": 0}


def test_engine_hook_degrades_on_provider_failure_without_rewriting_stage1():
    original = stage1()
    outcome = run_phase6_context_hook(
        Settings.from_env({"PHASE6_ENABLED": "1"}), NOW,
        results=(original,), screening_run_id=1,
        refs_by_symbol={"BTCUSDT": {"ai": (ref("ai-1", "ERROR"),)}},
    )
    assert outcome == {"contexts": 1, "available": 0, "errors": 1}
    assert original.category == "A"


def test_conflicting_context_is_context_only_and_all_missing_is_not_available():
    original = stage1()
    outcome = run_phase6_context_hook(
        Settings.from_env({"PHASE6_ENABLED": "1"}), NOW,
        results=(original,), screening_run_id=1,
        refs_by_symbol={"BTCUSDT": {
            "news": (ref("news-1", "AVAILABLE"), ref("news-2", "ERROR")),
        }},
    )
    assert outcome == {"contexts": 1, "available": 0, "errors": 1}
    assert original.category == "A"

    missing = run_phase6_context_hook(
        Settings.from_env({"PHASE6_ENABLED": "1"}), NOW,
        results=(original,), screening_run_id=1,
    )
    assert missing == {"contexts": 1, "available": 0, "errors": 0}


def test_context_references_drop_nested_raw_payload_and_transport_metadata():
    enriched = build_stage1_context(
        stage1(), screening_run_id=4,
        news_refs=({"event_id": "n", "nested": {"raw_payload": "secret", "safe": "ok"}, "headers": {"x": "y"}},),
        processed_at=NOW,
    )
    assert enriched.news_refs == ({"event_id": "n", "nested": {"safe": "ok"}},)
