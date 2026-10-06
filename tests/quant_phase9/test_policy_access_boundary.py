from __future__ import annotations

import ast
from pathlib import Path


def test_policy_files_have_one_production_loader():
    root = Path(__file__).resolve().parents[2] / "src" / "quant_phase9"
    forbidden = ("phase9_policy_v1.json", "phase9_policy_v1.approval.json")
    for path in root.rglob("*.py"):
        if path.name == "policy.py":
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assert not any(name in source for name in forbidden), path
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "quant_phase9.policy":
                assert all(not alias.name.startswith("_") for alias in node.names), path
