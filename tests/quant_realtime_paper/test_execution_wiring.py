from __future__ import annotations

import importlib
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_execution.contracts import ExecutionResultV1, PositionSnapshotV1
from quant_nautilus.acceptance import fixture_case
from quant_phase9.contracts import PatternMatchStatusV1
from quant_phase9.canonical import canonical_sha256
from quant_phase9.policy import PolicyContentV1, PolicyManifestV1, load_approved_policy_manifest
from quant_phase1.contracts import DataStatus
from tests.quant_execution.fixtures import NOW, risk_inputs


REVISION = "b" * 40
_DEFAULT_POLICY = object()
_NO_ADAPTER = object()


def _pipeline(*args, **kwargs):
    try:
        module = importlib.import_module("quant_realtime_paper.wiring")
    except ModuleNotFoundError:
        pytest.fail("realtime paper execution wiring module is missing")
    return module.RealtimePaperExecutionPipeline(*args, **kwargs)


class Store:
    def __init__(self):
        self.intents = {}
        self.states = {}
        self.results = []
        self.positions = []
        self.epoch = 0

    def reserve(self, intent, policy, *, now):
        if intent.intent_id in self.intents:
            return False
        self.intents[intent.intent_id] = intent
        self.states[intent.intent_id] = "RESERVED"
        return True

    def submission_state(self, intent_id):
        return self.states[intent_id]

    def acquire_owner(self, account_id, owner, *, now, lease_seconds):
        self.epoch += 1
        return self.epoch

    def begin_submission(self, intent_id, owner, epoch, *, now):
        if self.states[intent_id] != "RESERVED":
            return False
        self.states[intent_id] = "SUBMITTING"
        return True

    def reject_before_submit(self, result, owner, epoch, *, now):
        assert self.states[result.intent_id]=='SUBMITTING' and result.filled_quantity==0
        self.record_result(result)

    def record_result(self, result):
        self.results.append(result)
        self.states[result.intent_id] = result.status

    def record_position(self, position):
        self.positions.append(position)

    def release_owner(self, account_id, owner, epoch):
        return None


class PaperAdapter:
    def __init__(self, *, fail_submit=False):
        self.intent = None
        self.fail_submit = fail_submit
        self.submit_calls = 0
        self._results = []
        self.position = None

    def capabilities(self):
        return frozenset({"MARKET_IOC", "STOP_MARKET_REDUCE_ONLY", "POSITION_SNAPSHOT", "LOCAL_RECONCILE"})

    def preflight(self, intent, now):
        assert intent.mode == "PAPER"
        self.intent = intent
        self.position = _flat_position(intent)

    def submit(self, intent):
        self.submit_calls += 1
        self.intent = intent
        if self.fail_submit:
            raise OSError("paper adapter unavailable")
        self._results.append(ExecutionResultV1(
            intent.intent_id, "paper-exec-1", intent.client_order_id, (), "REJECTED",
            intent.approved_quantity, Decimal("0"), intent.approved_quantity, None,
            Decimal("0"), "USDT", None, "NOT_REQUIRED", NOW, NOW,
            "LOCAL_PAPER_REJECTED", "test-adapter-v1",
        ))
        self.position = _flat_position(intent)

    def events(self):
        return iter(tuple(self._results))

    def position_snapshot(self, canonical_symbol):
        return self.position

    def reconcile(self):
        return (self.position,) if self.position is not None else ()


def _flat_position(intent):
    return PositionSnapshotV1(
        intent.account_id, intent.venue, intent.canonical_symbol, "PAPER", "FLAT",
        Decimal("0"), "BASE", None, Decimal("10000"), Decimal("10000"), Decimal("0"),
        Decimal("50000"), Decimal("0"), Decimal("0"), Decimal("0"), None,
        "NOT_REQUIRED", (), (), "RECONCILED", NOW, "test-adapter-v1",
    )


