from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from datetime import timezone

import pytest

from quant_data_layer.replay import (
    ReplayContractError,
    compare_replay_outcomes,
    load_replay_dataset,
    seeded_random,
    verify_replay_components,
)


AS_OF = "2026-09-26T00:00:00Z"


def _record(phase: int, stream_id: str, identity: str, *, status="AVAILABLE", value="1"):
    return {
        "phase": phase,
        "stream_id": stream_id,
        "source_id": f"phase{phase}_source",
        "canonical_identity": identity,
        "event_timestamp_utc": "2026-09-25T23:58:00Z",
        "source_timestamp_utc": "2026-09-25T23:58:00Z",
        "fetched_at_utc": "2026-09-25T23:58:01Z",
        "processed_at_utc": "2026-09-25T23:58:02Z",
        "provenance": "SYNTHETIC_FIXTURE",
        "status": status,
        "fields": {
            "sample": {
                "value": None if status == "NOT_AVAILABLE" else value,
                "status": status,
                "provenance": "SYNTHETIC_FIXTURE" if status == "AVAILABLE" else "NOT_AVAILABLE",
            }
        },
        "payload": {"fixture": True},
    }


def _write_dataset(tmp_path: Path, records: list[dict], *, manifest_overrides=None):
    raw = "".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records).encode()
    compressed = gzip.compress(raw, mtime=0)
    (tmp_path / "records.jsonl.gz").write_bytes(compressed)
    manifest = {
        "replay_version": "DATA_LAYER_REPLAY_V1",
        "git_sha": "a" * 40,
        "collector_image_digest": "sha256:" + "b" * 64,
        "engine_image_digest": "sha256:" + "c" * 64,
        "postgres_image_digest": "sha256:" + "d" * 64,
        "postgres_server_version": "16.15",
        "migration_version": 15,
        "config_sha256": "e" * 64,
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "seed": 20260926,
        "as_of_utc": AS_OF,
        "fixture": {
            "filename": "records.jsonl.gz",
            "compressed_sha256": hashlib.sha256(compressed).hexdigest(),
            "compressed_bytes": len(compressed),
            "uncompressed_bytes": len(raw),
            "record_count": len(records),
        },
        "required_phases": list(range(1, 9)),
        "replay_components_sha256": {"test_component": "f" * 64},
        "fixture_origin": "synthetic_test_data_only",
        "external_provider_calls": False,
        "private_api_calls": False,
        "trading_enabled": False,
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _all_phase_records():
    return [
        _record(1, "phase1.closed_klines", "BTCUSDT:5m:2026-09-25T23:55:00Z"),
        _record(2, "phase2.open_interest", "BTCUSDT:oi:2026-09-25T23:55:00Z"),
        _record(3, "phase3.bitget_trades", "bitget:trade:1001"),
        _record(4, "phase4.liquidation_events", "bitget:liq:2001"),
        _record(5, "phase5.market_regime", "btc:regime:2026-09-25T23:55:00Z"),
        _record(6, "phase6.news_feed", "fixture:news:3001"),
        _record(7, "phase7.bitcoin_blocks", "bitcoin:main:968443:tx:1:vout:0"),
        _record(8, "phase8.options_context", "BTC:options:2026-09-25T23:55:00Z"),
    ]


def test_replay_loader_checks_hashes_phase_coverage_and_missing_is_not_zero(tmp_path):
    records = _all_phase_records()
    records[-1]["fields"]["unknown_iv"] = {
        "value": None,
        "status": "NOT_AVAILABLE",
        "provenance": "NOT_AVAILABLE",
    }
    path = _write_dataset(tmp_path, records)

    dataset = load_replay_dataset(path)

    assert dataset.manifest.replay_version == "DATA_LAYER_REPLAY_V1"
    assert dataset.manifest.migration_version == 15
    assert {record.phase for record in dataset.records} == set(range(1, 9))
    assert dataset.records[-1].fields["unknown_iv"]["value"] is None
    assert dataset.records[-1].fields["unknown_iv"]["status"] == "NOT_AVAILABLE"


def test_replay_loader_rejects_tampered_archive(tmp_path):
    path = _write_dataset(tmp_path, _all_phase_records())
    with (tmp_path / "records.jsonl.gz").open("ab") as stream:
        stream.write(b"tampered")

    with pytest.raises(ReplayContractError, match="hash"):
        load_replay_dataset(path)


@pytest.mark.parametrize("limit", [1, 7])
def test_replay_loader_enforces_record_and_decompressed_byte_limits(tmp_path, limit):
    path = _write_dataset(tmp_path, _all_phase_records())

    with pytest.raises(ReplayContractError, match="limit"):
        load_replay_dataset(path, max_records=limit)

    with pytest.raises(ReplayContractError, match="limit"):
        load_replay_dataset(path, max_uncompressed_bytes=limit)


def test_replay_loader_rejects_future_event_time(tmp_path):
    records = _all_phase_records()
    records[0]["event_timestamp_utc"] = "2026-09-26T00:00:01Z"
    path = _write_dataset(tmp_path, records)

    with pytest.raises(ReplayContractError, match="future"):
        load_replay_dataset(path)


def test_replay_loader_normalizes_z_timestamps_to_utc(tmp_path):
    records = _all_phase_records()
    records[0]["event_timestamp_utc"] = "2026-09-25T23:58:00Z"
    path = _write_dataset(tmp_path, records)

    dataset = load_replay_dataset(path)

    assert dataset.records[0].event_timestamp_utc.tzinfo == timezone.utc


def test_replay_loader_requires_field_provenance_to_be_nonempty(tmp_path):
    records = _all_phase_records()
    records[0]["fields"]["sample"]["provenance"] = ""
    path = _write_dataset(tmp_path, records)

    with pytest.raises(ReplayContractError, match="provenance"):
        load_replay_dataset(path)


def test_replay_loader_enforces_compressed_byte_limit(tmp_path):
    path = _write_dataset(tmp_path, _all_phase_records())

    with pytest.raises(ReplayContractError, match="compressed byte limit"):
        load_replay_dataset(path, max_compressed_bytes=1)


def test_replay_loader_rejects_credential_bearing_urls_without_echo(tmp_path):
    records = _all_phase_records()
    credential = "do-not-print-this"
    records[0]["payload"]["provider_url"] = "https://" + "user:" + credential + "@rpc.invalid/"
    path = _write_dataset(tmp_path, records)

    with pytest.raises(ReplayContractError) as error:
        load_replay_dataset(path)

    assert credential not in str(error.value)


def test_replay_manifest_rejects_secret_material_without_echoing_it(tmp_path):
    secret = "test-secret-material-not-for-output"
    path = _write_dataset(tmp_path, _all_phase_records(), manifest_overrides={"api_key": secret})

    with pytest.raises(ReplayContractError) as error:
        load_replay_dataset(path)

    assert secret not in str(error.value)


def test_replay_component_files_are_hash_pinned_and_repository_relative(tmp_path):
    component = tmp_path / "runner.py"
    component.write_text("stable runner", encoding="utf-8")
    path = _write_dataset(tmp_path, _all_phase_records(), manifest_overrides={
        "replay_components_sha256": {
            "runner.py": hashlib.sha256(component.read_bytes()).hexdigest(),
        }
    })
    from quant_data_layer.replay import load_manifest

    verify_replay_components(load_manifest(path), project_root=tmp_path)
    component.write_text("changed runner", encoding="utf-8")
    with pytest.raises(ReplayContractError, match="component hash"):
        verify_replay_components(load_manifest(path), project_root=tmp_path)


def test_replay_manifest_rejects_component_path_escape(tmp_path):
    from quant_data_layer.replay import load_manifest

    path = _write_dataset(tmp_path, _all_phase_records(), manifest_overrides={
        "replay_components_sha256": {"../outside.py": "f" * 64}
    })
    with pytest.raises(ReplayContractError, match="components"):
        load_manifest(path)


def test_replay_manifest_requires_pinned_postgres_16_15(tmp_path):
    path = _write_dataset(tmp_path, _all_phase_records(), manifest_overrides={
        "postgres_server_version": "17.0",
    })

    with pytest.raises(ReplayContractError, match="16.15"):
        load_replay_dataset(path)


def test_seeded_random_and_replay_comparison_are_deterministic():
    first = seeded_random(20260926, "phase7.bitcoin_blocks")
    second = seeded_random(20260926, "phase7.bitcoin_blocks")
    assert [first.randrange(1_000_000) for _ in range(5)] == [second.randrange(1_000_000) for _ in range(5)]

    outcome = {
        "row_counts": {"klines": 1, "bitcoin_onchain_events": 12_665},
        "identity_sha256": {"bitcoin_onchain_events": "1" * 64},
        "cursors": {"bitcoin": 968_443, "ethereum": 20_000_119},
        "quality_statuses": {"phase8.unknown_iv": "NOT_AVAILABLE"},
        "aggregations": {"phase3.flow_windows": 3},
        "contexts": {"phase5.regime": "NEUTRAL"},
        "stage1_outputs": {"eligible_count": 0},
        "options_outputs": {"context_only": True},
    }
    comparison = compare_replay_outcomes((outcome, outcome, outcome))

    assert comparison.equal is True
    assert comparison.differences == ()


def test_replay_comparison_names_semantic_difference_path():
    first = {"row_counts": {"klines": 10}, "cursors": {"bitcoin": 100}}
    second = {"row_counts": {"klines": 10}, "cursors": {"bitcoin": 99}}

    comparison = compare_replay_outcomes((first, second))

    assert comparison.equal is False
    assert "cursors.bitcoin" in comparison.differences


def test_data_layer_fixture_builder_covers_all_registered_streams_and_large_block():
    from quant_data_layer.backpressure import STREAM_CONTRACTS
    from scripts.build_data_layer_replay_v1_fixture import build_records

    records = build_records()
    covered = {(row["phase"], row["stream_id"]) for row in records}
    required = {
        (int(contract.phase.value.removeprefix("phase")), contract.stream_id)
        for contract in STREAM_CONTRACTS
        if contract.phase is not None
    }
    btc_events = [
        row for row in records
        if row["phase"] == 7 and row["payload"].get("chain") == "BITCOIN"
        and row["payload"].get("block_height") == 968_443
    ]

    assert required.issubset(covered)
    assert len(btc_events) == 12_665
    assert all(row["provenance"] == "SYNTHETIC_FIXTURE" for row in btc_events)


def test_data_layer_fixture_marks_ai_and_unknown_options_fields_missing():
    from scripts.build_data_layer_replay_v1_fixture import build_records

    records = build_records()
    ai = next(row for row in records if row["canonical_identity"] == "fixture:ai:not-configured")
    unknown_iv = next(row for row in records if row["canonical_identity"] == "BTC:markprice:fixture:1")
    sparse = next(row for row in records if row["canonical_identity"].endswith(":ticker:sparse:2"))

    assert ai["payload"]["provider_state"] == "NOT_CONFIGURED"
    assert ai["fields"]["analysis"]["status"] == "NOT_AVAILABLE"
    assert ai["fields"]["analysis"]["value"] is None
    assert unknown_iv["fields"]["iv"]["unit_status"] == "UNCONFIRMED"
    assert unknown_iv["fields"]["iv"]["status"] == "NOT_AVAILABLE"
    assert sparse["payload"]["message_kind"] == "sparse_update"
    assert sparse["payload"]["must_preserve_unmentioned"] is True


def test_full_data_layer_fixture_obeys_bounded_manifest_contract(tmp_path):
    from scripts.build_data_layer_replay_v1_fixture import build_records

    records = build_records()
    path = _write_dataset(tmp_path, records)
    dataset = load_replay_dataset(path)

    assert dataset.manifest.fixture.record_count == 12_738
    assert dataset.uncompressed_bytes < 16 * 1024 * 1024
    assert len(dataset.records) < 20_000
    assert sum(
        record.phase == 7 and record.payload.get("block_height") == 968_443
        for record in dataset.records
    ) == 12_665
