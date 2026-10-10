"""PostgreSQL adapter behavior that needs no server.

``psycopg.connect`` is intercepted, so these cover which arguments the driver
receives and which SQL the adapter sends, not what a server does with them.
The credentials below are synthetic.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from migr8.adapters.base import OutcomeClass  # noqa: E402
from migr8.adapters.postgres import PostgresAdapter  # noqa: E402
from migr8.config import PASSWORD_ENV, Config, LockConfig  # noqa: E402
from migr8.errors import ConfigError, UnknownOutcomeError, UsageError  # noqa: E402
from migr8.latch import RunLatch  # noqa: E402


def make_config(dsn: str, user: str | None = None) -> Config:
    return Config(
        path=None,
        adapter="postgres",
        dsn=dsn,
        user=user,
        target_schema="migr8",
        lock=LockConfig("advisory", 5, id=4711),
    )


class Intercepted:
    """Records psycopg.connect calls and fails them, so no session is built."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise psycopg.OperationalError("intercepted")


@pytest.fixture
def connect_calls(monkeypatch):
    intercepted = Intercepted()
    monkeypatch.setattr(psycopg, "connect", intercepted)
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    return intercepted


def connect(config: Config) -> None:
    with pytest.raises(UsageError, match="cannot connect to PostgreSQL"):
        PostgresAdapter(config).connect()


# --- credentials travel as keyword arguments ---------------------------------------------------


def test_database_user_reaches_the_driver_as_a_keyword(connect_calls, monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, "s3cret")
    connect(make_config("host=db.example port=5432 dbname=app", user="migrator"))
    assert connect_calls.calls == [
        (
            ("host=db.example port=5432 dbname=app",),
            {"autocommit": True, "user": "migrator", "password": "s3cret"},
        )
    ]


def test_no_credentials_leaves_the_libpq_fallbacks_alone(connect_calls):
    connect(make_config("host=db.example dbname=app"))
    assert connect_calls.calls == [(("host=db.example dbname=app",), {"autocommit": True})]


@pytest.mark.parametrize(
    "password",
    ["correct horse", "x host=other.invalid", "a\\b", "it's", "p=q"],
)
def test_a_password_is_never_spliced_into_the_dsn(connect_calls, monkeypatch, password):
    monkeypatch.setenv(PASSWORD_ENV, password)
    dsn = "host=db.example dbname=app user=migrator"
    connect(make_config(dsn))
    assert connect_calls.calls == [((dsn,), {"autocommit": True, "password": password})]


def test_a_uri_dsn_takes_the_password_as_a_keyword(connect_calls, monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, "s3cret")
    dsn = "postgresql://migrator@db.example:5432/app"
    connect(make_config(dsn))
    assert connect_calls.calls == [((dsn,), {"autocommit": True, "password": "s3cret"})]


def test_equal_users_in_the_config_and_the_dsn_are_accepted(connect_calls):
    dsn = "host=db.example dbname=app user=migrator"
    connect(make_config(dsn, user="migrator"))
    assert connect_calls.calls == [((dsn,), {"autocommit": True, "user": "migrator"})]


def test_conflicting_users_are_a_configuration_error(connect_calls):
    with pytest.raises(ConfigError) as info:
        PostgresAdapter(make_config("host=db.example user=dsnuser", user="cfguser"))
    message = str(info.value)
    assert "database.user" in message and "database.dsn" in message
    assert "dsnuser" not in message and "cfguser" not in message
    assert info.value.exit_code == 1
    assert connect_calls.calls == []


@pytest.mark.parametrize(
    "dsn",
    [
        "host=db.example dbname=app password=hunter2",
        "postgresql://migrator:hunter2@db.example/app",
        "postgresql://db.example/app?password=hunter2",
    ],
)
def test_a_dsn_with_a_password_is_a_configuration_error(connect_calls, dsn):
    with pytest.raises(ConfigError) as info:
        PostgresAdapter(make_config(dsn))
    assert PASSWORD_ENV in str(info.value)
    assert "hunter2" not in str(info.value)
    assert info.value.exit_code == 1
    assert connect_calls.calls == []


