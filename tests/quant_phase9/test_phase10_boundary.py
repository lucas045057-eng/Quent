from __future__ import annotations

from dataclasses import fields
from pathlib import Path

from quant_data_layer.observability import MAX_SOURCE_SNAPSHOTS, SourceId, SourcePhase
from quant_phase9.contracts import DecisionCandidateV1


ROOT = Path(__file__).resolve().parents[2]


def test_phase9_decision_stays_free_of_order_and_risk_fields():
    names = {field.name for field in fields(DecisionCandidateV1)}
    forbidden = {
        "entry", "entry_price", "stop", "stop_loss", "position_size",
        "leverage", "order_id", "order_type", "quantity",
    }
    assert not names & forbidden


def test_core_phase1_to_phase9_has_no_nautilus_dependency():
    for package in (ROOT / "src").glob("quant_phase[1-9]*"):
        if not package.is_dir():
            continue
        for path in package.rglob("*.py"):
            assert "nautilus_trader" not in path.read_text(encoding="utf-8"), path


def test_one_phase9_aggregate_fits_existing_source_cap():
    assert MAX_SOURCE_SNAPSHOTS == 12
    assert SourcePhase.PHASE9.value == "phase9"
    assert SourceId.PHASE9_EVALUATIONS.value == "phase9.evaluations"
