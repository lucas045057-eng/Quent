"""Opt-in fixtures for integration tests that need disposable infrastructure."""
from __future__ import annotations

import os

import pytest

from tests.postgres_fixture import DEFAULT_CANONICAL_DSN, fresh_test_database


@pytest.fixture(scope="session", autouse=True)
def provision_isolated_test_postgres():
    if os.environ.get("QUANT_PROVISION_TEST_POSTGRES") != "1":
        yield
        return
    test_dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not test_dsn:
        raise RuntimeError("TEST_POSTGRES_DSN_REQUIRED")
    canonical_dsn = (
        os.environ.get("CANONICAL_POSTGRES_DSN")
        or os.environ.get("POSTGRES_DSN")
        or DEFAULT_CANONICAL_DSN
    )
    with fresh_test_database(test_dsn, canonical_dsn):
        yield
