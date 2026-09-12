"""Fixtures for the live-database suites.

Every test starts from a clean, disposable namespace. Nothing here connects to
anything but the locally published loopback test services described by the
environment; if they are absent the suites are skipped and the acceptance gate
stays visibly incomplete rather than being satisfied by a mock.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import support

ORACLE_ENV = ("MIGR8_ORACLE_DSN", "MIGR8_ORACLE_USER", "MIGR8_ORACLE_PASSWORD")
PG_ENV = ("MIGR8_PG_HOST", "MIGR8_PG_PORT", "MIGR8_PG_DB", "MIGR8_PG_USER",
          "MIGR8_PG_PASSWORD")

LOCK_ID = 4711


def _missing(names: tuple[str, ...]) -> list[str]:
    return [name for name in names if not os.environ.get(name)]


# --- Oracle ---------------------------------------------------------------------

@pytest.fixture(scope="session")
def oracle_settings():
    missing = _missing(ORACLE_ENV)
    if missing:
        pytest.skip(
            "Oracle test service not configured: missing "
            + ", ".join(missing)
            + ". Start it with testenv/dbctl.sh up and use testenv/dbctl.sh test."
        )
    oracledb = pytest.importorskip("oracledb")
    settings = {
        "dsn": os.environ["MIGR8_ORACLE_DSN"],
        "user": os.environ["MIGR8_ORACLE_USER"].upper(),
        "password": os.environ["MIGR8_ORACLE_PASSWORD"],
    }
    try:
        with oracledb.connect(
            user=settings["user"], password=settings["password"], dsn=settings["dsn"]
        ) as connection:
            settings["banner"] = connection.version
    except oracledb.Error as exc:
        pytest.skip(f"Oracle test service is not reachable at {settings['dsn']}: {exc}")
    return settings


def _oracle_clean(settings, schema: str) -> None:
    import oracledb

    with oracledb.connect(
        user=settings["user"], password=settings["password"], dsn=settings["dsn"]
    ) as connection:
        cursor = connection.cursor()
        rows = cursor.execute(
            "SELECT object_name, object_type FROM all_objects WHERE owner = :owner "
            "AND object_type IN ('TABLE','VIEW','PROCEDURE','FUNCTION','PACKAGE','TYPE',"
            "'TRIGGER','SEQUENCE','LIBRARY') ORDER BY "
            "CASE object_type WHEN 'TRIGGER' THEN 0 WHEN 'VIEW' THEN 1 "
            "WHEN 'PACKAGE' THEN 2 WHEN 'PROCEDURE' THEN 3 WHEN 'FUNCTION' THEN 4 "
            "WHEN 'TABLE' THEN 5 ELSE 6 END",
            owner=schema,
        ).fetchall()
        for name, kind in rows:
            suffix = " CASCADE CONSTRAINTS PURGE" if kind == "TABLE" else ""
            if kind == "TYPE":
                suffix = " FORCE"
            try:
                cursor.execute(f'DROP {kind} "{schema}"."{name}"{suffix}')
            except oracledb.DatabaseError:
                pass
        connection.commit()


@pytest.fixture
def oracle_project(tmp_path, oracle_settings):
    """A clean Oracle namespace plus a config file pointing at it."""
    schema = oracle_settings["user"]
    _oracle_clean(oracle_settings, schema)
    config = support.write(tmp_path / "migr8.toml", f"""
        [database]
        adapter = "oracle"
        dsn = "{oracle_settings['dsn']}"
        user = "{oracle_settings['user']}"
        target_schema = "{schema}"

        [oracle]
        ddl_lock_timeout_seconds = 10

        [lock]
        provider = "dbms_lock"
        package = "SYS.DBMS_LOCK"
        id = {LOCK_ID}
        timeout_seconds = 20
    """)
    os.environ["MIGR8_PASSWORD"] = oracle_settings["password"]
    yield tmp_path, config, schema
    _oracle_clean(oracle_settings, schema)


@pytest.fixture
def oracle_query(oracle_settings):
    """Run a query through an independent observer connection."""
    import oracledb

    def run(sql: str, **binds):
        """Execute a statement; returns rows for a query and commits otherwise."""
        with oracledb.connect(
            user=oracle_settings["user"], password=oracle_settings["password"],
            dsn=oracle_settings["dsn"],
        ) as connection:
            cursor = connection.cursor()
            cursor.execute(sql, binds)
            if cursor.description is None:
                connection.commit()
                return []
            rows = [tuple(row) for row in cursor.fetchall()]
            connection.commit()
            return rows

    return run


# --- PostgreSQL ------------------------------------------------------------------

@pytest.fixture(scope="session")
def pg_settings():
    missing = _missing(PG_ENV)
    if missing:
        pytest.skip(
            "PostgreSQL test service not configured: missing " + ", ".join(missing)
            + ". Start it with testenv/dbctl.sh up and use testenv/dbctl.sh test."
        )
    psycopg = pytest.importorskip("psycopg")
    conninfo = (
        f"host={os.environ['MIGR8_PG_HOST']} port={os.environ['MIGR8_PG_PORT']} "
        f"dbname={os.environ['MIGR8_PG_DB']} user={os.environ['MIGR8_PG_USER']} "
        f"password={os.environ['MIGR8_PG_PASSWORD']}"
    )
    try:
        with psycopg.connect(conninfo, autocommit=True) as connection:
            banner = connection.execute("SELECT version()").fetchone()[0]
    except psycopg.Error as exc:
        pytest.skip(f"PostgreSQL test service is not reachable: {exc}")
    return {
        "conninfo": conninfo,
        "banner": banner,
        "user": os.environ["MIGR8_PG_USER"],
        "schema": os.environ.get("MIGR8_PG_SCHEMA", "migr8"),
    }


@pytest.fixture
def pg_project(tmp_path, pg_settings):
    import psycopg

    schema = pg_settings["schema"]
    with psycopg.connect(pg_settings["conninfo"], autocommit=True) as connection:
        connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        connection.execute(f'CREATE SCHEMA "{schema}"')
    config = support.write(tmp_path / "migr8.toml", f"""
        [database]
        adapter = "postgres"
        dsn = "{pg_settings['conninfo'].replace(
            'password=' + os.environ['MIGR8_PG_PASSWORD'], '').strip()}"
        user = "{pg_settings['user']}"
        target_schema = "{schema}"

        [lock]
        provider = "advisory"
        id = {LOCK_ID}
        timeout_seconds = 20
    """)
    os.environ["MIGR8_PASSWORD"] = os.environ["MIGR8_PG_PASSWORD"]
    yield tmp_path, config, schema
    with psycopg.connect(pg_settings["conninfo"], autocommit=True) as connection:
        connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.fixture
def pg_query(pg_settings):
    import psycopg

    def run(sql: str, params: tuple = ()):
        """Execute a statement through an independent observer connection.

        ``search_path`` is set so unqualified application tables in the test
        schema resolve the way the migrations see them.
        """
        with psycopg.connect(pg_settings["conninfo"], autocommit=True) as connection:
            connection.execute(
                psycopg.sql.SQL("SET search_path = {}, pg_catalog").format(
                    psycopg.sql.Identifier(pg_settings["schema"])
                )
            )
            cursor = connection.execute(sql, params or None)
            if cursor.description is None:
                return []
            return [tuple(row) for row in cursor.fetchall()]

    return run


@pytest.fixture
def oracle_sys(oracle_settings):
    """Administrative access to the explicitly identified disposable test instance."""
    password = os.environ.get("MIGR8_ORACLE_SYS_PASSWORD")
    if not password:
        pytest.skip("MIGR8_ORACLE_SYS_PASSWORD is not set")
    import oracledb

    def run(sql: str, **binds):
        with oracledb.connect(
            user="SYS", password=password, dsn=oracle_settings["dsn"],
            mode=oracledb.AUTH_MODE_SYSDBA,
        ) as connection:
            cursor = connection.cursor()
            cursor.execute(sql, binds)
            if cursor.description is None:
                connection.commit()
                return []
            rows = [tuple(row) for row in cursor.fetchall()]
            connection.commit()
            return rows

    return run
