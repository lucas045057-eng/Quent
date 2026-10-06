"""Offline concurrency fixtures; no real candidate/trade evidence."""
import asyncio
import threading
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
import pytest

@pytest.mark.asyncio
async def test_stage1_owns_one_worker_despite_busy_default_pool(monkeypatch):
    from quant_phase1.entrypoints import engine
    from quant_phase1.config import Settings
    ids=[]
    def work(*args,**kwargs):
        ids.append(threading.get_ident())
        return {"symbols":478}
    class Admission:
        @asynccontextmanager
        async def admit(self,request):yield
    monkeypatch.setattr(engine,"run_database_cycle",work)
    release=threading.Event()
    blockers=[asyncio.create_task(asyncio.to_thread(release.wait,2)) for _ in range(8)]
    try:
        with ThreadPoolExecutor(max_workers=1) as dedicated:
            for _ in range(8):
                result=await engine._run_admitted_database_cycle(
                    Settings.from_env({"TRADING_MODE":"paper"}),object(),None,Admission(),executor=dedicated)
                assert result=={"symbols":478}
            assert len(set(ids))==1
    finally:
        release.set();await asyncio.gather(*blockers)

@pytest.mark.asyncio
async def test_owned_worker_cancellation_keeps_admission_until_work_done(monkeypatch):
    from quant_phase1.entrypoints import engine
    from quant_phase1.config import Settings
    entered=threading.Event();release=threading.Event();finished=threading.Event();held=[]
    def work(*args,**kwargs):
        entered.set();assert release.wait(2);finished.set();return {"symbols":478}
    class Admission:
        @asynccontextmanager
        async def admit(self,request):
            held.append(True)
            try:yield
            finally:assert finished.is_set();held.clear()
    monkeypatch.setattr(engine,"run_database_cycle",work)
    with ThreadPoolExecutor(max_workers=1) as dedicated:
        task=asyncio.create_task(engine._run_admitted_database_cycle(
            Settings.from_env({"TRADING_MODE":"paper"}),object(),None,Admission(),executor=dedicated))
        assert await asyncio.to_thread(entered.wait,1)
        task.cancel();await asyncio.sleep(.01)
        assert held and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert not held

@pytest.mark.asyncio
async def test_engine_lifecycle_supplies_and_closes_dedicated_executor(monkeypatch):
    from quant_phase1.entrypoints import engine
    owned=[]
    async def service(*args,stage1_executor,**kwargs):
        owned.append(stage1_executor)
        ids=list(stage1_executor.map(lambda _:threading.get_ident(),range(12)))
        assert len(set(ids))==1
    monkeypatch.setattr(engine,"_run_service_owned",service)
    await engine.run_service()
    with pytest.raises(RuntimeError):owned[0].submit(lambda:None)

@pytest.mark.asyncio
async def test_worker_reclaims_only_after_batch_frame_released(monkeypatch):
    from quant_phase1.entrypoints import engine
    from quant_phase1.config import Settings
    import weakref
    refs=[];events=[]
    class Batch:pass
    def work(*args,**kwargs):
        batch=Batch();refs.append(weakref.ref(batch))
        events.append(('work',threading.get_ident()))
        return {'symbols':478,'available':0,'persisted':478}
    def reclaim():
        assert refs and refs[-1]() is None
        events.append(('reclaim',threading.get_ident()))
    class Admission:
        @asynccontextmanager
        async def admit(self,request):
            yield
            assert events[-1][0]=='reclaim'
    monkeypatch.setattr(engine,'run_database_cycle',work)
    monkeypatch.setattr(engine,'_release_unused_allocator_pages',reclaim)
    with ThreadPoolExecutor(max_workers=1) as dedicated:
        result=await engine._run_admitted_database_cycle(
            Settings.from_env({'TRADING_MODE':'paper'}),object(),None,Admission(),executor=dedicated)
    assert result=={'symbols':478,'available':0,'persisted':478}
    assert [x[0] for x in events]==['work','reclaim']
    assert events[0][1]==events[1][1]

@pytest.mark.asyncio
async def test_worker_reclaims_on_failure_and_preserves_original_exception(monkeypatch):
    from quant_phase1.entrypoints import engine
    from quant_phase1.config import Settings
    events=[]
    def work(*args,**kwargs):raise ValueError('fixture read failure')
    class Admission:
        @asynccontextmanager
        async def admit(self,request):
            try:yield
            finally:assert events==['reclaim']
    monkeypatch.setattr(engine,'run_database_cycle',work)
    monkeypatch.setattr(engine,'_release_unused_allocator_pages',lambda:events.append('reclaim'))
    with ThreadPoolExecutor(max_workers=1) as dedicated:
        with pytest.raises(ValueError,match='fixture read failure'):
            await engine._run_admitted_database_cycle(
                Settings.from_env({'TRADING_MODE':'paper'}),object(),None,Admission(),executor=dedicated)

@pytest.mark.asyncio
async def test_cancelled_worker_retains_admission_through_page_reclaim(monkeypatch):
    from quant_phase1.entrypoints import engine
    from quant_phase1.config import Settings
    entered=threading.Event();release=threading.Event();held=[]
    def reclaim():entered.set();assert release.wait(2)
    class Admission:
        @asynccontextmanager
        async def admit(self,request):
            held.append(True)
            try:yield
            finally:held.clear()
    monkeypatch.setattr(engine,'run_database_cycle',lambda *a,**k:{'symbols':478})
    monkeypatch.setattr(engine,'_release_unused_allocator_pages',reclaim)
    with ThreadPoolExecutor(max_workers=1) as dedicated:
        task=asyncio.create_task(engine._run_admitted_database_cycle(
            Settings.from_env({'TRADING_MODE':'paper'}),object(),None,Admission(),executor=dedicated))
        assert await asyncio.to_thread(entered.wait,1)
        task.cancel();await asyncio.sleep(.01)
        assert held and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert not held

@pytest.mark.parametrize('trim', [None, lambda _:0, lambda _:1])
def test_allocator_release_support_and_noop_are_safe(monkeypatch,trim):
    from quant_phase1.entrypoints import engine
    monkeypatch.setattr(engine,'_allocator_trim',trim)
    engine._release_unused_allocator_pages()

def test_allocator_release_failure_does_not_replace_cycle_result(monkeypatch):
    from quant_phase1.entrypoints import engine
    def broken(_):raise OSError('fixture unsupported runtime')
    monkeypatch.setattr(engine,'_allocator_trim',broken)
    engine._release_unused_allocator_pages()

@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_release_admission_while_reclaim_running(monkeypatch):
    from quant_phase1.entrypoints import engine
    from quant_phase1.config import Settings
    entered=threading.Event();release=threading.Event();finished=threading.Event();held=[]
    def reclaim():
        entered.set();assert release.wait(2);finished.set()
    class Admission:
        @asynccontextmanager
        async def admit(self,request):
            held.append(True)
            try:yield
            finally:held.clear()
    monkeypatch.setattr(engine,'run_database_cycle',lambda *a,**k:{'symbols':478})
    monkeypatch.setattr(engine,'_release_unused_allocator_pages',reclaim)
    with ThreadPoolExecutor(max_workers=1) as dedicated:
        task=asyncio.create_task(engine._run_admitted_database_cycle(
            Settings.from_env({'TRADING_MODE':'paper'}),object(),None,Admission(),executor=dedicated))
        try:
            assert await asyncio.to_thread(entered.wait,1)
            task.cancel();await asyncio.sleep(.01)
            task.cancel();await asyncio.sleep(.01)
            assert held and not task.done() and not finished.is_set()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):await task
        assert finished.is_set() and not held
