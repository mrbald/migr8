"""Thick-mode failover refusal, checked with a fake connection (spec Section 12, invariant 6).

Oracle Client can fail a Thick-mode session over to another instance without an
error, which drops the DBMS_LOCK and the session settings.  After connecting in
Thick mode the adapter reads the session's failover type and refuses anything
but NONE.  The fake answers the setup statements; it does not model a server.
"""

from __future__ import annotations

import types

import pytest
import support

from migr8.config import PASSWORD_ENV
from migr8.config import load as load_config
from migr8.errors import ConnectionSetupError

oracledb = pytest.importorskip("oracledb")

FAILOVER_SQL = "failover_type"


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self._row = None

    def execute(self, sql, *args, **kwargs):
        self.connection.statements.append(sql)
        self._row = None
        if FAILOVER_SQL in sql:
            if self.connection.failover_error is not None:
                raise self.connection.failover_error
            self._row = self.connection.failover_row
        elif "sys_context" in sql:
            self._row = (1, 2, "FREE", "host")
        elif "serial#" in sql:
            self._row = (7,)
        elif "all_objects" in sql:
            self._row = (1,)
        elif "v$version" in sql:
            self._row = ("Oracle fake",)
        elif "v$parameter" in sql:
            self._row = ("IMMEDIATE",)
        return self

    def fetchone(self):
        return self._row


class Connection:
    version = "23.0"
    transaction_in_progress = False

    def __init__(self, *, thin, failover_row=("NONE", "NONE"), failover_error=None):
        self.thin = thin
        self.failover_row = failover_row
        self.failover_error = failover_error
        self.statements: list[str] = []
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return Cursor(self)

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def adapter(tmp_path, monkeypatch, connection, *, thick):
    from migr8.adapters import oracle

    path = support.write(
        tmp_path / "migr8.toml",
        f"""
        [database]
        adapter = "oracle"
        dsn = "localhost:1521/FREEPDB1"
        user = "MIGR8_TEST"

        [oracle]
        allow_thick_mode = {"true" if thick else "false"}

        [lock]
        provider = "dbms_lock"
        package = "SYS.DBMS_LOCK"
        id = 4711
        timeout_seconds = 20
        """,
    )
    monkeypatch.setenv(PASSWORD_ENV, "synthetic")
    monkeypatch.setattr(oracle, "_enable_thick_mode", lambda lib_dir, config_dir: None)
    monkeypatch.setattr(oracledb, "connect", lambda *args, **kwargs: connection)
    return oracle.OracleAdapter(load_config(path))


def ran_failover_query(connection) -> bool:
    return any(FAILOVER_SQL in statement for statement in connection.statements)


@pytest.mark.parametrize("failover_type", ["SELECT", "SESSION", "TRANSACTION", None])
def test_thick_mode_with_a_failover_type_is_refused(tmp_path, monkeypatch, failover_type):
    connection = Connection(thin=False, failover_row=(failover_type, "BASIC"))
    built = adapter(tmp_path, monkeypatch, connection, thick=True)

    with pytest.raises(ConnectionSetupError, match="transparent failover") as raised:
        built.connect()

    assert raised.value.phase == "connect"
    assert connection.closed
    assert not any("COMMIT_WAIT" in statement for statement in connection.statements)


def test_thick_mode_with_failover_type_none_proceeds(tmp_path, monkeypatch):
    connection = Connection(thin=False, failover_row=("NONE", "NONE"))
    built = adapter(tmp_path, monkeypatch, connection, thick=True)

    built.connect()

    assert ran_failover_query(connection)
    assert not connection.closed
    assert built.session_identity() is not None


def test_thin_mode_never_reads_the_failover_type(tmp_path, monkeypatch):
    connection = Connection(thin=True, failover_row=("SELECT", "BASIC"))
    built = adapter(tmp_path, monkeypatch, connection, thick=False)

    built.connect()

    assert not ran_failover_query(connection)


@pytest.mark.parametrize("code", [942, 1031])
def test_thick_mode_without_read_access_to_v_session_names_the_grant(tmp_path, monkeypatch, code):
    detail = types.SimpleNamespace(code=code, full_code=f"ORA-{code:05d}", message="denied")
    connection = Connection(thin=False, failover_error=oracledb.DatabaseError(detail))
    built = adapter(tmp_path, monkeypatch, connection, thick=True)

    with pytest.raises(ConnectionSetupError, match=r"SYS\.V_\$SESSION") as raised:
        built.connect()

    assert f"ORA-{code:05d}" in raised.value.message
    assert connection.closed
