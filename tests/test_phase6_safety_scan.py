from __future__ import annotations

from pathlib import Path

from quant_phase1.config import Settings


ROOT = Path(__file__).parents[1]
PHASE6 = ROOT / "src" / "quant_phase6"


def test_phase6_domain_has_no_provider_sdk_or_execution_surface():
    source = "\n".join(path.read_text(encoding="utf-8") for path in PHASE6.glob("*.py"))
    forbidden = (
        "from openai", "import openai", "from deepseek", "import deepseek",
        "order_api", "position_api", "live_executor", "private_api",
        "TRADING_MODE=live",
    )
    assert not any(marker.lower() in source.lower() for marker in forbidden)
    assert "phase7_" not in source.lower()
    assert "bitcoin_rpc" not in source.lower()
    assert "ethereum_rpc" not in source.lower()


def test_phase6_runtime_remains_paper_only_and_prompt_data_is_untrusted():
    assert Settings.from_env({}).trading_mode == "paper"
    prompts = (PHASE6 / "prompts.py").read_text(encoding="utf-8")
    assert "UNTRUSTED_DATA" in prompts
    assert "system_instructions" in prompts


def test_phase6_does_not_add_daemon_or_container():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert set(line.strip().rstrip(":") for line in compose.splitlines() if line.startswith("  ") and line.endswith(":")) >= {
        "postgres", "quant-collector", "quant-engine",
    }
    assert "phase6-daemon" not in compose


def test_local_compose_whitelists_phase7_secrets_for_collector_only():
    compose = (ROOT / "docker-compose.local.yml").read_text(encoding="utf-8")
    assert "env_file:" not in compose
    assert "PHASE6_ENABLED: \"1\"" in compose
    assert 'PHASE7_ENABLED: "${PHASE7_ENABLED:-0}"' in compose
    collector, engine = compose.split("  quant-engine:", 1)
    assert 'PHASE7_BITCOIN_RPC_URL: "${PHASE7_BITCOIN_RPC_URL:-}"' in collector
    assert 'PHASE7_BITCOIN_RPC_PASSWORD: "${PHASE7_BITCOIN_RPC_PASSWORD:-}"' in collector
    assert 'PHASE7_ETHEREUM_RPC_URL: "${PHASE7_ETHEREUM_RPC_URL:-}"' in collector
    assert "_RPC_URL:" not in engine
    assert "_RPC_PASSWORD:" not in engine
    for limit in ("mem_limit: 768m", "mem_limit: 512m"):
        assert limit in compose
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert ".env.*" in dockerignore
    assert "!.env.example" in dockerignore
