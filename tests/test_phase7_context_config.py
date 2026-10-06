from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

import pytest

from quant_phase7.context_config import load_reviewed_label_snapshots, load_whale_thresholds
from quant_phase7.contracts import Chain


def _write_snapshot(path, *, reviewed=True):
    document = {
        "chain": "BITCOIN",
        "source_id": "operator-reviewed",
        "source_version": "registry-v1",
        "label_version": "labels-2026-09",
        "source_urls": ["https://labels.example/source.json"],
        "reviewer": "operator",
        "effective_from": "2026-09-20T00:00:00Z",
        "effective_to": None,
        "coverage_denominator": 2,
        "reviewed": reviewed,
        "labels": [
            {"address": "bc1qsource", "category": "KNOWN_EXTERNAL", "confidence": "1"},
            {"address": "bc1qdestination", "category": "KNOWN_EXCHANGE", "confidence": "1"},
        ],
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    document["snapshot_hash"] = hashlib.sha256(encoded).hexdigest()
    path.write_text(json.dumps({"snapshots": [document]}), encoding="utf-8")


def test_label_snapshot_loader_verifies_hash_and_operator_review(tmp_path):
    path = tmp_path / "labels.json"
    _write_snapshot(path)

    result = load_reviewed_label_snapshots(str(path))

    assert result[Chain.BITCOIN].reviewed is True
    assert result[Chain.BITCOIN].coverage_denominator == 2
    assert len(result[Chain.BITCOIN].labels) == 2
    assert result[Chain.BITCOIN].labels[0].effective_from == datetime(2026, 9, 20, tzinfo=timezone.utc)


def test_label_snapshot_loader_rejects_tampering_and_unreviewed_data(tmp_path):
    path = tmp_path / "labels.json"
    _write_snapshot(path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["snapshots"][0]["labels"][0]["category"] = "KNOWN_EXCHANGE"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        load_reviewed_label_snapshots(str(path))

    _write_snapshot(path, reviewed=False)
    with pytest.raises(ValueError, match="fields are invalid"):
        load_reviewed_label_snapshots(str(path))


def test_threshold_loader_is_versioned_and_has_no_implicit_thresholds(tmp_path):
    assert load_whale_thresholds("") == {}
    path = tmp_path / "thresholds.json"
    path.write_text(json.dumps({"thresholds": [{
        "chain": "BITCOIN",
        "asset_id": "BITCOIN:NATIVE:NATIVE:btc-v1",
        "threshold_version": "btc-large-v1",
        "tiers": [{"name": "LARGE", "min_usd": "10000", "max_usd": None}],
    }]}), encoding="utf-8")

    result = load_whale_thresholds(str(path))

    config = result[(Chain.BITCOIN, "BITCOIN:NATIVE:NATIVE:btc-v1")]
    assert config.threshold_version == "btc-large-v1"
    assert config.tiers[0].min_usd == 10_000
