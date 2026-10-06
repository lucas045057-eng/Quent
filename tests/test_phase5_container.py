from pathlib import Path


def test_phase5_runtime_taxonomy_is_packaged_in_container():
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY config ./config" in dockerfile


def test_local_compose_runs_collector_entrypoint_for_collector_service():
    compose = (Path(__file__).parents[1] / "docker-compose.local.yml").read_text(encoding="utf-8")
    assert 'command: ["python", "-m", "quant_phase1.entrypoints.collector"]' in compose
