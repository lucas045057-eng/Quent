from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from quant_phase1.contracts import DataStatus
from quant_phase1.db import apply_migrations
from quant_phase1.stage1 import Stage1Result
from quant_phase9.contracts import EvaluationIdentityV1
from quant_phase9.intake import (
    AlreadyAcknowledgedError,
    EventIdentityConflictError,
    EventNotClaimedError,
    LeaseExpiredError,
    LeaseOwnershipError,
    Phase9OutboxWriter,
    RetryableErrorCodeV1,
    ack,
    admit_event,
    build_stage1_candidate_event,
    claim_pending,
    release_for_retry,
)


NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def database():
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is required for outbox recovery integration tests")

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(dsn)
    assert info.get("host") in {"127.0.0.1", "localhost", "::1"}, "test DSN must use loopback"
    assert info.get("dbname") == "quant_phase9_test", "test requires disposable database"
    schema = f"phase9_outbox_test_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    connection = psycopg.connect(dsn, options=f"-c search_path={schema},public")
    try:
        apply_migrations(connection)
        connection.commit()
        yield dsn, schema, connection
    finally:
        connection.close()
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def _event(
    result_id: int,
    *,
    status: DataStatus = DataStatus.AVAILABLE,
    valid_until: datetime | None = NOW + timedelta(hours=1),
    market: str = "USDT_PERPETUAL",
    in_scope: bool = True,
):
    result = Stage1Result(
        symbol=f"T{result_id}USDT",
        category="A",
        reason="fixture result",
        status=status,
        inputs_used=("price",),
        indicators={"atr": Decimal("1.25")},
        structure="BULLISH",
        reason_codes=("STRUCTURE_ALIGNED",),
        timestamp=NOW,
    )
    return build_stage1_candidate_event(
        screening_result_id=result_id,
        run_id=result_id + 10_000,
        screening_result=result,
        market=market,
        instrument_scope={
            "category": "USDT-FUTURES" if market == "USDT_PERPETUAL" else "SPOT",
            "quote_coin": "USDT",
            "contract_type": "perpetual" if market == "USDT_PERPETUAL" else "spot",
            "status": "online",
            "in_scope": "true" if in_scope else "false",
        },
        candidate_created_at=NOW,
        candidate_valid_until=valid_until,
        stage1_policy_version="phase1-basic-v1",
        source_as_of=NOW,
    )


def _identity(event, *, timeframe: str = "15m"):
    return EvaluationIdentityV1(
        stage1_candidate_id=event.stage1_candidate_id,
        market=event.market,
        symbol=event.symbol,
        timeframe=timeframe,
        evaluation_window_start=NOW - timedelta(minutes=15),
        evaluation_window_end=NOW,
        policy_generation="phase9-policy-v1",
        material_change_generation="0",
    )


def _seed(connection, event) -> None:
    Phase9OutboxWriter().emit(connection, event=event)


def _open(dsn: str, schema: str):
    import psycopg

    return psycopg.connect(dsn, options=f"-c search_path={schema},public")


def test_admission_is_idempotent_and_terminal_outbox_states_are_never_resurrected(database):
    _, _, connection = database
    event = _event(501)
    _seed(connection, event)

    assert admit_event(connection, event=event, identity=_identity(event), now=NOW).value == "ADMITTED"
    connection.commit()
    assert admit_event(connection, event=event, identity=_identity(event), now=NOW).value == "ALREADY_ADMITTED"
    connection.commit()
    assert connection.execute("SELECT count(*) FROM phase9_evaluations").fetchone()[0] == 1

    connection.execute(
        """UPDATE outbox_events SET phase9_state='ACKNOWLEDGED', attempt_count=1,
           attempt_limit=3, acknowledged_at=%s WHERE event_id=%s""",
        (NOW, event.event_id),
    )
    connection.commit()
    assert admit_event(connection, event=event, identity=_identity(event), now=NOW).value == "ALREADY_ADMITTED"
    assert connection.execute(
        "SELECT phase9_state FROM outbox_events WHERE event_id=%s", (event.event_id,)
    ).fetchone()[0] == "ACKNOWLEDGED"

    connection.execute(
        """UPDATE outbox_events SET phase9_state='DEAD_LETTER', attempt_count=3,
           acknowledged_at=NULL, terminal_reason='ATTEMPT_LIMIT_EXHAUSTED'
           WHERE event_id=%s""",
        (event.event_id,),
    )
    connection.commit()
    assert admit_event(connection, event=event, identity=_identity(event), now=NOW).value == "ALREADY_ADMITTED"
    assert connection.execute(
        "SELECT phase9_state FROM outbox_events WHERE event_id=%s", (event.event_id,)
    ).fetchone()[0] == "DEAD_LETTER"
    assert connection.execute("SELECT count(*) FROM phase9_evaluations").fetchone()[0] == 1


