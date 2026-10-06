"""Guard and provision an explicitly requested disposable PostgreSQL test database."""
from __future__ import annotations

from contextlib import contextmanager
import os
from typing import Iterator

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo


TEST_DATABASE_NAME = "quant_phase9_test"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_CANONICAL_DSN = "postgresql://quant:quant@postgres:5432/quant"


def _identity(info: dict[str, str]) -> tuple[str, str, str]:
    return (
        (info.get("host") or "").strip().lower(),
        str(info.get("port") or "5432"),
        info.get("dbname") or "",
    )


def validate_test_postgres_dsn(test_dsn: str, canonical_dsn: str) -> dict[str, str]:
    try:
        info = conninfo_to_dict(test_dsn)
        canonical = conninfo_to_dict(canonical_dsn)
    except (TypeError, ValueError) as exc:
        raise ValueError("POSTGRES_DSN_INVALID") from exc
    hosts = (info.get("host") or "").split(",")
    if not hosts or any(host.strip().lower() not in LOOPBACK_HOSTS for host in hosts):
        raise ValueError("TEST_POSTGRES_HOST_NOT_LOOPBACK")
    if info.get("dbname") != TEST_DATABASE_NAME:
        raise ValueError("TEST_DATABASE_NAME_INVALID")
    if canonical.get("dbname") == TEST_DATABASE_NAME or _identity(info) == _identity(canonical):
        raise ValueError("CANONICAL_DATABASE_USED")
    return info


def assert_database_is_fresh(exists: bool) -> None:
    if exists:
        raise ValueError("REUSED_TEST_DATABASE")


@contextmanager
def fresh_test_database(test_dsn: str, canonical_dsn: str) -> Iterator[dict[str, object]]:
    info = validate_test_postgres_dsn(test_dsn, canonical_dsn)
    admin_dsn = make_conninfo(test_dsn, dbname="postgres")
    database = info["dbname"]
    created = False
    old_canonical_flag = os.environ.get("CANONICAL_DATABASE_USED")
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            exists = admin.execute(
                "SELECT 1 FROM pg_database WHERE datname=%s", (database,),
            ).fetchone() is not None
            assert_database_is_fresh(exists)
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
            created = True
        with psycopg.connect(test_dsn, autocommit=True) as connection:
            current_database = connection.execute("SELECT current_database()").fetchone()[0]
            time_zone = connection.execute("SHOW TIME ZONE").fetchone()[0]
            if current_database != TEST_DATABASE_NAME:
                raise ValueError("TEST_DATABASE_IDENTITY_MISMATCH")
            if time_zone != "UTC":
                raise ValueError("TEST_POSTGRES_TIME_ZONE_MISMATCH")
            user_relations = connection.execute(
                """SELECT count(*) FROM pg_class c
                   JOIN pg_namespace n ON n.oid=c.relnamespace
                   WHERE n.nspname NOT IN ('pg_catalog','information_schema','pg_toast')"""
            ).fetchone()[0]
            if user_relations:
                raise ValueError("TEST_DATABASE_NOT_EMPTY")
        os.environ["CANONICAL_DATABASE_USED"] = "false"
        yield {
            "database": current_database,
            "time_zone": time_zone,
            "canonical_database_used": False,
            "fresh_database": True,
        }
    finally:
        if created:
            with psycopg.connect(admin_dsn, autocommit=True) as admin:
                admin.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=%s AND pid<>pg_backend_pid()", (database,),
                )
                admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database)))
        if old_canonical_flag is None:
            os.environ.pop("CANONICAL_DATABASE_USED", None)
        else:
            os.environ["CANONICAL_DATABASE_USED"] = old_canonical_flag
