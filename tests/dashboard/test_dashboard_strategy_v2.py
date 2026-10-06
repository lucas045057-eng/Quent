from pathlib import Path
from tests.dashboard.test_dashboard_api import client
from quant_execution.risk_config import RiskConfigV2

def test_strategy_v2_api_preserves_unknown_and_research_windows(tmp_path):
    response=client(tmp_path).get("/api/strategy-v2")
    assert response.status_code==200
    data=response.json()["data"]
    assert data["strategy_version"]=="QUANT_PAPER_V2"
    assert data["horizons"]==["1_3H","3_8H","8_24H"]
    assert data["items"]==[] and data["data_source"]=="UNKNOWN"

def test_risk_policy_api_shows_file_revision_and_error(tmp_path,monkeypatch):
    path=tmp_path/"config"/"risk_policy_v2.json";path.parent.mkdir()
    path.write_text(RiskConfigV2().model_dump_json(by_alias=True))
    c=client(tmp_path);body=c.get("/api/risk-policy").json()["data"]
    assert body["status"]=="ACTIVE" and body["config"]["risk_per_trade_equity_ratio"]=="0.0025"
    path.write_text("{")
    body=c.get("/api/risk-policy").json()["data"]
    assert body["status"]=="ERROR" and body["config"]["revision"]==1
    assert body["new_risk_allowed"] is False
    assert c.post("/api/risk-policy").status_code==405


def test_screening_identity_is_distinct_from_market_input_digest(tmp_path, monkeypatch):
    from dashboard.backend.config import DashboardConfig
    from dashboard.backend.service import DashboardService
    service=DashboardService(DashboardConfig(root=tmp_path))
    database={'tables':{'strategy_v2_artifacts':[
        {'artifact_kind':'SCREENING','canonical_digest':'a'*64,
         'payload':{'market_digest':'b'*64,'candidates':[],'waiting_triggers':[]}}
    ]},'warnings':[]}
    monkeypatch.setattr(service,'sources',lambda:(database,{}))
    data=service.strategy_v2()['data']
    assert data['screening_digest']=='a'*64
    assert data['market_digest']=='b'*64
