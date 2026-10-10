"""Driver errors during connection setup and lock acquisition (spec Section 11.3).

Nothing that can commit has been submitted at that point, so no outcome is in
doubt.  Contention exits 5, a lost connection exits 1 as a connection setup
failure, and any other driver error exits 1 with the driver code and without the
driver message.  The PostgreSQL and Oracle cases use fake connections that raise
at one chosen statement: they cover how the adapters classify the error, not
what a server does.
"""

from __future__ import annotations

import json
import sqlite3
import types

import pytest
import support

from migr8 import adapters
from migr8.config import PASSWORD_ENV
from migr8.config import load as load_config
from migr8.engine import Engine
from migr8.errors import ConnectionSetupError, Exit, LockNotAcquiredError, UsageError
from migr8.manifest import load as load_manifest
from migr8.staging import cleanup, stage

SECRET = "secret-token-in-a-driver-message"


def run_engine(config_path, manifest_path):
    config = load_config(config_path)
    adapter = adapters.create(config)
    capture = stage(load_manifest(manifest_path))
    try:
        return Engine(config=config, adapter=adapter, capture=capture).run()
    finally:
        cleanup(capture.staging_root)


# --- SQLite: another connection holds the database ---------------------------------


@pytest.mark.sqlite
def test_sqlite_contention_at_connect_exits_5_for_migrate_and_status(tmp_path, capsys):
    config_path, manifest_path = support.simple_sql_project(tmp_path)
    config_path.write_text(config_path.read_text().replace("= 2000", "= 100"))
    assert support.run_cli(["migrate"], cwd=tmp_path) == Exit.OK
    capsys.readouterr()

    holder = sqlite3.connect(tmp_path / "build" / "probe.db", isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        migrate_code = support.run_cli(["migrate", "--json"], cwd=tmp_path)
        migrate_out = json.loads(capsys.readouterr().out)
        status_code = support.run_cli(["status", "--json"], cwd=tmp_path)
        status_out = json.loads(capsys.readouterr().out)
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert migrate_code == Exit.LOCK_NOT_ACQUIRED
    assert migrate_out["outcome"] == "lock_not_acquired"
    assert "SQLITE_BUSY" in migrate_out["message"]
    assert "uncommitted work" not in migrate_out["message"]
    assert status_code == Exit.LOCK_NOT_ACQUIRED
    assert status_out["outcome"] == "lock_not_acquired"
    assert status_out["phase"] == "connect"


# --- the shared classification -------------------------------------------------------


@pytest.fixture
def sqlite_adapter(tmp_path):
    config_path, _ = support.simple_sql_project(tmp_path)
    return adapters.create(load_config(config_path))


@pytest.mark.sqlite
def test_a_sqlite_error_that_is_not_contention_is_a_usage_error_with_its_code(sqlite_adapter):
    error = sqlite3.OperationalError(f"disk I/O error {SECRET}")
    error.sqlite_errorname = "SQLITE_IOERR"
    result = sqlite_adapter.setup_error(error, "setting up the connection")
    assert type(result) is UsageError
    assert "SQLITE_IOERR" in result.message
    assert SECRET not in result.message


@pytest.mark.sqlite
def test_a_sqlite_busy_error_is_contention(sqlite_adapter):
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorname = "SQLITE_BUSY"
    assert isinstance(
        sqlite_adapter.setup_error(error, "setting up the connection"), LockNotAcquiredError
    )


# --- PostgreSQL: a class 08 error at each setup statement ------------------------------


class FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class FakePostgresConnection:
    """Answers the setup statements and raises *error* at the one named by *fail_on*."""

    def __init__(self, fail_on, error):
        self.fail_on = fail_on
        self.error = error
        self.statements: list[str] = []
        self.closed = False
        self.info = types.SimpleNamespace(transaction_status=None, backend_pid=4242)

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if self.fail_on in sql:
            raise self.error
        if sql.startswith("SHOW synchronous_commit"):
            return FakeCursor(("on",))
        if "pg_namespace" in sql:
            return FakeCursor((1,))
        if "version()" in sql:
            return FakeCursor(("PostgreSQL fake",))
        if "pg_backend_pid" in sql:
            return FakeCursor((4242, "2026-01-01T00:00:00.000000", None))
        if "pg_try_advisory_lock" in sql:
            return FakeCursor((True,))
        return FakeCursor(None)

    def close(self):
        self.closed = True


def postgres_project(tmp_path):
    support.unit(tmp_path, "m1", {"up.sql": "CREATE TABLE IF NOT EXISTS t (id integer);\n"})
    manifest_path = support.manifest(
        tmp_path,
        [
            {
                "id": "create-t",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            }
        ],
    )
    config_path = support.write(
        tmp_path / "migr8.toml",
        """
        [database]
        adapter = "postgres"
        dsn = "host=db.invalid dbname=app"
        target_schema = "migr8"

        [lock]
        provider = "advisory"
        id = 4711
        timeout_seconds = 5
        """,
    )
    return config_path, manifest_path


POSTGRES_STATEMENTS = [
    "SET synchronous_commit",
    "SET search_path",
    "pg_namespace",
    "version()",
    "pg_backend_pid",
    "pg_try_advisory_lock",
]


@pytest.mark.parametrize("fail_on", POSTGRES_STATEMENTS)
def test_postgres_connection_loss_during_setup_exits_1_as_a_setup_failure(
    tmp_path, monkeypatch, fail_on
):
    psycopg = pytest.importorskip("psycopg")
    config_path, manifest_path = postgres_project(tmp_path)
    connection = FakePostgresConnection(
        fail_on, psycopg.errors.ConnectionFailure(f"server closed the connection {SECRET}")
    )
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: connection)
    monkeypatch.delenv(PASSWORD_ENV, raising=False)

    report = run_engine(config_path, manifest_path)

    assert report.exit_code == Exit.USAGE
    assert report.phase == "connect"
    assert "the connection was lost while" in report.message
    assert "SQLSTATE 08006" in report.message
    assert "no outcome is in doubt" in report.message
    assert SECRET not in report.message
    assert not report.connection_discarded
    assert connection.closed
    assert not any(sql.startswith("BEGIN") for sql in connection.statements)


