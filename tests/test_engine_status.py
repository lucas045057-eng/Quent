from quant_phase1.contracts import DataStatus
from quant_phase1.entrypoints.engine import persistence_status


def test_persistence_failure_never_reports_available_cycle_status() -> None:
    assert persistence_status(True) is DataStatus.AVAILABLE
    assert persistence_status(False) is DataStatus.ERROR