def test_a_malformed_dsn_error_does_not_repeat_the_dsn():
    with pytest.raises(ConfigError) as info:
        PostgresAdapter(make_config("host=db.example not-a-pair"))
    assert "not-a-pair" not in str(info.value)


def test_a_connect_failure_does_not_report_the_password(connect_calls, monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, "s3cret")
    with pytest.raises(UsageError) as info:
        PostgresAdapter(make_config("host=db.example", user="migrator")).connect()
    assert "s3cret" not in str(info.value)


# --- a fake session for the SQL the adapter sends ----------------------------------------------


class FakeResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class FakeInfo:
    transaction_status = psycopg.pq.TransactionStatus.IDLE


class FakeConnection:
    def __init__(self, fail_on: str | None = None, error: Exception | None = None) -> None:
        self.sent: list[str] = []
        self.info = FakeInfo()
        self._fail_on = fail_on
        self._error = error

    def execute(self, sql: str, params=None):
        self.sent.append(sql)
        if self._fail_on is not None and self._fail_on in sql:
            assert self._error is not None
            raise self._error
        if sql.startswith("SHOW synchronous_commit"):
            return FakeResult([("on",)])
        if "pg_namespace" in sql:
            return FakeResult([(1,)])
        if "COUNT(*)" in sql:
            return FakeResult([(0,)])
        return FakeResult([])


def adapter_with(connection: FakeConnection) -> PostgresAdapter:
    adapter = PostgresAdapter(make_config("host=db.example dbname=app"))
    adapter._conn = connection  # ``_db`` is a read-only property over this
    return adapter


# --- the search path names only the target schema ----------------------------------------------


def test_the_search_path_names_only_the_target_schema():
    """pg_catalog is searched first unless it is listed, and listing it first would
    make it the default creation schema for a migration's unqualified DDL."""
    connection = FakeConnection()
    adapter_with(connection)._configure_session()
    assert 'SET search_path = "migr8"' in connection.sent
    assert not [sql for sql in connection.sent if "search_path" in sql and "pg_catalog" in sql]


# --- when the snapshot read sends its COMMIT ---------------------------------------------------


def test_a_connection_failure_during_the_read_sends_no_commit():
    error = psycopg.errors.ConnectionException()
    assert error.sqlstate.startswith("08")
    connection = FakeConnection(fail_on="FROM", error=error)
    adapter = adapter_with(connection)
    adapter.latch = RunLatch()
    with pytest.raises(UnknownOutcomeError):
        adapter.read_snapshot(consistent=True)
    assert connection.sent[0].startswith("BEGIN ISOLATION LEVEL REPEATABLE READ")
    assert "COMMIT" not in connection.sent
    assert adapter.latch.unknown


def test_an_interrupt_during_the_read_sends_no_commit():
    connection = FakeConnection(fail_on="FROM", error=KeyboardInterrupt())
    adapter = adapter_with(connection)
    with pytest.raises(KeyboardInterrupt):
        adapter._read_snapshot(True)
    assert "COMMIT" not in connection.sent


def test_a_latched_run_sends_no_commit():
    connection = FakeConnection()
    adapter = adapter_with(connection)
    adapter.latch = RunLatch()
    latched = UnknownOutcomeError("earlier loss", operation="commit", phase="metadata")
    adapter.latch.latch_unknown(latched)
    with pytest.raises(UnknownOutcomeError):
        adapter._read_snapshot(True)
    assert "COMMIT" not in connection.sent


def test_a_server_rejection_during_the_read_still_ends_the_transaction():
    connection = FakeConnection(fail_on="FROM", error=psycopg.errors.UndefinedTable())
    adapter = adapter_with(connection)
    with pytest.raises(psycopg.errors.UndefinedTable):
        adapter._read_snapshot(True)
    assert connection.sent[-1] == "COMMIT"


