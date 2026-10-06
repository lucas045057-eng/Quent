from dataclasses import replace
from datetime import datetime,timezone,timedelta
from decimal import Decimal as D
import pytest
from tests.quant_nautilus.test_paper_restart import paper_db
from quant_execution.contracts import AccountSnapshotV1,PositionSnapshotV1
from quant_execution.persistence import ExecutionStore
from quant_realtime_paper.assembly import reconcile_initial_paper_account

def funded(conn,change=None):
    at=datetime.now(timezone.utc)-timedelta(minutes=20)
    account=AccountSnapshotV1('funded','BITGET_PAPER','PAPER',D(1000),D(1000),D(0),D(0),0,at,'RECONCILED')
    store=ExecutionStore(conn);store.record_account(account)
    for symbol in ('BTC-USDT-PERP','ETH-USDT-PERP'):
        if change=='position_missing' and symbol=='BTC-USDT-PERP':continue
        state='UNKNOWN' if change=='unknown_position' else 'RECONCILED'
        store.record_position(PositionSnapshotV1('funded','BITGET_PAPER',symbol,'PAPER','FLAT',D(0),'BASE',None,D(1000),D(1000),D(0),None,None,D(0),D(0),D(0),'FLAT',(),(),state,at,'EXPLICIT_OPERATOR_INITIAL_FUNDING_V2'))
    profile={'account_id':'funded','initial_funding':{'basis':'EXPLICIT_HUMAN_PAPER_FUNDING','amount_usdt':'1000','funded_at':at.isoformat(),'canonical_symbols':['BTC-USDT-PERP','ETH-USDT-PERP']}}
    return account,profile

def test_first_risk_reconciles_real_locked_empty_account_without_changing_balances(paper_db):
    conn,_,_=paper_db;account,profile=funded(conn);now=datetime.now(timezone.utc)
    current=reconcile_initial_paper_account(conn,profile,now=now)
    assert current==replace(account,as_of=now)
    later=reconcile_initial_paper_account(conn,profile,now=now+timedelta(seconds=10))
    assert later.equity==D(1000) and later.as_of==now+timedelta(seconds=10)
    assert conn.execute('SELECT count(*) FROM execution_local_events').fetchone()[0]==0

@pytest.mark.parametrize('change',['funding_missing','amount_mismatch','future','unknown_position','account_changed','leased','position_missing','local_event'])
def test_unverified_initial_account_never_gets_a_new_clock(paper_db,change):
    conn,_,_=paper_db;account,profile=funded(conn,change);now=datetime.now(timezone.utc)
    if change=='funding_missing':profile.pop('initial_funding')
    elif change=='amount_mismatch':profile['initial_funding']['amount_usdt']='2000'
    elif change=='future':profile['initial_funding']['funded_at']=(now+timedelta(seconds=1)).isoformat()
    elif change=='account_changed':ExecutionStore(conn).record_account(replace(account,available_balance=D(999)))
    elif change=='leased':conn.execute('UPDATE execution_accounts SET lease_expires_at=%s', (now+timedelta(seconds=30),))
    elif change=='local_event':ExecutionStore(conn).append_local_event(account_id='funded',event_key='unknown',kind='CONFIG',payload={'unresolved':True})
    with pytest.raises(ValueError):reconcile_initial_paper_account(conn,profile,now=now)
    assert conn.execute('SELECT as_of FROM execution_accounts').fetchone()[0]==account.as_of