def test_invalid_conflicting_expired_and_empty_timeframe_inputs_fail_closed(database):
    _, _, connection = database
    event = _event(502)
    _seed(connection, event)
    invalid = replace(event, canonical_payload_digest="b" * 64)
    assert admit_event(connection, event=invalid, identity=_identity(event), now=NOW).value == "INVALID_EVENT"
    assert connection.execute("SELECT count(*) FROM phase9_evaluations").fetchone()[0] == 0
    with pytest.raises(ValueError, match="timeframe"):
        _identity(event, timeframe="")
    assert admit_event(connection, event=event, identity=object(), now=NOW).value == "INVALID_EVENT"

    expired = _event(503, valid_until=NOW)
    _seed(connection, expired)
    assert admit_event(connection, event=expired, identity=_identity(expired), now=NOW).value == "EVENT_EXPIRED"
    assert connection.execute(
        "SELECT evaluation_state, intake_disposition, reason_code "
        "FROM phase9_evaluations WHERE stage1_candidate_id=%s",
        (expired.stage1_candidate_id,),
    ).fetchone() == ("CANCELLED", "EXPIRED", "STAGE1_EXPIRED")

    connection.execute(
        "UPDATE outbox_events SET payload=jsonb_set(payload, '{canonical_payload_digest}', to_jsonb(%s::text)) WHERE event_id=%s",
        ("c" * 64, event.event_id),
    )
    connection.commit()
    with pytest.raises(EventIdentityConflictError):
        admit_event(connection, event=event, identity=_identity(event), now=NOW)


def test_one_candidate_is_durably_admitted_once_per_configured_timeframe(database):
    _, _, connection = database
    event = _event(511)
    _seed(connection, event)

    assert admit_event(connection, event=event, identity=_identity(event, timeframe="15m"), now=NOW).value == "ADMITTED"
    assert admit_event(connection, event=event, identity=_identity(event, timeframe="1H"), now=NOW).value == "ADMITTED"
    assert admit_event(connection, event=event, identity=_identity(event, timeframe="15m"), now=NOW).value == "ALREADY_ADMITTED"
    connection.commit()

    assert connection.execute(
        "SELECT timeframe FROM phase9_evaluations ORDER BY timeframe"
    ).fetchall() == [("15m",), ("1H",)]
    assert connection.execute(
        "SELECT published_at FROM outbox_events WHERE event_id=%s", (event.event_id,)
    ).fetchone()[0] is None


def test_missing_ttl_defers_and_nonavailable_or_out_of_scope_candidates_are_durably_rejected(database):
    _, _, connection = database
    no_ttl = _event(504, valid_until=None)
    _seed(connection, no_ttl)
    assert admit_event(connection, event=no_ttl, identity=_identity(no_ttl), now=NOW).value == "ADMITTED"

    unavailable = _event(505, status=DataStatus.STALE)
    _seed(connection, unavailable)
    assert admit_event(connection, event=unavailable, identity=_identity(unavailable), now=NOW).value == "ADMITTED"

    out_of_scope = _event(506, market="UNSUPPORTED", in_scope=False)
    _seed(connection, out_of_scope)
    assert admit_event(
        connection, event=out_of_scope, identity=_identity(out_of_scope), now=NOW
    ).value == "ADMITTED"
    connection.commit()

    rows = connection.execute(
        """SELECT stage1_candidate_id, evaluation_state, intake_disposition, reason_code
           FROM phase9_evaluations ORDER BY stage1_candidate_id"""
    ).fetchall()
    assert rows == [
        (504, "QUEUED", "DEFERRED", "INTAKE_TTL_NOT_CONFIGURED"),
        (505, "CANCELLED", "REJECTED", "STAGE1_STATUS_NOT_AVAILABLE"),
        (506, "CANCELLED", "REJECTED", "INSTRUMENT_OUT_OF_SCOPE"),
    ]