def _setup(tmp_path, *, adapter=None, store=None, phase9=None, risk_override=None, policy=_DEFAULT_POLICY,
           revision=REVISION, pattern="TREND_CONTINUATION"):
    case = fixture_case(tmp_path / "fixture", mode="PAPER", now=NOW,
                        code_version=REVISION, pattern=pattern)
    approval = case.policy if policy is _DEFAULT_POLICY else policy
    stage1 = type("Stage1", (), {
        "symbol": "BTCUSDT", "category": "A", "status": DataStatus.AVAILABLE,
        "reason_codes": ("STRUCTURE_ALIGNED",),
    })()
    calls = {"phase9": 0, "risk": 0, "adapter": 0}

    def evaluate(result, now, correlation_id):
        calls["phase9"] += 1
        if phase9 is not None:
            return phase9(case)
        return case.snapshot, case.decision, "ACTIVE"

    def inputs(snapshot, candidate, now):
        calls["risk"] += 1
        values = risk_inputs(now=now)
        values["envelope"] = replace(
            values["envelope"], candidate=candidate,
            evaluation_snapshot_hash=str(snapshot.snapshot_digest),
            input_snapshot_hash=str(candidate.input_snapshot_hash),
        )
        if risk_override:
            values.update(risk_override)
        return values

    paper_store = store or Store()

    def make_adapter(intent):
        calls["adapter"] += 1
        return None if adapter is _NO_ADAPTER else (adapter or PaperAdapter())

    pipeline = _pipeline(
        approved_policy=approval, current_revision=revision,
        phase9_evaluate=evaluate, risk_inputs=inputs, execution_store=paper_store,
        adapter_factory=make_adapter,
        environ={"TRADING_MODE": "paper", "PAPER_ONLY": "true", "LIVE_ALLOWED": "false"},
    )
    return pipeline, stage1, case, calls, paper_store


def _process(pipeline, stage1, **overrides):
    options = dict(
        data_source="REAL_PUBLIC_DATA", market_data_fresh=True, snapshot_complete=True,
        now=NOW, correlation_id="cycle-0001",
    )
    options.update(overrides)
    return pipeline.process_stage1(stage1, **options)


def test_healthy_approved_candidate_passes_phase9_risk_and_paper_audit(tmp_path):
    adapter = PaperAdapter()
    pipeline, stage1, case, calls, store = _setup(tmp_path, adapter=adapter)

    result = _process(pipeline, stage1)

    assert calls == {"phase9": 1, "risk": 1, "adapter": 1}
    assert result.disposition == "PAPER_RESULT_RECORDED"
    assert result.execution_intent.mode == "PAPER"
    assert result.execution_results[0].status == "REJECTED"
    assert len(store.results) == 1 and len(store.positions) == 1
    assert result.reconciliation == "RECONCILED"
    assert {"position_size", "leverage", "order"}.isdisjoint(case.decision.__dataclass_fields__)
    assert all(event["correlation_id"] == "cycle-0001" for event in result.events)
    assert all(isinstance(event["at"], datetime) and event["reason_code"] for event in result.events)



def test_correct_approval_commit_matches_current_build_revision(tmp_path):
    pipeline, stage1, _case, calls, _store = _setup(tmp_path, revision=REVISION)

    result = _process(pipeline, stage1)

    assert result.disposition == "PAPER_RESULT_RECORDED"
    assert calls["phase9"] == 1 and calls["risk"] == 1


def test_missing_paper_adapter_blocks_without_fake_result(tmp_path):
    pipeline, stage1, _case, calls, store = _setup(tmp_path, adapter=_NO_ADAPTER)

    result = _process(pipeline, stage1)

    assert result.disposition == "NO_TRADE_BY_SYSTEM_NOT_READY"
    assert result.reason_code == "PAPER_UNAVAILABLE"
    assert result.execution_results == ()
    assert store.results == [] and calls["adapter"] == 1

def test_risk_rejection_never_constructs_or_calls_paper_adapter(tmp_path):
    override = {"account": replace(risk_inputs()["account"], as_of=NOW.replace(year=2026, day=27))}
    pipeline, stage1, _case, calls, _store = _setup(tmp_path, risk_override=override)

    result = _process(pipeline, stage1)

    assert result.disposition == "NO_TRADE_BY_SYSTEM_NOT_READY"
    assert result.reason_code == "STALE_ACCOUNT"
    assert calls["risk"] == 1 and calls["adapter"] == 0


