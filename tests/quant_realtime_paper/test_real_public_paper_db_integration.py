import os

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from tests.postgres_fixture import assert_database_is_fresh, validate_test_postgres_dsn


VALID_DSN = "postgresql://quant_test:secret@127.0.0.1:55443/quant_phase9_test"
CANONICAL_DSN = "postgresql://quant:secret@postgres:5432/quant"


def test_test_dsn_requires_loopback_host_and_exact_disposable_database():
    info = validate_test_postgres_dsn(VALID_DSN, CANONICAL_DSN)
    assert info["host"] == "127.0.0.1"
    assert info["dbname"] == "quant_phase9_test"
    with pytest.raises(ValueError, match="LOOPBACK"):
        validate_test_postgres_dsn(
            "postgresql://quant_test:secret@db.internal:55443/quant_phase9_test",
            CANONICAL_DSN,
        )
    with pytest.raises(ValueError, match="TEST_DATABASE_NAME"):
        validate_test_postgres_dsn(
            "postgresql://quant_test:secret@127.0.0.1:55443/quant",
            CANONICAL_DSN,
        )


def test_test_dsn_rejects_canonical_identity_and_reused_database():
    with pytest.raises(ValueError, match="CANONICAL_DATABASE_USED"):
        validate_test_postgres_dsn(VALID_DSN, VALID_DSN)
    with pytest.raises(ValueError, match="CANONICAL_DATABASE_USED"):
        validate_test_postgres_dsn(
            VALID_DSN, "postgresql://quant:secret@canonical.internal:5432/quant_phase9_test",
        )
    assert_database_is_fresh(False)
    with pytest.raises(ValueError, match="REUSED_TEST_DATABASE"):
        assert_database_is_fresh(True)


@pytest.mark.skipif(
    os.environ.get("QUANT_PROVISION_TEST_POSTGRES") != "1",
    reason="isolated PostgreSQL fixture is enabled only for the explicit DB test run",
)
def test_fixture_database_is_loopback_fresh_utc_and_noncanonical():
    dsn = os.environ["TEST_POSTGRES_DSN"]
    info = conninfo_to_dict(dsn)
    assert info["host"] in {"127.0.0.1", "localhost", "::1"}
    with psycopg.connect(dsn) as connection:
        assert connection.execute("SELECT current_database()").fetchone()[0] == "quant_phase9_test"
        assert connection.execute("SHOW TIME ZONE").fetchone()[0] == "UTC"
    assert os.environ.get("CANONICAL_DATABASE_USED") == "false"