def test_claim_retry_attempts_are_bounded_and_third_failure_dead_letters(database):
    _, _, connection = database
    event = _event(507)
    _seed(connection, event)
    connection.commit()

    lease_until = NOW + timedelta(seconds=20)
    assert [item.event_id for item in claim_pending(
        connection, consumer_name="worker-a", limit=1, now=NOW, lease_until=lease_until
    )] == [event.event_id]
    connection.commit()
    assert connection.execute(
        "SELECT attempt_count, attempt_limit, phase9_state FROM outbox_events WHERE event_id=%s",
        (event.event_id,),
    ).fetchone() == (1, 3, "LEASED")

    with pytest.raises(LeaseOwnershipError):
        ack(connection, event_id=event.event_id, consumer_name="worker-b", now=NOW)
    with pytest.raises(LeaseExpiredError):
        ack(connection, event_id=event.event_id, consumer_name="worker-a", now=lease_until)
    with pytest.raises(ValueError):
        release_for_retry(
            connection, event_id=event.event_id, consumer_name="worker-a",
            retry_at=NOW, error_code=RetryableErrorCodeV1.DB_SERIALIZATION_CONFLICT, now=NOW,
        )
    connection.rollback()

    retry_at = NOW + timedelta(seconds=30)
    release_for_retry(
        connection, event_id=event.event_id, consumer_name="worker-a", retry_at=retry_at,
        error_code=RetryableErrorCodeV1.DB_SERIALIZATION_CONFLICT, now=NOW,
    )
    connection.commit()
    for expected_attempt in (2, 3):
        claimed = claim_pending(
            connection, consumer_name=f"worker-{expected_attempt}", limit=1,
            now=retry_at, lease_until=retry_at + timedelta(seconds=20),
        )
        assert [item.event_id for item in claimed] == [event.event_id]
        connection.commit()
        assert connection.execute(
            "SELECT attempt_count FROM outbox_events WHERE event_id=%s", (event.event_id,)
        ).fetchone()[0] == expected_attempt
        release_for_retry(
            connection, event_id=event.event_id, consumer_name=f"worker-{expected_attempt}",
            retry_at=retry_at + timedelta(seconds=30),
            error_code=RetryableErrorCodeV1.AI_GATEWAY_UNAVAILABLE, now=retry_at,
        )
        connection.commit()
        retry_at += timedelta(seconds=30)

    assert connection.execute(
        "SELECT phase9_state, attempt_count, attempt_limit, terminal_reason FROM outbox_events WHERE event_id=%s",
        (event.event_id,),
    ).fetchone() == ("DEAD_LETTER", 3, 3, "ATTEMPT_LIMIT_EXHAUSTED")
    assert claim_pending(
        connection, consumer_name="worker-4", limit=1, now=retry_at,
        lease_until=retry_at + timedelta(seconds=20),
    ) == ()
    with pytest.raises(EventNotClaimedError):
        ack(connection, event_id=event.event_id, consumer_name="worker-4", now=retry_at)
    with pytest.raises(EventNotClaimedError):
        release_for_retry(
            connection, event_id=event.event_id, consumer_name="worker-4",
            retry_at=retry_at + timedelta(seconds=1),
            error_code=RetryableErrorCodeV1.AI_GATEWAY_TIMEOUT, now=retry_at,
        )


def test_crash_restart_reclaims_only_expired_lease_and_ack_is_idempotent(database):
    dsn, schema, connection = database
    event = _event(508)
    _seed(connection, event)
    connection.commit()

    first_expiry = NOW + timedelta(seconds=10)
    assert len(claim_pending(
        connection, consumer_name="old-worker", limit=1, now=NOW, lease_until=first_expiry
    )) == 1
    connection.commit()
    connection.close()

    with _open(dsn, schema) as restarted:
        assert claim_pending(
            restarted, consumer_name="new-worker", limit=1,
            now=NOW + timedelta(seconds=5), lease_until=NOW + timedelta(seconds=30),
        ) == ()
        restarted.commit()
        reclaimed = claim_pending(
            restarted, consumer_name="new-worker", limit=1,
            now=first_expiry, lease_until=NOW + timedelta(seconds=40),
        )
        assert [item.event_id for item in reclaimed] == [event.event_id]
        restarted.commit()
        assert restarted.execute(
            "SELECT attempt_count, lease_owner FROM outbox_events WHERE event_id=%s", (event.event_id,)
        ).fetchone() == (2, "new-worker")
        with pytest.raises(LeaseOwnershipError):
            ack(restarted, event_id=event.event_id, consumer_name="old-worker", now=first_expiry)
        ack(restarted, event_id=event.event_id, consumer_name="new-worker", now=first_expiry)
        restarted.commit()
        ack(restarted, event_id=event.event_id, consumer_name="any-worker", now=first_expiry)
        restarted.commit()
        assert restarted.execute(
            "SELECT phase9_state, acknowledged_at FROM outbox_events WHERE event_id=%s", (event.event_id,)
        ).fetchone() == ("ACKNOWLEDGED", first_expiry)


