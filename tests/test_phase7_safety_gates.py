from __future__ import annotations

from pathlib import Path
import ast
import re

import pytest

from quant_phase7.sources import SourceStatus, default_source_definitions
from quant_phase7.retention import Phase7RetentionPolicy


ROOT = Path(__file__).resolve().parents[1]
PHASE7 = ROOT / "src" / "quant_phase7"


def phase7_source_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in PHASE7.rglob("*.py"))


def test_phase7_has_no_private_trading_or_ai_imports_or_routes():
    text = phase7_source_text().lower()
    forbidden = (
        # Provider API keys are permitted only for read-only RPC data sources;
        # private exchange credentials and signing material remain forbidden.
        "private api", "api secret", "bitget_api_key", "bitget_api_secret",
        "bitget_api_passphrase", "passphrase",
        "order api", "submit_order", "create_order", "cancel_order",
        "position api", "live executor", "trading_mode=live",
        "openai", "anthropic", "ai_provider",
    )
    assert not [token for token in forbidden if token in text]
    for path in PHASE7.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [node.module or ""]
            else:
                continue
            assert not any(
                name.startswith(("quant_phase6", "openai", "anthropic"))
                for name in imported
            )


def test_phase7_source_limits_are_finite_and_bitget_has_no_silent_fallback():
    definitions = default_source_definitions()
    assert all(item.max_retries <= 4 for item in definitions)
    assert all(0 < item.max_bytes <= 16 * 1024 * 1024 and item.max_events > 0 for item in definitions)
    assert all(item.queue_capacity > 0 and item.max_concurrency > 0 for item in definitions)
    assert all(item.fallback_source_id is None for item in definitions)
    bitget = next(item for item in definitions if item.source_id == "bitget_spot_uta_v3")
    assert bitget.status is SourceStatus.PENDING_CONTRACT


def test_phase7_retention_and_resource_defaults_are_bounded():
    policy = Phase7RetentionPolicy()
    assert policy.days_for("phase7_spot_flow_windows") == 30
    assert max(policy.as_map().values()) <= 730
    assert len(policy.tables()) == 8
    acceptance_compose = ROOT / "docker-compose.phase7-acceptance.yml"
    assert acceptance_compose.exists()
    acceptance = acceptance_compose.read_text(encoding="utf-8")
    assert "phase7_acceptance_pgdata" in acceptance
    assert "mem_limit: 768m" in acceptance
    assert "mem_limit: 256m" in acceptance
    assert "mem_limit: 384m" in acceptance
    assert "ports:" not in acceptance
    assert 'PHASE7_BITCOIN_RPC_MAX_RESPONSE_BYTES: "${PHASE7_BITCOIN_RPC_MAX_RESPONSE_BYTES:-33554432}"' in acceptance
    for compose_path in (ROOT / "docker-compose.yml", ROOT / "docker-compose.local.yml", ROOT / "docker-compose.server.yml"):
        service_names = re.findall(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", compose_path.read_text(encoding="utf-8"))
        assert not any(name.startswith("phase7-") for name in service_names)
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "quant-collector" in compose and "quant-engine" in compose and "postgres:" in compose


def test_phase7_acceptance_preflight_is_compose_v1_compatible_and_never_starts_runtime():
    runner = (ROOT / "scripts" / "phase7_acceptance_preflight.py").read_text(encoding="utf-8")
    assert '["docker", "compose", "version"]' in runner
    assert 'shutil.which("docker-compose")' in runner
    assert '"config", "--quiet"' in runner
    assert '"config", "--services"' in runner
    assert '"docker", "build"' in runner
    assert '"docker", "image", "inspect"' in runner
    assert '"up"' not in runner
    assert '"start"' not in runner
    assert 'org.opencontainers.image.revision' in runner
    assert 'quant-phase7:phase7-' in runner
    assert '.env.local' in runner and 'git", "check-ignore"' in runner


def test_paper_mode_is_the_only_project_default():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "trading_mode = \"paper\"" in pyproject
    assert "TRADING_MODE=live" not in pyproject


@pytest.mark.parametrize("path", sorted(PHASE7.rglob("*.py")))
def test_phase7_modules_compile(path: Path):
    compile(path.read_text(encoding="utf-8"), str(path), "exec")