def test_postgres_other_driver_errors_exit_1_with_the_code_not_the_message(tmp_path, monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    config_path, manifest_path = postgres_project(tmp_path)
    connection = FakePostgresConnection(
        "pg_try_advisory_lock", psycopg.errors.InsufficientPrivilege(f"denied {SECRET}")
    )
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: connection)
    monkeypatch.delenv(PASSWORD_ENV, raising=False)

    report = run_engine(config_path, manifest_path)

    assert report.exit_code == Exit.USAGE
    assert "SQLSTATE 42501" in report.message
    assert "connection was lost" not in report.message
    assert SECRET not in report.message


def test_postgres_lock_not_available_during_setup_is_contention(tmp_path, monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    config_path, manifest_path = postgres_project(tmp_path)
    connection = FakePostgresConnection("SET search_path", psycopg.errors.LockNotAvailable("busy"))
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: connection)
    monkeypatch.delenv(PASSWORD_ENV, raising=False)

    report = run_engine(config_path, manifest_path)

    assert report.exit_code == Exit.LOCK_NOT_ACQUIRED


# --- Oracle: ORA-03113 and friends at each setup statement ------------------------------


class FakeOracleCursor:
    def __init__(self, connection):
        self.connection = connection
        self._row = None

    def var(self, _type):
        return types.SimpleNamespace(getvalue=lambda: 0)

    def execute(self, sql, *args, **kwargs):
        self.connection.statements.append(sql)
        if self.connection.fail_on in sql:
            raise self.connection.error
        self._row = None
        if "sys_context" in sql:
            self._row = (1, 2, "FREE", "host")
        elif "serial#" in sql:
            self._row = (7,)
        elif "all_objects" in sql:
            self._row = (1,)
        elif "v$version" in sql:
            self._row = ("Oracle fake",)
        return self

    def fetchone(self):
        return self._row