def test_claim_validates_bounds_and_functions_leave_commit_to_caller(database):
    dsn, schema, connection = database
    event = _event(509)
    _seed(connection, event)
    connection.commit()

    with pytest.raises(ValueError):
        claim_pending(connection, consumer_name="worker", limit=0, now=NOW, lease_until=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError):
        claim_pending(connection, consumer_name="worker", limit=1, now=NOW, lease_until=NOW)

    assert len(claim_pending(
        connection, consumer_name="worker", limit=1, now=NOW,
        lease_until=NOW + timedelta(seconds=10),
    )) == 1
    with _open(dsn, schema) as observer:
        assert observer.execute(
            "SELECT phase9_state FROM outbox_events WHERE event_id=%s", (event.event_id,)
        ).fetchone()[0] == "PENDING"
    connection.rollback()

    connection.close()


def test_retry_rejects_nonretryable_code_and_acknowledged_event(database):
    _, _, connection = database
    event = _event(510)
    _seed(connection, event)
    connection.commit()
    expiry = NOW + timedelta(seconds=10)
    claim_pending(connection, consumer_name="worker", limit=1, now=NOW, lease_until=expiry)
    connection.commit()

    with pytest.raises(ValueError):
        release_for_retry(
            connection, event_id=event.event_id, consumer_name="worker",
            retry_at=NOW + timedelta(seconds=1), error_code="PERMANENT_PARSER_ERROR", now=NOW,
        )
    ack(connection, event_id=event.event_id, consumer_name="worker", now=NOW)
    connection.commit()
    with pytest.raises(AlreadyAcknowledgedError):
        release_for_retry(
            connection, event_id=event.event_id, consumer_name="worker",
            retry_at=NOW + timedelta(seconds=2),
            error_code=RetryableErrorCodeV1.AI_GATEWAY_TIMEOUT, now=NOW,
        )


def test_ack_and_retry_raise_exact_errors_for_missing_unclaimed_wrong_owner_and_expired_leases(database):
    _, _, connection = database
    event = _event(513)
    _seed(connection, event)
    connection.commit()
    with pytest.raises(EventNotClaimedError):
        ack(connection, event_id="f" * 64, consumer_name="worker", now=NOW)
    with pytest.raises(EventNotClaimedError):
        ack(connection, event_id=event.event_id, consumer_name="worker", now=NOW)
    with pytest.raises(EventNotClaimedError):
        release_for_retry(
            connection, event_id=event.event_id, consumer_name="worker",
            retry_at=NOW + timedelta(seconds=2),
            error_code=RetryableErrorCodeV1.DB_SERIALIZATION_CONFLICT, now=NOW,
        )

    expiry = NOW + timedelta(seconds=10)
    claim_pending(connection, consumer_name="owner-a", limit=1, now=NOW, lease_until=expiry)
    with pytest.raises(LeaseOwnershipError):
        release_for_retry(
            connection, event_id=event.event_id, consumer_name="owner-b",
            retry_at=NOW + timedelta(seconds=20),
            error_code=RetryableErrorCodeV1.DB_SERIALIZATION_CONFLICT, now=NOW,
        )
    with pytest.raises(LeaseExpiredError):
        release_for_retry(
            connection, event_id=event.event_id, consumer_name="owner-a",
            retry_at=expiry + timedelta(seconds=1),
            error_code=RetryableErrorCodeV1.DB_SERIALIZATION_CONFLICT, now=expiry,
        )
    connection.rollback()


def test_concurrent_claims_use_skip_locked_and_preserve_deterministic_order(database):
    dsn, schema, first_connection = database
    earlier = _event(514)
    later = _event(515)
    _seed(first_connection, earlier)
    _seed(first_connection, later)
    first_connection.commit()

    first_page = claim_pending(
        first_connection, consumer_name="worker-a", limit=1,
        now=NOW, lease_until=NOW + timedelta(seconds=20),
    )
    assert len(first_page) == 1
    with _open(dsn, schema) as second_connection:
        second_page = claim_pending(
            second_connection, consumer_name="worker-b", limit=1,
            now=NOW, lease_until=NOW + timedelta(seconds=20),
        )
        assert len(second_page) == 1
        assert second_page[0].event_id != first_page[0].event_id
    assert first_page[0].event_id == min(earlier.event_id, later.event_id)
    assert second_page[0].event_id == max(earlier.event_id, later.event_id)
