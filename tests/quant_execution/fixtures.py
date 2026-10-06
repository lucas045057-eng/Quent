from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import UUID

from quant_phase9.contracts import (
    DecisionCandidateV1, DecisionDirectionBiasV1, ConfidenceBandV1, PatternMatchStatusV1,
)

NOW = datetime(2026, 9, 28, 0, 15, tzinfo=timezone.utc)


def candidate(now=NOW, *, symbol='BTCUSDT', short=False):
    return DecisionCandidateV1(
        decision_id=UUID('10000000-0000-0000-0000-000000000001'),
        evaluation_id=UUID('20000000-0000-0000-0000-000000000001'),
        stage1_candidate_id=91, symbol=symbol, market='USDT_PERPETUAL', timeframe='1H',
        created_at=now, valid_until=now + timedelta(minutes=20), eligible=True,
        direction_bias=DecisionDirectionBiasV1.BEARISH if short else DecisionDirectionBiasV1.BULLISH,
        confidence_band=ConfidenceBandV1.HIGH, matched_pattern='TREND_CONTINUATION',
        pattern_status=PatternMatchStatusV1.MATCHED, supporting_evidence_ids=(),
        conflicting_evidence_ids=(), degraded_evidence_ids=(), missing_evidence=(), veto_reasons=(),
        jev_review_id=None, reason_codes=('FIXTURE_DRIVEN_ACCEPTANCE',), short_summary='fixture',
        input_snapshot_hash='e' * 64, evidence_schema_version='PHASE9_EVIDENCE_CHAIN_V1',
        pattern_policy_version='1.0.0', freshness_policy_version='1.0.0',
        decision_policy_version='1.0.0', ttl_policy_version='1.0.0',
        prompt_version='NONE', code_version='b' * 40, supersedes_decision_id=None,
    )


def risk_inputs(now=NOW):
    from quant_execution.contracts import AccountSnapshotV1, DecisionEnvelopeV1, InstrumentSpecV1, QuoteV1
    from quant_execution.risk import RiskPolicyV1

    return dict(
        envelope=DecisionEnvelopeV1(candidate(now), 'ACTIVE', 'd' * 64, 'e' * 64),
        instrument=InstrumentSpecV1('BTCUSDT', 'BTC-USDT-PERP', 'LOCAL', 'BTCUSDT-PERP.LOCAL',
            'USDT', D('0.001'), D('0.001'), D('100'), D('0.1'), D('5'), True),
        quote=QuoteV1('BTC-USDT-PERP', D('49999'), D('50001'), now, 'AVAILABLE'),
        account=AccountSnapshotV1('fixture-account', 'LOCAL', 'PAPER', D('10000'),
            D('10000'), D('0'), D('0'), 0, now, 'RECONCILED'),
        policy=RiskPolicyV1('risk-v1', D('1000'), D('1000'), D('1'), D('10'),
            D('2000'), D('20'), 2, 5, 5, D('5'), D('10'), 60,
            ('1.0.0',), ('b' * 40,)),
        stop_price=D('49901'), now=now,
    )
