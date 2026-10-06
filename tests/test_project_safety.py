from pathlib import Path


ROOT = Path(__file__).parents[1]
SRC = ROOT / "src" / "quant_phase1"


def test_project_has_importable_phase1_entrypoints():
    assert (SRC / "entrypoints" / "collector.py").is_file()
    assert (SRC / "entrypoints" / "engine.py").is_file()


def test_phase1_source_has_no_private_or_order_modules():
    forbidden = {"private", "orders", "positions", "live_executor", "executor"}
    source_names = {path.stem.lower() for path in SRC.rglob("*.py")}
    assert source_names.isdisjoint(forbidden)


def test_phase1_entrypoints_import_without_trading_side_effects():
    import importlib

    collector = importlib.import_module("quant_phase1.entrypoints.collector")
    engine = importlib.import_module("quant_phase1.entrypoints.engine")
    assert callable(collector.main)
    assert callable(engine.main)
