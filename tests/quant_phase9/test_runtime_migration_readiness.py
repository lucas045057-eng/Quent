"""Runtime schema checks must never acquire DDL or auto-upgrade privileges."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from quant_phase1.db import SchemaNotReadyError
from quant_phase1.service import open_repository


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_FILES = (
    "src/quant_phase1/service.py",
    "src/quant_phase1/entrypoints/collector.py",
    "src/quant_phase1/entrypoints/engine.py",
    "src/quant_phase2/runtime.py",
    "src/quant_phase6/runtime.py",
    "src/quant_phase7/runtime.py",
    "src/quant_phase8/runtime.py",
    "scripts/phase7_acceptance_db.py",
)


@pytest.mark.parametrize("relative_path", RUNTIME_FILES)
def test_runtime_source_has_no_migration_runner_reference(relative_path: str) -> None:
    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
    names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "apply_migrations" not in names | imports


class MissingSchemaConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def execute(self, query: str, params: object = None) -> "MissingSchemaConnection":
        self.queries.append(query)
        if not query.lstrip().upper().startswith("SELECT"):
            pytest.fail(f"runtime attempted a write or DDL: {query}")
        return self

    def fetchone(self) -> tuple[None]:
        return (None,)


def test_open_repository_fails_closed_without_schema_and_does_not_write() -> None:
    connection = MissingSchemaConnection()
    with pytest.raises(SchemaNotReadyError, match="schema_migrations is absent"):
        open_repository(lambda _dsn: connection, "unused")
    assert connection.queries == ["SELECT to_regclass('schema_migrations')"]
