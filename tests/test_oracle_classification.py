"""Oracle outcome classification and the validity checks that depend on it, without a database.

The adapter's transport list is compared with python-oracledb's own table of
session-dead errors, and the required-object checks run against a scripted
cursor that raises driver-shaped errors.
"""

from __future__ import annotations

import pytest
import support

from migr8.adapters.base import OutcomeClass
from migr8.errors import ConfigError, UnknownOutcomeError
from migr8.latch import LatchState, RunLatch
from migr8.manifest import RequiredObject

oracledb = pytest.importorskip("oracledb")
driver_errors = pytest.importorskip("oracledb.errors")

from migr8.adapters.oracle import (  # noqa: E402
    SESSION_DEAD_DPY_CODE,
    TRANSPORT_ORA_CODES,
    OracleAdapter,
    _require_identifier,
)

REQUIRED = (RequiredObject("PACKAGE BODY", "PKG"),)


def settings(tmp_path):
    from migr8.config import load as load_config

    path = support.write(
        tmp_path / "migr8.toml",
        """
        [database]
        adapter = "oracle"
        dsn = "localhost:1521/FREEPDB1"
        user = "MIGR8_TEST"

        [oracle]
        ddl_lock_timeout_seconds = 10

        [lock]
        provider = "dbms_lock"
        package = "SYS.DBMS_LOCK"
        id = 4711
        timeout_seconds = 20
        """,
    )
    return load_config(path)


def ora(code: int, text: str = "server said no") -> Exception:
    """An exception built the way the driver builds one from a server error."""
    error = driver_errors._Error(f"ORA-{code:05}: {text}", code=code)
    return error.exc_type(error)


def session_dead_codes() -> set[int]:
    table = driver_errors.ERR_ORACLE_ERROR_XREF
    return {code for code, mapped in table.items() if mapped == driver_errors.ERR_CONNECTION_CLOSED}


@pytest.fixture
def adapter(tmp_path):
    built = OracleAdapter(settings(tmp_path))
    built.latch = RunLatch()
    return built


# --- the transport list against the driver --------------------------------------------------


def test_the_driver_table_of_session_dead_codes_is_not_empty():
    assert {3113, 12570, 603} <= session_dead_codes()


def test_every_session_dead_code_of_the_driver_is_in_the_transport_list():
    missing = sorted(session_dead_codes() - TRANSPORT_ORA_CODES)
    assert not missing, f"add these ORA numbers to TRANSPORT_ORA_CODES: {missing}"


@pytest.mark.parametrize("code", sorted(session_dead_codes()))
def test_a_session_dead_error_is_a_communication_failure(adapter, code):
    exc = ora(code)
    assert exc.args[0].is_session_dead
    assert exc.args[0].full_code == SESSION_DEAD_DPY_CODE
    assert adapter.classify_exception(exc) is OutcomeClass.COMMUNICATION_FAILURE


def test_a_driver_session_dead_error_is_unknown_even_if_its_ora_number_is_unlisted(adapter):
    """The driver's flag decides when its table gains a code this adapter has not listed."""
    code = max(TRANSPORT_ORA_CODES) + 1000
    error = driver_errors._Error(f"ORA-{code:05}: invented", code=code)
    error.is_session_dead = True
    assert code not in TRANSPORT_ORA_CODES
    assert adapter.classify_exception(error.exc_type(error)) is OutcomeClass.COMMUNICATION_FAILURE


def test_a_closed_connection_without_an_ora_number_is_unknown(adapter):
    exc = driver_errors._create_exception(driver_errors.ERR_CONNECTION_CLOSED)
    assert adapter.classify_exception(exc) is OutcomeClass.COMMUNICATION_FAILURE


@pytest.mark.parametrize("code", [1, 942, 1031, 1400, 2291])
def test_other_server_errors_stay_definite(adapter, code):
    assert adapter.classify_exception(ora(code)) is OutcomeClass.SERVER_REJECTION


# --- fully matched identifiers --------------------------------------------------------------


@pytest.mark.parametrize("value", ["MIGR8\n", "MIGR8_TEST\n"])
def test_an_identifier_with_a_trailing_newline_is_refused(value):
    with pytest.raises(ConfigError):
        _require_identifier(value, "database.target_schema")


