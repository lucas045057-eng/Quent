from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_compose_has_exact_phase5_service_budget_and_no_extra_service():
    text = (ROOT / "docker-compose.local.yml").read_text(encoding="utf-8")
    assert text.count("container_name:") == 3
    postgres = text.split("  postgres:", 1)[1].split("  quant-collector:", 1)[0]
    collector = text.split("  quant-collector:", 1)[1].split("  quant-engine:", 1)[0]
    engine = text.split("  quant-engine:", 1)[1].split("\nvolumes:", 1)[0]
    assert "mem_limit: 768m" in postgres
    assert "mem_limit: 768m" in collector
    assert "mem_limit: 512m" in engine
    assert "TRADING_MODE: paper" in text
    assert "live" not in text.lower()


def test_compose_uses_restart_policy_for_all_phase5_services():
    text = (ROOT / "docker-compose.local.yml").read_text(encoding="utf-8")
    assert text.count("restart: unless-stopped") == 3
    services = ("postgres", "quant-collector", "quant-engine")
    starts = [text.index(f"  {service}:") for service in services]
    ends = starts[1:] + [text.index("\nvolumes:")]
    for start, end in zip(starts, ends):
        block = text[start:end]
        assert "restart: unless-stopped" in block


def test_phase5_runtime_has_no_private_or_phase6_surface():
    runtime = (ROOT / "src/quant_phase5/runtime.py").read_text(encoding="utf-8")
    health = (ROOT / "src/quant_phase5/health.py").read_text(encoding="utf-8")
    combined = runtime + health
    for forbidden in ("BITGET_API_KEY", "BITGET_API_SECRET", "order api", "position api", "phase6", "live_executor"):
        assert forbidden.lower() not in combined.lower()
