from pathlib import Path

from quant_phase3.resources import assess_resource_usage


ROOT = Path(__file__).parents[1]


def test_resource_assessment_warns_before_collector_memory_limit_and_fails_on_oom_budget():
    warning = assess_resource_usage(collector_memory_mib=225, engine_memory_mib=200, postgres_memory_mib=300)
    critical = assess_resource_usage(collector_memory_mib=257, engine_memory_mib=200, postgres_memory_mib=300)

    assert warning.status == "RESOURCE_WARNING"
    assert "collector" in warning.reasons[0]
    assert critical.status == "RESOURCE_CRITICAL"


def test_compose_keeps_two_quant_containers_paper_mode_limits_and_log_rotation():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    server = (ROOT / "docker-compose.server.yml").read_text(encoding="utf-8")

    assert compose.count("quant-collector:") == 1
    assert compose.count("quant-engine:") == 1
    assert "PHASE3_ENABLED: \"0\"" in compose
    assert compose.count("mem_limit: 256m") == 1
    assert compose.count("mem_limit: 384m") == 2
    assert compose.count("max-size: 10m") >= 3
    assert "env_file:" in server
    assert "mem_limit: 256m" in server and "mem_limit: 384m" in server


def test_phase3_source_has_no_private_or_order_execution_path():
    source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "src" / "quant_phase3").rglob("*.py"))

    for forbidden in (
        "/api/v3/trade/place-order",
        "/api/v3/trade/cancel-order",
        "/api/v3/position",
        "LiveExecutor",
        "ACCESS-KEY",
        "BITGET_API_SECRET",
    ):
        assert forbidden not in source


def test_no_phase3_raw_trade_warehouse_or_unbounded_queue_contract_is_introduced():
    migration = (ROOT / "migrations" / "008_phase3_flow.sql").read_text(encoding="utf-8").lower()
    source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "src" / "quant_phase3").rglob("*.py"))

    assert "raw_trades" not in migration
    assert "asyncio.queue()" not in source.lower()