@pytest.mark.parametrize("blocker,overrides", [
    ("APPROVAL_MISSING", {"policy": None}),
    ("APPROVAL_COMMIT_MISMATCH", {"revision": "c" * 40}),
    ("STALE_MARKET_DATA", {"market_data_fresh": False}),
    ("SNAPSHOT_INCOMPLETE", {"snapshot_complete": False}),
])
def test_unready_provenance_or_market_state_fails_closed(tmp_path, blocker, overrides):
    setup = {key: value for key, value in overrides.items() if key in {"policy", "revision"}}
    process_overrides = {key: value for key, value in overrides.items() if key not in setup}
    pipeline, stage1, _case, calls, _store = _setup(tmp_path, **setup)

    result = _process(pipeline, stage1, **process_overrides)

    assert result.disposition == "NO_TRADE_BY_SYSTEM_NOT_READY"
    assert result.reason_code == blocker
    assert calls["phase9"] == 0 and calls["adapter"] == 0


def test_empty_policy_patterns_block_execution_before_phase9(tmp_path):
    manifest = tmp_path / "empty-policy.json"
    fixture_manifest = fixture_case(
        tmp_path / "empty-fixture", mode="PAPER", now=NOW, code_version=REVISION,
    ).policy.manifest
    content_data = fixture_manifest.policy_content.model_dump(mode="python")
    content_data["enabled_patterns"] = []
    content = PolicyContentV1.model_validate(content_data)
    manifest_data = fixture_manifest.model_dump(mode="python")
    manifest_data.update(
        policy_content=content,
        manifest_digest=str(canonical_sha256(content.model_dump(mode="python"))),
    )
    empty_manifest = PolicyManifestV1.model_validate(manifest_data)
    manifest.write_text(empty_manifest.model_dump_json(), encoding="utf-8")
    from quant_phase9.approval import create_approval_artifact
    approval_path = tmp_path / "empty-approval.json"
    create_approval_artifact(manifest, approval_path, approved_by="test-owner", approved_commit=REVISION)
    approval = load_approved_policy_manifest(manifest, approval_path, expected_commit=REVISION)
    assert approval.manifest.policy_content.enabled_patterns == ()
    adapter = PaperAdapter()
    pipeline, stage1, _case, calls, store = _setup(tmp_path, policy=approval, adapter=adapter)

    result = _process(pipeline, stage1)

    assert result.disposition == "NO_TRADE_BY_SYSTEM_NOT_READY"
    assert result.reason_code == "POLICY_DISABLED"
    assert calls == {"phase9": 0, "risk": 0, "adapter": 0}
    assert result.decision_candidate is None and result.execution_intent is None
    assert adapter.submit_calls == 0
    assert store.intents == {} and store.results == [] and store.positions == []


def test_phase9_insufficient_is_strategy_no_trade_without_risk_or_execution(tmp_path):
    def insufficient(case):
        return case.snapshot, replace(
            case.decision, eligible=False,
            pattern_status=PatternMatchStatusV1.NOT_CONFIGURED,
            reason_codes=("PHASE9_INSUFFICIENT",),
        ), "INACTIVE"

    pipeline, stage1, _case, calls, _store = _setup(tmp_path, phase9=insufficient)

    result = _process(pipeline, stage1)

    assert result.disposition == "NO_TRADE_BY_STRATEGY"
    assert result.reason_code == "PHASE9_INSUFFICIENT"
    assert calls["risk"] == 0 and calls["adapter"] == 0


def test_paper_adapter_exception_never_fabricates_fill_or_execution_result(tmp_path):
    adapter = PaperAdapter(fail_submit=True)
    pipeline, stage1, _case, calls, store = _setup(tmp_path, adapter=adapter)

    result = _process(pipeline, stage1)

    assert result.disposition == "NO_TRADE_BY_SYSTEM_NOT_READY"
    assert result.reason_code == "PAPER_SUBMIT_FAILED"
    assert result.execution_results == ()
    assert store.results == [] and store.positions == []
    assert adapter.submit_calls == 1 and calls["adapter"] == 1


def test_restart_retry_does_not_resubmit_a_durably_reserved_intent(tmp_path):
    adapter = PaperAdapter()
    store = Store()
    first, stage1, _case, _calls, _store = _setup(tmp_path, adapter=adapter, store=store)
    initial = _process(first, stage1)
    second, stage1b, _case, _calls, _store = _setup(tmp_path / "again", adapter=adapter, store=store)

    replay = _process(second, stage1b)

    assert initial.disposition == "PAPER_RESULT_RECORDED"
    assert replay.disposition == "NO_TRADE_BY_SYSTEM_NOT_READY"
    assert replay.reason_code == "INTENT_ALREADY_SUBMITTED"
    assert adapter.submit_calls == 1
