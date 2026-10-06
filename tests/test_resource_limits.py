from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_docker_has_only_collector_engine_and_postgres_services():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "quant-collector:" in compose
    assert "quant-engine:" in compose
    assert "postgres:" in compose
    assert "executor:" not in compose
    assert "redis:" not in compose


def test_docker_memory_limits_match_phase1_budget():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "mem_limit: 256m" in compose
    assert "mem_limit: 384m" in compose


def test_docker_configuration_is_paper_only_and_has_no_private_keys():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "TRADING_MODE=paper" in compose
    assert "BITGET_API_SECRET" not in compose
    assert "BITGET_API_PASSPHRASE" not in compose


def test_phase2_is_enabled_only_in_the_existing_paper_engine():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'PHASE2_ENABLED: "1"' in compose
    assert "PHASE2_SYMBOLS" not in compose
    assert compose.count("PHASE2_ENABLED") == 1
    assert "executor:" not in compose


def test_server_compose_uses_existing_database_network_without_host_mode():
    server_compose = (ROOT / "docker-compose.server.yml").read_text(encoding="utf-8")
    assert "network_mode: host" not in server_compose
    assert "suixiangji-staging_default:" in server_compose
    assert "external: true" in server_compose


def test_local_compose_resource_budget_matches_release_candidate_documentation():
    compose = (ROOT / "docker-compose.local.yml").read_text(encoding="utf-8")
    expected = {"postgres": "768m", "quant-collector": "768m", "quant-engine": "512m"}
    limits = {}
    service = None
    for line in compose.splitlines():
        if line.startswith("  ") and not line.startswith("    ") and line.endswith(":"):
            service = line.strip()[:-1]
        elif service in expected and line.startswith("    mem_limit: "):
            limits[service] = line.split(":", 1)[1].strip()
    assert limits == expected
