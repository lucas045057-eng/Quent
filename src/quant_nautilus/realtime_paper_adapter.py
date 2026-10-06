"""ExecutionAdapter bridge to the existing, local Nautilus Sandbox journal."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator
from uuid import uuid4
from contextlib import contextmanager

from quant_execution.contracts import (
    ExecutionIntentV1,
    ExecutionResultV1,
    PositionSnapshotV1,
    position_from_json,
)
from quant_execution.persistence import ExecutionStore, FencedOwner, ReservationRejected, checkpoint_positions, latest_position_rows
from quant_nautilus.paper import LocalPaper, payload, wall_now
from quant_phase1.contracts import DataStatus, Ticker
from quant_phase9.canonical import canonical_json


class NautilusLocalPaperExecutionAdapter:
    """Accept a canonical public BBO and submit it only to Nautilus Sandbox."""

    _CAPABILITIES = frozenset({
        "MARKET_IOC", "STOP_MARKET_REDUCE_ONLY", "POSITION_SNAPSHOT",
        "LOCAL_RECONCILE", "PAPER_STRATEGY_EXITS", "PAPER_POSITION_MANAGEMENT",
        "V2_CONFIGURED_HOLD","V2_CONFIGURED_LEVERAGE","V2_SHARED_ACCOUNT_MULTI_POSITION",
    })

    def __init__(
        self,
        connection,
        intent: ExecutionIntentV1,
        *,
        instrument,
        quote: Ticker,
        data_source: str,
        quote_max_age_seconds: int,
        native_runtime=None,
    ) -> None:
        if not isinstance(intent, ExecutionIntentV1):
            raise TypeError("ExecutionIntentV1 is required")
        if not isinstance(quote, Ticker):
            raise TypeError("canonical Phase1 Ticker is required")
        if (isinstance(quote_max_age_seconds, bool) or not isinstance(quote_max_age_seconds, int)
                or quote_max_age_seconds <= 0):
            raise ValueError("quote_max_age_seconds must be a positive integer")
        self.connection = connection
        self.native_runtime = native_runtime
        if native_runtime is not None and (native_runtime.connection is not connection or native_runtime.account_id != intent.account_id):
            raise ReservationRejected("LOCAL_RUNTIME_ACCOUNT_BINDING_MISMATCH")
        self.store = ExecutionStore(connection)
        self.intent = intent
        self.instrument = instrument
        self.quote = quote
        self.data_source = data_source
        self.quote_max_age_seconds = quote_max_age_seconds
        self._results: tuple[ExecutionResultV1, ...] = ()
        self._position: PositionSnapshotV1 | None = None
        self._submitted = False
        self.store.assert_ready()
        if intent.mode != "PAPER":
            raise ValueError("local Nautilus Sandbox accepts PAPER mode only")

    @contextmanager
    def _native_operation(self, *, owner=None, verification=False, recover=True):
        if self.native_runtime is not None:
            with self.native_runtime.operation(self.intent.intent_id, instrument=self.instrument,
                    owner=owner, recover_pending_on_start=recover, verification=verification) as runner:
                yield runner
        else:
            runner=LocalPaper(self.connection,self.intent.intent_id,owner=owner,
                owner_lease_seconds=30,recover_pending_on_start=recover,
                acceptance_kind="REAL_PUBLIC_DATA_LOCAL_PAPER_V1",instrument=self.instrument,
                verify_only=verification)
            try:yield runner
            finally:runner.close()

    def capabilities(self) -> frozenset[str]:
        return self._CAPABILITIES

    def _validate_public_quote(self, now: datetime) -> None:
        if (now.tzinfo is None or now.utcoffset() is None
                or now.utcoffset() != timezone.utc.utcoffset(now)):
            raise ValueError("runtime time must be UTC")
        if self.data_source != "REAL_PUBLIC_DATA":
            raise ValueError("REAL_PUBLIC_DATA is required for runtime Paper execution")
        if self.quote.status is not DataStatus.AVAILABLE:
            raise ValueError("STALE_MARKET_DATA")
        if self.quote.symbol != self.intent.core_symbol:
            raise ValueError("canonical quote symbol mismatch")
        times = (self.quote.exchange_timestamp, self.quote.fetched_at, self.quote.processed_at)
        if any(value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value) for value in times):
            raise ValueError("canonical quote timestamps must be UTC")
        if any(value > now for value in times):
            raise ValueError("canonical quote is from the future")
        if any((now - value).total_seconds() > self.quote_max_age_seconds for value in times):
            raise ValueError("STALE_MARKET_DATA")
        if (self.quote.bid_price <= 0 or self.quote.ask_price < self.quote.bid_price
                or self.quote.bid_size <= 0 or self.quote.ask_size <= 0):
            raise ValueError("canonical public BBO is invalid")
        if "fixture" in self.quote.source.lower() or "fixture" in self.quote.exchange.lower():
            raise ValueError("fixture quote cannot enter runtime Paper")
        if str(self.instrument.id) != self.intent.instrument_id:
            raise ValueError("Nautilus instrument identity mismatch")
        if self.instrument.id.venue.value != self.intent.venue:
            raise ValueError("Nautilus instrument venue mismatch")
        canonical = f"{self.instrument.base_currency.code}-{self.instrument.quote_currency.code}-PERP"
        if canonical != self.intent.canonical_symbol or self.instrument.is_inverse:
            raise ValueError("Nautilus canonical instrument mismatch")
        for value, maker in (
            (self.quote.bid_price, self.instrument.make_price),
            (self.quote.ask_price, self.instrument.make_price),
            (self.quote.bid_size, self.instrument.make_qty),
            (self.quote.ask_size, self.instrument.make_qty),
        ):
            if maker(value).as_decimal() != value:
                raise ValueError("Nautilus precision would change the canonical quote")

    def preflight(self, intent: ExecutionIntentV1, now: datetime) -> None:
        if intent != self.intent:
            raise ReservationRejected("INTENT_BINDING_MISMATCH")
        if intent.mode != "PAPER":
            raise ReservationRejected("PAPER_MODE_REQUIRED")
        if not intent.created_at <= now < intent.valid_until:
            raise ReservationRejected("INTENT_EXPIRED")
        self._validate_public_quote(now)

    def _current_account(self, *, now: datetime):
        row = self.connection.execute(
            "SELECT venue,mode,payload,as_of,owner_id,owner_epoch,lease_expires_at "
            "FROM execution_accounts WHERE account_id=%s",
            (self.intent.account_id,),
        ).fetchone()
        if row is None or row[0] != self.intent.venue or row[1] != "PAPER":
            raise ReservationRejected("PAPER_ACCOUNT_UNAVAILABLE")
        if row[2].get("reconciliation_status") != "RECONCILED":
            raise ReservationRejected("PAPER_ACCOUNT_UNRECONCILED")
        if row[3] > now:
            raise ReservationRejected("PAPER_ACCOUNT_FROM_FUTURE")
        return row

    def _latest_positions(self, *, now: datetime) -> tuple[PositionSnapshotV1, ...]:
        rows = latest_position_rows(self.connection,self.intent.account_id)
        if not rows:
            raise ReservationRejected("PAPER_POSITION_UNKNOWN")
        by_symbol: dict[str, list[tuple[datetime, PositionSnapshotV1]]] = {}
        for symbol, as_of, raw_payload in rows:
            position = position_from_json(canonical_json(raw_payload))
            by_symbol.setdefault(symbol, []).append((as_of, position))

        checkpoints=self.native_runtime.verified_checkpoint_positions() if self.native_runtime is not None else None
        if checkpoints is None:checkpoints=checkpoint_positions(self.connection,self.intent.account_id)

        positions: list[PositionSnapshotV1] = []
        for symbol, snapshots in by_symbol.items():
            latest_as_of = max(as_of for as_of, _ in snapshots)
            latest = [position for as_of, position in snapshots if as_of == latest_as_of]
            checkpoint_position=checkpoints.get(symbol)
            if checkpoint_position:
                matching = [position for position in latest if payload(position) == checkpoint_position]
                if matching:
                    positions.append(matching[0])
                    continue
            distinct = {canonical_json(position) for position in latest}
            if len(distinct) != 1:
                raise ReservationRejected("PAPER_POSITION_AMBIGUOUS")
            positions.append(latest[0])

        result = tuple(positions)
        for position in result:
            if (position.account_id != self.intent.account_id or position.venue != self.intent.venue
                    or position.mode != "PAPER" or position.reconciliation_status != "RECONCILED"
                    or position.as_of > now):
                raise ReservationRejected("PAPER_POSITION_UNRECONCILED")
        return result

    def _check_local_journal(self, positions: tuple[PositionSnapshotV1, ...]) -> None:
        if self.native_runtime is not None:
            expected=self.native_runtime.verified_checkpoint_positions()
            if expected is not None:
                for symbol,checkpoint in expected.items():
                    current=next((p for p in positions if p.canonical_symbol==symbol),None)
                    if current is None or payload(current)!=checkpoint:
                        raise ReservationRejected("LOCAL_RECONCILIATION_MISMATCH")
                return
        last_event = None
        for event in self.store.iter_local_events(self.intent.account_id):
            last_event = event
        if last_event is None:
            return
        _, last_kind, last_payload, _, _ = last_event
        if last_kind != "CHECKPOINT":
            raise ReservationRejected("LOCAL_NATIVE_STATE_UNRESOLVED")
        expected=checkpoint_positions(self.connection,self.intent.account_id)
        if not expected and last_payload.get('position'):expected={last_payload['position']['canonical_symbol']:last_payload['position']}
        for symbol,checkpoint in expected.items():
            current=next((p for p in positions if p.canonical_symbol==symbol),None)
            if current is None or payload(current)!=checkpoint:raise ReservationRejected('LOCAL_RECONCILIATION_MISMATCH')

    def reconcile(self) -> tuple[PositionSnapshotV1, ...]:
        now = wall_now()
        account = self._current_account(now=now)
        owner_id, lease_until = account[4], account[6]
        if owner_id is not None and lease_until is not None and lease_until > now:
            raise FencedOwner("local Paper account is owned by another active process")
        pending = self.connection.execute(
            """SELECT count(*) FROM execution_submission_states s
                 JOIN execution_intents i ON i.intent_id=s.intent_id
                WHERE i.account_id=%s AND s.status IN ('SUBMITTING','UNKNOWN')""",
            (self.intent.account_id,),
        ).fetchone()[0]
        if pending:
            raise ReservationRejected("PAPER_SUBMISSION_UNRESOLVED")
        # Completed history is retained; active submission ambiguity was checked above.
        positions = self._latest_positions(now=now)
        self._check_local_journal(positions)
        return positions

    def _owner_for_submission(self, *, now: datetime, runner=None) -> tuple[str, int]:
        state = self.store.submission_state(self.intent.intent_id)
        account = self._current_account(now=now)
        current_owner, current_epoch, lease_until = account[4], account[5], account[6]
        if state == "RESERVED":
            if runner is not None:
                self.store.assert_owner(self.intent.account_id,runner.owner,runner.epoch,now=now)
                owner,epoch=runner.owner,runner.epoch
            else:
                if current_owner is not None and lease_until is not None and lease_until > now:
                    raise FencedOwner("paper execution account already has an owner")
                owner = "local-paper-" + self.intent.intent_id.hex[:40]
                epoch = self.store.acquire_owner(
                    self.intent.account_id, owner, now=now, lease_seconds=30,
                )
            if not self.store.begin_submission(self.intent.intent_id, owner, epoch, now=now):
                raise ReservationRejected("SUBMISSION_NOT_RESERVED")
            return owner, epoch
        if state == "SUBMITTING":
            if current_owner is None or lease_until is None or lease_until <= now:
                raise FencedOwner("submitting paper intent has no live owner")
            return current_owner, current_epoch
        raise ReservationRejected("QUERY_BEFORE_RETRY: command already submitted")

    def submit(self, intent: ExecutionIntentV1) -> None:
        if intent != self.intent:
            raise ReservationRejected("INTENT_BINDING_MISMATCH")
        if self._submitted:
            raise ReservationRejected("QUERY_BEFORE_RETRY: command already submitted")
        now = wall_now()
        self._validate_public_quote(now)
        state=self.store.submission_state(intent.intent_id)
        if state!="RESERVED":raise ReservationRejected("QUERY_BEFORE_RETRY: command already submitted")
        account=self._current_account(now=now)
        if account[4] is not None and account[6] is not None and account[6]>now:
            raise FencedOwner("paper execution account already has an owner")
        # Unique to this operation; never adopt another live process's owner.
        owner="local-paper-"+uuid4().hex
        with self._native_operation(owner=owner,recover=False) as runner:
            # Restoration may itself outlast the execution quote ceiling.
            self.preflight(intent,wall_now())
            runner.command("QUOTE", {
                "at": self.quote.exchange_timestamp.isoformat(),
                "processed_at": self.quote.processed_at.isoformat(),
                "bid": str(self.quote.bid_price),
                "ask": str(self.quote.ask_price),
                "bid_size": str(self.quote.bid_size),
                "ask_size": str(self.quote.ask_size),
            })
            self.preflight(intent,wall_now())
            self._owner_for_submission(now=wall_now(),runner=runner)
            runner.command("SUBMIT", {
                "intent_id": str(intent.intent_id),
                "client_order_id": intent.client_order_id,
            }, preauthorized=True)
            self._results = tuple(runner.session.adapter.results)
            if self._results:
                self._position = runner.session.snapshot()
                if self._position.reconciliation_status != "RECONCILED":
                    raise ReservationRejected("LOCAL_RECONCILIATION_FAILED")
            self._submitted = True

    def preflight_existing_account(self) -> None:
        """Restore the original native journal without emitting a market command.

        Account/positions/checkpoints and unresolved submissions are verified by
        reconcile before recovery. Normal management owns fresh quote commands.
        """
        try:self.reconcile()
        except BaseException:
            if self.native_runtime is not None:self.native_runtime.invalidate()
            raise
        with self._native_operation(verification=True,recover=False) as runner:
            if not runner.restored or runner.unknown:
                raise ReservationRejected("PAPER_NATIVE_RESTORE_UNPROVEN")
            if runner.session.snapshot().reconciliation_status != "RECONCILED":
                raise ReservationRejected("PAPER_NATIVE_RESTORE_UNRECONCILED")

    def manage_position(self, *, now: datetime, emergency=False) -> PositionSnapshotV1:
        # Entry TTL governs new exposure; it must never disable existing exits.
        self._validate_public_quote(now)
        with self._native_operation() as runner:
            self._validate_public_quote(wall_now())
            runner.command("QUOTE", {
                "at": self.quote.exchange_timestamp.isoformat(),
                "processed_at": self.quote.processed_at.isoformat(),
                "bid": str(self.quote.bid_price), "ask": str(self.quote.ask_price),
                "bid_size": str(self.quote.bid_size), "ask_size": str(self.quote.ask_size),
            })
            if emergency:
                runner.command("EMERGENCY_EXIT", {"intent_id":str(self.intent.intent_id)})
            self._position = runner.session.snapshot()
            self._results = tuple(runner.session.adapter.results)
            return self._position

    def cancel(self, intent_id) -> None:
        if intent_id != self.intent.intent_id:
            raise ReservationRejected("INTENT_BINDING_MISMATCH")
        raise ReservationRejected("LOCAL_CANCEL_REQUIRES_RECOVERY_ASSEMBLY")

    def amend(self, intent_id, **constraints) -> None:
        raise NotImplementedError("amend is unsupported by the pinned Local Paper adapter")

    def events(self) -> Iterator[ExecutionResultV1]:
        return iter(self._results)

    def position_snapshot(self, canonical_symbol: str) -> PositionSnapshotV1:
        if canonical_symbol != self.intent.canonical_symbol:
            raise ReservationRejected("POSITION_SYMBOL_MISMATCH")
        if self._position is not None:
            return self._position
        positions = self._latest_positions(now=wall_now())
        for position in positions:
            if position.canonical_symbol == canonical_symbol:
                return position
        raise ReservationRejected("PAPER_POSITION_UNKNOWN")
