from datetime import datetime, timezone
from decimal import Decimal as D
import json
import pytest
from quant_execution.contracts import AccountSnapshotV1

def account():
    return AccountSnapshotV1("account","BINANCE","PAPER",D("10000"),D("10000"),D("0"),D("0"),0,
        datetime(2026,10,3,tzinfo=timezone.utc),"RECONCILED")

def test_default_risk_policy_matches_existing_defaults():
    from quant_execution.risk_config import RiskConfigV2, resolve_risk_policy
    p=resolve_risk_policy(RiskConfigV2(),account(),code_version="a"*40,decision_versions=("2.0.0",))
    assert (p.max_risk,p.max_notional,p.max_exposure,p.max_leverage)==(D("25"),D("1000"),D("1000"),D("1"))

def test_updated_ratios_remove_legacy_hidden_caps():
    from quant_execution.risk_config import RiskConfigV2, resolve_risk_policy
    c=RiskConfigV2(risk_per_trade_equity_ratio="0.005",max_reserved_risk_equity_ratio="0.005",
        max_position_notional_equity_ratio="0.20",max_total_exposure_equity_ratio="0.20",max_leverage="2")
    p=resolve_risk_policy(c,account(),code_version="a"*40,decision_versions=("2.0.0",))
    assert (p.max_risk,p.max_notional,p.max_leverage)==(D("50"),D("2000"),D("2"))

def test_invalid_reload_blocks_new_risk_and_retains_last_valid_revision(tmp_path):
    from quant_execution.risk_config import RiskConfigV2, RiskConfigLoader
    f=tmp_path/"risk.json";f.write_text(RiskConfigV2().model_dump_json())
    loader=RiskConfigLoader(f);first=loader.poll()
    f.write_text('{"risk_per_trade_equity_ratio": "NaN"}')
    bad=loader.poll()
    assert bad.status=="ERROR" and not bad.new_risk_allowed
    assert bad.config.digest==first.config.digest

def test_revision_detects_concurrent_edits(tmp_path):
    from quant_execution.risk_config import RiskConfigV2, save_risk_config
    f=tmp_path/"risk.json";c=RiskConfigV2()
    save_risk_config(f,c,expected_digest=None)
    update=RiskConfigV2(revision=2,max_leverage="2")
    save_risk_config(f,update,expected_digest=c.digest)
    with pytest.raises(ValueError,match="CONCURRENT"):
        save_risk_config(f,RiskConfigV2(revision=3),expected_digest=c.digest)

def test_hold_and_cooldown_are_configurable():
    from quant_execution.risk_config import RiskConfigV2
    c=RiskConfigV2(max_hold_seconds_by_horizon={"1_3H":3600,"3_8H":18000,"8_24H":43200},cooldown_seconds=0)
    assert c.hold_seconds("8_24H")==43200 and c.cooldown_seconds==0

@pytest.mark.parametrize("field,value",[("max_leverage","NaN"),("max_open_positions",0),
    ("risk_per_trade_equity_ratio","-1"),("unknown_field",1)])
def test_invalid_risk_inputs_fail_closed(field,value):
    from quant_execution.risk_config import RiskConfigV2
    with pytest.raises(ValueError):RiskConfigV2(**{field:value})
