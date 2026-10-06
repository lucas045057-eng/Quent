import os
from decimal import Decimal as D
from quant_nautilus.paper_acceptance import run_acceptance


def test_real_paper_acceptance_has_actual_restarts_memory_and_honest_smoke_label(tmp_path):
    report=run_acceptance(os.environ['TEST_POSTGRES_DSN'],tmp_path,duration_seconds=1,formal=False)
    assert report['checks_passed'] is True
    assert report['formal_acceptance_pass'] is False
    assert report['acceptance_kind']=='FIXTURE_DRIVEN_ACCEPTANCE'
    assert len(report['cases'])==2
    for row in report['cases']:
        assert row['first_pid'] != row['second_pid']
        assert row['initial_digest']==row['restored_digest']
        assert row['funding_payment_count']==1 and row['result_count']>0
        assert row['peak_rss_bytes']>0 and row['rss_bytes']>0
        assert D(row['funding_cash'])!=0
