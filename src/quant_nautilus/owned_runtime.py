"""Reuse one original Sandbox within one Paper process, never across owners."""
from contextlib import contextmanager
from threading import get_ident
from uuid import uuid4

from quant_execution.persistence import ExecutionStore, ReservationRejected
from quant_nautilus.paper import LocalPaper, wall_now
from quant_phase9.canonical import canonical_sha256


class OwnedLocalPaperRuntime:
    """A disposable native cache, bounded to an unchanged immutable journal.

    The durable journal remains authoritative. Each operation reacquires the
    original account fence; the lease is released between operations. A changed
    journal or pending recovery always destroys the cache and fully replays.
    """

    def __init__(self, connection, account_id):
        if not account_id:
            raise ValueError("native runtime requires its original account")
        self.connection=connection
        self.account_id=account_id
        self.store=ExecutionStore(connection)
        self._thread=get_ident()
        self._busy=False
        self._closed=False
        self._runner=None
        self._signature=None
        self._owner="native-runtime-"+uuid4().hex

    def _journal_signature(self):
        # Sequence and row count are inexpensive index reads, not JSON/native
        # replay. Every cached prefix was validated by the original restore;
        # original immutable audit triggers prohibit modifying that prefix.
        count,seq=self.connection.execute(
            "SELECT count(*),COALESCE(max(seq),0) FROM execution_local_events WHERE account_id=%s",
            (self.account_id,),
        ).fetchone()
        row=self.connection.execute(
            "SELECT event_kind,payload,content_digest,intent_id FROM execution_local_events WHERE account_id=%s AND seq=%s",
            (self.account_id,seq),
        ).fetchone() if count else None
        if row and str(canonical_sha256(row[1]))!=row[2]:
            raise ReservationRejected("LOCAL_EVENT_DIGEST_MISMATCH")
        return count,seq,row[0] if row else None,row[2] if row else None,row[3] if row else None,row[1] if row else None

    def _discard(self):
        runner,self._runner=self._runner,None
        self._signature=None
        if runner is not None:runner.close()

    def invalidate(self):
        if get_ident()!=self._thread or self._busy:
            raise ReservationRejected("LOCAL_RUNTIME_OWNER_CONTEXT_INVALID")
        self._discard()

    def verified_checkpoint_positions(self):
        """Reuse only an unchanged fully verified immutable native prefix."""
        if self._closed or get_ident()!=self._thread or self._busy:
            raise ReservationRejected("LOCAL_RUNTIME_OWNER_CONTEXT_INVALID")
        if self._runner is None:return None
        try:
            signature=self._journal_signature()
            if signature!=self._signature or signature[2]!="CHECKPOINT" or signature[4] is None:
                return None
            runner=self._runner;selected=runner.session.intent.intent_id
            try:
                runner.session.select_intent(signature[4])
                actual=runner.state()
                expected=signature[5]
                if "portfolio" not in expected:actual.pop("portfolio",None)
                if actual!=expected:raise ReservationRejected("LOCAL_RECONCILIATION_MISMATCH")
            finally:runner.session.select_intent(selected)
            positions=expected.get("portfolio") or (expected["position"],)
            return {row["canonical_symbol"]:row for row in positions}
        except BaseException:
            self._discard()
            raise

    @contextmanager
    def operation(self, intent_id, *, instrument, owner=None,
                  recover_pending_on_start=True, verification=False,
                  acceptance_kind="REAL_PUBLIC_DATA_LOCAL_PAPER_V1"):
        if self._closed or get_ident()!=self._thread or self._busy:
            raise ReservationRejected("LOCAL_RUNTIME_OWNER_CONTEXT_INVALID")
        self._busy=True
        lease_owner=owner or self._owner
        epoch=None
        try:
            intent=self.store.intent(intent_id)
            if intent.account_id!=self.account_id or intent.mode!="PAPER":
                raise ReservationRejected("LOCAL_RUNTIME_ACCOUNT_BINDING_MISMATCH")
            epoch=self.store.acquire_owner(self.account_id,lease_owner,now=wall_now(),lease_seconds=30)
            signature=self._journal_signature()
            pending=self.connection.execute(
                "SELECT count(*) FROM execution_submission_states s JOIN execution_intents i USING(intent_id) "
                "WHERE i.account_id=%s AND s.status IN ('SUBMITTING','UNKNOWN')",(self.account_id,),
            ).fetchone()[0]
            if pending and not recover_pending_on_start:
                raise ReservationRejected("PAPER_SUBMISSION_UNRESOLVED")
            if self._runner is not None and (signature!=self._signature or (pending and recover_pending_on_start)):
                self._discard()
            if self._runner is None:
                runner=LocalPaper(self.connection,intent_id,owner=lease_owner,
                    owner_lease_seconds=30,recover_pending_on_start=recover_pending_on_start,
                    acceptance_kind=acceptance_kind,instrument=instrument,verify_only=verification)
                self._runner=runner
            else:
                runner=self._runner
                runner.owner,runner.epoch=lease_owner,epoch
                runner.verify_only=verification
                existing=runner.session.adapters.get(intent_id)
                if existing is None:
                    if verification:raise ReservationRejected("PAPER_NATIVE_RESTORE_UNPROVEN")
                    if instrument is None or str(instrument.id)!=intent.instrument_id:
                        raise ReservationRejected("INSTRUMENT_BINDING_MISMATCH")
                    runner.session.add_intent(instrument,intent)
                    runner.public_instruments[intent_id]=instrument
                    runner.acceptance_kinds[intent_id]=acceptance_kind
                    runner.intent=intent;runner.session.select_intent(intent_id)
                    with self.connection.transaction():
                        self.store.assert_owner(self.account_id,lease_owner,epoch,now=wall_now())
                        runner._journal("CONFIG",runner.config())
                else:
                    if existing.intent!=intent:
                        raise ReservationRejected("LOCAL_CONFIG_MISMATCH")
                    if instrument is not None and instrument.to_dict(instrument)!=runner.session.instruments[intent_id].to_dict(runner.session.instruments[intent_id]):
                        raise ReservationRejected("INSTRUMENT_BINDING_MISMATCH")
                    runner.intent=intent;runner.session.select_intent(intent_id)
                if verification:
                    if signature[2]!="CHECKPOINT" or not existing or not existing.results:
                        raise ReservationRejected("PAPER_NATIVE_RESTORE_UNPROVEN")
                    runner.restored=True
            yield runner
            self.store.assert_owner(self.account_id,runner.owner,runner.epoch,now=wall_now())
            self._signature=self._journal_signature()
        except BaseException:
            self._discard()
            raise
        finally:
            if epoch is not None:self.store.release_owner(self.account_id,lease_owner,epoch)
            self._busy=False

    def prepare(self, intent_id, *, instrument):
        """Restore before the caller captures its strict execution quote."""
        if self._runner is not None and self.verified_checkpoint_positions() is not None:
            return
        with self.operation(intent_id,instrument=instrument):
            pass

    def close(self):
        if get_ident()!=self._thread or self._busy:raise ReservationRejected("LOCAL_RUNTIME_OPERATION_ACTIVE")
        self._discard()
        self._closed=True