def test_a_lock_package_with_a_trailing_newline_is_refused(tmp_path):
    from migr8.config import load as load_config

    path = support.write(
        tmp_path / "migr8.toml",
        """
        [database]
        adapter = "oracle"
        dsn = "localhost:1521/FREEPDB1"
        user = "MIGR8_TEST"

        [lock]
        provider = "dbms_lock"
        package = "SYS.DBMS_LOCK\\n"
        id = 4711
        timeout_seconds = 20
        """,
    )
    with pytest.raises(ConfigError):
        OracleAdapter(load_config(path))


def test_an_ordinary_identifier_is_accepted():
    assert _require_identifier("MIGR8_TEST", "database.target_schema") == "MIGR8_TEST"


# --- validity checks: a lost session is not a validity verdict ------------------------------


class ScriptedCursor:
    def __init__(self, conn: ScriptedConnection) -> None:
        self.conn = conn
        self.rows: list[tuple] = []

    def execute(self, sql, *args, **kwargs):
        self.conn.sent.append(" ".join(sql.split())[:40])
        exc, rows = self.conn.script.pop(0)
        if exc is not None:
            raise exc
        self.rows = rows
        return self

    def fetchall(self):
        return self.rows


class ScriptedConnection:
    def __init__(self, script: list[tuple[Exception | None, list[tuple]]]) -> None:
        self.script = list(script)
        self.sent: list[str] = []

    def cursor(self) -> ScriptedCursor:
        return ScriptedCursor(self)


#: One required-object row: type, name, status, exact_count, other_types, hard, soft.
def _row(status: str, hard: int = 0, soft: int = 0) -> tuple:
    return ("PACKAGE BODY", "PKG", status, 1, None, hard, soft)


def _check(adapter, script):
    conn = ScriptedConnection(script)
    adapter._conn = conn
    return conn, adapter.check_required_objects(REQUIRED)


def _assert_latched_unknown(adapter, conn, executed: int):
    assert adapter.latch.state is LatchState.UNKNOWN_OUTCOME
    assert len(conn.sent) == executed
    with pytest.raises(UnknownOutcomeError):
        adapter.check_required_objects(REQUIRED)
    assert len(conn.sent) == executed, "a latched run must not send more SQL"


def test_a_lost_session_in_the_required_object_query_latches_unknown(adapter):
    conn = ScriptedConnection([(ora(3113), [])])
    adapter._conn = conn
    with pytest.raises(UnknownOutcomeError) as info:
        adapter.check_required_objects(REQUIRED)
    assert info.value.phase == "final_validity"
    _assert_latched_unknown(adapter, conn, executed=1)


@pytest.mark.parametrize(
    "row", [_row("INVALID", hard=1), _row("VALID", soft=1)], ids=["invalid", "warning"]
)
def test_a_lost_session_in_the_compiler_message_lookup_latches_unknown(adapter, row):
    conn = ScriptedConnection([(None, [row]), (ora(3113), [])])
    adapter._conn = conn
    with pytest.raises(UnknownOutcomeError):
        adapter.check_required_objects(REQUIRED)
    _assert_latched_unknown(adapter, conn, executed=2)


def test_a_closed_connection_in_the_required_object_query_latches_unknown(adapter):
    closed = driver_errors._create_exception(driver_errors.ERR_CONNECTION_CLOSED)
    conn = ScriptedConnection([(closed, [])])
    adapter._conn = conn
    with pytest.raises(UnknownOutcomeError):
        adapter.check_required_objects(REQUIRED)
    _assert_latched_unknown(adapter, conn, executed=1)


@pytest.mark.parametrize("code", [942, 1031])
def test_a_rejected_required_object_query_is_a_validity_failure(adapter, code):
    conn, result = _check(adapter, [(ora(code), [])])
    assert len(result.failures) == 1
    assert f"ORA-{code:05}" in result.failures[0]
    assert adapter.latch.state is LatchState.OPEN
    assert len(conn.sent) == 1


@pytest.mark.parametrize("code", [942, 1031])
def test_a_privilege_error_in_the_message_lookup_names_privileges(adapter, code):
    _conn, result = _check(adapter, [(None, [_row("INVALID", hard=1)]), (ora(code), [])])
    assert "not readable with the current privileges" in result.failures[0]
    assert adapter.latch.state is LatchState.OPEN


def test_another_rejection_in_the_message_lookup_names_its_code_not_privileges(adapter):
    _conn, result = _check(adapter, [(None, [_row("INVALID", hard=1)]), (ora(904), [])])
    assert "ORA-00904" in result.failures[0]
    assert "privileges" not in result.failures[0]
    assert adapter.latch.state is LatchState.OPEN
