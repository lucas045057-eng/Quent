from __future__ import annotations

from pathlib import Path

import pytest

from quant_data_layer.replay import load_manifest
from quant_phase9.replay import load_phase9_replay_manifest


ROOT = Path(__file__).resolve().parents[2]


def test_data_layer_loader_remains_isolated_from_phase9():
    phase9_path = ROOT / "tests/fixtures/phase9/manifest.json"
    with pytest.raises(ValueError):
        load_manifest(phase9_path)
    phase9 = load_phase9_replay_manifest(phase9_path, project_root=ROOT)
    assert phase9.parent_dataset.manifest.replay_version == "DATA_LAYER_REPLAY_V1"


def test_phase9_loader_rejects_data_layer_manifest():
    with pytest.raises(ValueError):
        load_phase9_replay_manifest(
            ROOT / "tests/fixtures/data_layer_replay_v1/manifest.json",
            project_root=ROOT,
        )