class FakeOracleConnection:
    thin = True
    version = "23.0"
    transaction_in_progress = False

    def __init__(self, fail_on, error):
        self.fail_on = fail_on
        self.error = error
        self.statements: list[str] = []
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return FakeOracleCursor(self)

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def oracle_error(oracledb, code: int, message: str):
    detail = types.SimpleNamespace(code=code, full_code=f"ORA-{code:05d}", message=message)
    return oracledb.DatabaseError(detail)


def oracle_project(tmp_path):
    support.unit(tmp_path, "m1", {"up.sql": "CREATE TABLE t (id NUMBER)"})
    manifest_path = support.manifest(
        tmp_path,
        [
            {
                "id": "create-t",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            }
        ],
    )
    config_path = support.write(
        tmp_path / "migr8.toml",
        """
        [database]
        adapter = "oracle"
        dsn = "db.invalid:1521/FREEPDB1"
        user = "MIGR8_TEST"

        [lock]
        provider = "dbms_lock"
        package = "SYS.DBMS_LOCK"
        id = 4711
        timeout_seconds = 5
        """,
    )
    return config_path, manifest_path


ORACLE_STATEMENTS = [
    "COMMIT_WAIT",
    "DDL_LOCK_TIMEOUT",
    "all_objects",
    "sys_context",
    "REQUEST",
]


@pytest.mark.parametrize("fail_on", ORACLE_STATEMENTS)
def test_oracle_connection_loss_during_setup_exits_1_as_a_setup_failure(
    tmp_path, monkeypatch, fail_on
):
    oracledb = pytest.importorskip("oracledb")
    config_path, manifest_path = oracle_project(tmp_path)
    connection = FakeOracleConnection(fail_on, oracle_error(oracledb, 3113, f"EOF {SECRET}"))
    monkeypatch.setattr(oracledb, "connect", lambda *args, **kwargs: connection)
    monkeypatch.setenv(PASSWORD_ENV, "synthetic")

    report = run_engine(config_path, manifest_path)

    assert report.exit_code == Exit.USAGE
    assert report.phase == "connect"
    assert "the connection was lost while" in report.message
    assert "ORA-03113" in report.message
    assert "no outcome is in doubt" in report.message
    assert "grants" not in report.message
    assert SECRET not in report.message
    assert connection.closed


def test_oracle_grants_message_only_for_privilege_errors(tmp_path, monkeypatch):
    oracledb = pytest.importorskip("oracledb")
    config_path, manifest_path = oracle_project(tmp_path)
    monkeypatch.setenv(PASSWORD_ENV, "synthetic")

    messages = {}
    for code in (1031, 6550, 942):
        connection = FakeOracleConnection("REQUEST", oracle_error(oracledb, code, SECRET))
        monkeypatch.setattr(
            oracledb, "connect", lambda *args, connection=connection, **kw: connection
        )
        report = run_engine(config_path, manifest_path)
        assert report.exit_code == Exit.USAGE
        assert SECRET not in report.message
        messages[code] = report.message

    assert "Required grants" in messages[1031]
    assert "ORA-01031" in messages[1031]
    assert "Required grants" in messages[6550]
    assert "Required grants" not in messages[942]
    assert "ORA-00942" in messages[942]


def test_oracle_resource_busy_during_setup_is_contention(tmp_path, monkeypatch):
    oracledb = pytest.importorskip("oracledb")
    config_path, manifest_path = oracle_project(tmp_path)
    connection = FakeOracleConnection("DDL_LOCK_TIMEOUT", oracle_error(oracledb, 54, "busy"))
    monkeypatch.setattr(oracledb, "connect", lambda *args, **kwargs: connection)
    monkeypatch.setenv(PASSWORD_ENV, "synthetic")

    report = run_engine(config_path, manifest_path)

    assert report.exit_code == Exit.LOCK_NOT_ACQUIRED


def test_the_connection_setup_error_is_a_usage_error():
    assert issubclass(ConnectionSetupError, UsageError)
    assert ConnectionSetupError.exit_code == Exit.USAGE