def test_a_successful_read_commits_the_snapshot_transaction():
    connection = FakeConnection()
    snapshot = adapter_with(connection)._read_snapshot(True)
    assert snapshot.history == () and snapshot.progress == ()
    assert connection.sent[-1] == "COMMIT"


# --- which SQLSTATEs leave the outcome unknown -------------------------------------------------

UNKNOWN_FOR_EVERY_CALL = ["08006", "08003", "40003"]
UNKNOWN_FOR_COMMIT_CAPABLE_CALLS = ["57P01", "57P02", "57P03", "58030", "58000", "XX000", "XX001"]
DEFINITE_FOR_EVERY_CALL = ["23505", "42P01", "40001", "40P01", "22012", "25P02", "53100", "57014"]


def simulated(sqlstate: str) -> Exception:
    return psycopg.errors.lookup(sqlstate)("simulated")


@pytest.mark.parametrize("commit_capable", [True, False])
@pytest.mark.parametrize("sqlstate", UNKNOWN_FOR_EVERY_CALL)
def test_connection_loss_and_40003_leave_every_outcome_unknown(sqlstate, commit_capable):
    adapter = PostgresAdapter(make_config("host=db.example dbname=app"))
    exc = simulated(sqlstate)
    outcome = adapter.classify_call_failure(exc, commit_capable=commit_capable)
    assert outcome is OutcomeClass.COMMUNICATION_FAILURE
    assert adapter.classify_exception(exc) is OutcomeClass.COMMUNICATION_FAILURE


@pytest.mark.parametrize("sqlstate", UNKNOWN_FOR_COMMIT_CAPABLE_CALLS)
def test_classes_57_58_and_xx_are_unknown_only_for_a_commit_capable_call(sqlstate):
    adapter = PostgresAdapter(make_config("host=db.example dbname=app"))
    exc = simulated(sqlstate)
    committing = adapter.classify_call_failure(exc, commit_capable=True)
    assert committing is OutcomeClass.COMMUNICATION_FAILURE
    other = adapter.classify_call_failure(exc, commit_capable=False)
    assert other is OutcomeClass.SERVER_REJECTION
    assert adapter.classify_exception(exc) is OutcomeClass.COMMUNICATION_FAILURE


@pytest.mark.parametrize("commit_capable", [True, False])
@pytest.mark.parametrize("sqlstate", DEFINITE_FOR_EVERY_CALL)
def test_ordinary_sqlstates_stay_definite(sqlstate, commit_capable):
    adapter = PostgresAdapter(make_config("host=db.example dbname=app"))
    exc = simulated(sqlstate)
    outcome = adapter.classify_call_failure(exc, commit_capable=commit_capable)
    assert outcome is OutcomeClass.SERVER_REJECTION
    assert adapter.classify_exception(exc) is OutcomeClass.SERVER_REJECTION


def test_a_failed_wal_write_during_commit_latches_unknown():
    adapter = PostgresAdapter(make_config("host=db.example dbname=app"))
    adapter.latch = RunLatch()

    def commit():
        raise simulated("58030")

    with pytest.raises(UnknownOutcomeError):
        adapter.guarded(commit, operation="commit", phase="final", commit_capable=True)
    assert adapter.latch.unknown


def test_a_shutdown_during_a_call_that_cannot_commit_is_a_definite_rejection():
    adapter = PostgresAdapter(make_config("host=db.example dbname=app"))
    adapter.latch = RunLatch()

    def query():
        raise simulated("57P01")

    with pytest.raises(psycopg.errors.AdminShutdown):
        adapter.guarded(query, operation="query", phase="migration", commit_capable=False)
    assert not adapter.latch.latched


def test_a_connection_loss_during_a_call_that_cannot_commit_still_latches_unknown():
    adapter = PostgresAdapter(make_config("host=db.example dbname=app"))
    adapter.latch = RunLatch()

    def query():
        raise simulated("08006")

    with pytest.raises(UnknownOutcomeError):
        adapter.guarded(query, operation="query", phase="migration", commit_capable=False)
    assert adapter.latch.unknown
