"""One contract suite, run against every configured adapter.

These are the behaviours the engine relies on and an adapter must therefore get
right. They live in one place so that adding an adapter means filling in a
dialect and running this file, rather than writing a new test module and hoping
it covers the same ground. Drift between adapters shows up here.

The SQLite probe always runs. Oracle and PostgreSQL run when their services are
configured and skip with an explicit reason otherwise; a skip is never treated as
coverage.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

import support
from migr8 import adapters
from migr8.adapters.base import Adapter, OutcomeClass
from migr8.config import load as load_config
from migr8.errors import MetadataDamagedError, UnsupportedCapabilityError, UsageError
from migr8.manifest import Language, Mode
from migr8.model import HISTORY_TABLE, META_TABLE, PROGRESS_TABLE, MetadataState
from migr8.sqltext import normalize

LOCK_ID = 4719  # distinct from the other suites so they never contend


def _sqlite_config(tmp_path):
    return support.sqlite_config(tmp_path, db_path=tmp_path / "build" / "contract.db")


def _oracle_config(tmp_path):
    for name in ("MIGR8_ORACLE_DSN", "MIGR8_ORACLE_USER", "MIGR8_ORACLE_PASSWORD"):
        if not os.environ.get(name):
            pytest.skip(f"Oracle test service not configured: {name} is unset")
    oracledb = pytest.importorskip("oracledb")
    dsn = os.environ["MIGR8_ORACLE_DSN"]
    user = os.environ["MIGR8_ORACLE_USER"].upper()
    password = os.environ["MIGR8_ORACLE_PASSWORD"]
    try:
        with oracledb.connect(user=user, password=password, dsn=dsn) as connection:
            cursor = connection.cursor()
            rows = cursor.execute(
                "SELECT object_name, object_type FROM all_objects WHERE owner = :owner "
                "AND object_type IN ('TABLE','VIEW') ORDER BY object_type DESC",
                owner=user,
            ).fetchall()
            for obj, kind in rows:
                suffix = " CASCADE CONSTRAINTS PURGE" if kind == "TABLE" else ""
                try:
                    cursor.execute(f'DROP {kind} "{user}"."{obj}"{suffix}')
                except oracledb.DatabaseError:
                    pass
            connection.commit()
    except oracledb.Error as exc:
        pytest.skip(f"Oracle test service is not reachable: {exc}")
    os.environ["MIGR8_PASSWORD"] = password
    return support.write(tmp_path / "migr8.toml", f"""
        [database]
        adapter = "oracle"
        dsn = "{dsn}"
        user = "{user}"
        target_schema = "{user}"

        [oracle]
        ddl_lock_timeout_seconds = 10

        [lock]
        provider = "dbms_lock"
        package = "SYS.DBMS_LOCK"
        id = {LOCK_ID}
        timeout_seconds = 20
    """)


def _postgres_config(tmp_path):
    needed = ("MIGR8_PG_HOST", "MIGR8_PG_PORT", "MIGR8_PG_DB",
              "MIGR8_PG_USER", "MIGR8_PG_PASSWORD")
    for name in needed:
        if not os.environ.get(name):
            pytest.skip(f"PostgreSQL test service not configured: {name} is unset")
    psycopg = pytest.importorskip("psycopg")
    schema = os.environ.get("MIGR8_PG_SCHEMA", "migr8")
    conninfo = (
        f"host={os.environ['MIGR8_PG_HOST']} port={os.environ['MIGR8_PG_PORT']} "
        f"dbname={os.environ['MIGR8_PG_DB']} user={os.environ['MIGR8_PG_USER']}"
    )
    password = os.environ["MIGR8_PG_PASSWORD"]
    try:
        with psycopg.connect(f"{conninfo} password={password}", autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.execute(f'CREATE SCHEMA "{schema}"')
    except psycopg.Error as exc:
        pytest.skip(f"PostgreSQL test service is not reachable: {exc}")
    os.environ["MIGR8_PASSWORD"] = password
    return support.write(tmp_path / "migr8.toml", f"""
        [database]
        adapter = "postgres"
        dsn = "{conninfo}"
        user = "{os.environ['MIGR8_PG_USER']}"
        target_schema = "{schema}"

        [lock]
        provider = "advisory"
        id = {LOCK_ID}
        timeout_seconds = 20
    """)


BUILDERS = {
    "sqlite-probe": _sqlite_config,
    "oracle": _oracle_config,
    "postgres": _postgres_config,
}

ADAPTERS = [
    pytest.param("sqlite-probe", marks=pytest.mark.sqlite_probe),
    pytest.param("oracle", marks=pytest.mark.oracle),
    pytest.param("postgres", marks=pytest.mark.postgres),
]


@pytest.fixture(params=ADAPTERS)
def ready(request, tmp_path):
    """A connected, locked, initialized adapter for one engine."""
    config_path = BUILDERS[request.param](tmp_path)
    adapter = adapters.create(load_config(config_path))
    adapter.connect()
    try:
        adapter.acquire_lock()
        adapter.prepare_storage()
        assert adapter.inspect_metadata().state in (
            MetadataState.ABSENT, MetadataState.INCOMPLETE_COMPATIBLE
        )
        adapter.initialize()
        yield adapter
    finally:
        adapter.close()


# --- identity of the shipped set --------------------------------------------------

def test_every_supported_adapter_is_covered_here():
    """A new adapter must appear in this suite, not only in the registry."""
    assert set(adapters.SUPPORTED) == set(BUILDERS)


# --- initialization ----------------------------------------------------------------

def test_initialize_reaches_a_complete_verified_layout(ready: Adapter):
    report = ready.inspect_metadata()
    assert report.state is MetadataState.COMPLETE, report.problems
    assert report.problems == ()
    meta = report.meta
    assert meta is not None
    assert meta.layout_version == 1
    assert meta.adapter == ready.name
    assert meta.target_namespace == ready.normalized_namespace()
    assert meta.lock_binding == ready.lock_binding()


def test_a_fresh_snapshot_is_empty_and_well_typed(ready: Adapter):
    snapshot = ready.read_snapshot(consistent=True)
    assert snapshot.history == ()
    assert snapshot.progress == ()
    assert snapshot.meta is not None
    assert isinstance(snapshot.meta.initialized_at, datetime)


def test_metadata_names_resolve_against_the_real_layout(ready: Adapter):
    """The rendered physical name must be the object initialization created."""
    rows = ready._fetch(f"SELECT count(*) FROM {ready.metadata_name(HISTORY_TABLE)}", {})
    assert rows[0][0] == 0
    column = ready.metadata_column("mode")
    rows = ready._fetch(
        f"SELECT {column} FROM {ready.metadata_name(HISTORY_TABLE)}", {}
    )
    assert rows == []


# --- engine transaction state -------------------------------------------------------

def test_engine_transaction_state_tracks_begin_commit_rollback(ready: Adapter):
    assert not ready.in_engine_transaction
    ready.begin()
    assert ready.in_engine_transaction
    with pytest.raises(UsageError, match="already open"):
        ready.begin()
    ready.rollback()
    assert not ready.in_engine_transaction
    ready.begin()
    ready.commit()
    assert not ready.in_engine_transaction


def test_has_open_transaction_reflects_uncommitted_work(ready: Adapter):
    """It answers "is there uncommitted work", which is not the same question."""
    assert not ready.has_open_transaction()
    ready.begin()
    ready._exec(
        f"INSERT INTO {ready.metadata_name(HISTORY_TABLE)} "
        f"({ready._columns(*Adapter._HISTORY_INSERT_COLUMNS)}) VALUES "
        "(:seq, :migration_id, :fingerprint, :fingerprint, 'sql', 'restartable', "
        "'ACTIVE', 1, {now}, {now}, NULL, NULL, NULL, NULL, NULL, 'contract-test')",
        {"seq": 1, "migration_id": "probe", "fingerprint": "fp1:" + "0" * 64},
    )
    assert ready.has_open_transaction()
    ready.rollback()
    assert not ready.has_open_transaction()
    assert ready.read_snapshot(consistent=True).history == ()


# --- transaction identity ------------------------------------------------------------

def test_transaction_identity_is_stable_within_and_lost_across_a_commit(ready: Adapter):
    ready.begin()
    established = ready.establish_transaction_identity()
    if established is None:
        pytest.skip(f"the {ready.name} adapter states a different enforcement boundary")
    assert ready.read_transaction_identity() == established
    ready.commit()
    after = ready.read_transaction_identity()
    assert after != established


# --- history transitions --------------------------------------------------------------

FP_A = "fp1:" + "a" * 64
FP_B = "fp1:" + "b" * 64


def test_admission_completion_and_progress_lifecycle(ready: Adapter):
    ready.insert_active_row(
        seq=1, migration_id="contract", fingerprint=FP_A, language=Language.PYTHON
    )
    snapshot = ready.read_snapshot(consistent=True)
    assert len(snapshot.history) == 1
    row = snapshot.history[0]
    assert (row.seq, row.migration_id, row.status, row.mode) == (
        1, "contract", "ACTIVE", "restartable"
    )
    assert row.attempt == 1
    assert row.fingerprint == row.first_fingerprint == FP_A
    assert isinstance(row.started_at, datetime)
    assert row.finished_at is None
    assert row.tool_version

    # A checkpoint requires an engine transaction and commits with it.
    with pytest.raises(UsageError, match="open batch transaction"):
        ready.progress_set("contract", "last_id", "1")
    ready.begin()
    ready.progress_set("contract", "last_id", "1")
    ready.progress_set("contract", "last_id", "2")  # upsert, not a duplicate
    ready.commit()
    assert ready.progress_get("contract", "last_id") == "2"
    assert ready.progress_get("contract", "absent") is None
    progress = ready.read_snapshot(consistent=True).progress
    assert [(p.migration_id, p.prog_key, p.prog_value) for p in progress] == [
        ("contract", "last_id", "2")
    ]

    # A later admission changes only what recovery is allowed to change.
    first = ready.read_snapshot(consistent=True).history[0]
    ready.update_active_attempt(
        migration_id="contract", fingerprint=FP_B, language=Language.SQL
    )
    row = ready.read_snapshot(consistent=True).history[0]
    assert row.attempt == 2
    assert row.fingerprint == FP_B
    assert row.language == "sql"
    assert row.first_fingerprint == FP_A, "the first fingerprint is permanent"
    assert row.seq == first.seq
    assert row.mode == first.mode
    assert row.started_at == first.started_at, "the original start time is permanent"
    assert row.status == "ACTIVE"

    # Completion flips the row and deletes the checkpoint in one transaction.
    ready.complete_active_row(migration_id="contract")
    snapshot = ready.read_snapshot(consistent=True)
    row = snapshot.history[0]
    assert row.status == "SUCCESS"
    assert isinstance(row.finished_at, datetime)
    assert row.first_fingerprint == FP_A
    assert snapshot.progress == ()


def test_success_row_is_inserted_inside_the_callers_transaction(ready: Adapter):
    ready.begin()
    ready.insert_success_row(
        seq=1, migration_id="atomic-one", fingerprint=FP_A,
        language=Language.SQL, mode=Mode.ATOMIC,
    )
    # Not visible until the caller commits: the row rides the migration's own
    # transaction, which is the whole point of atomic mode.
    ready.rollback()
    assert ready.read_snapshot(consistent=True).history == ()

    ready.begin()
    ready.insert_success_row(
        seq=1, migration_id="atomic-one", fingerprint=FP_A,
        language=Language.SQL, mode=Mode.ATOMIC,
    )
    ready.commit()
    row = ready.read_snapshot(consistent=True).history[0]
    assert row.status == "SUCCESS"
    assert row.attempt is None, "atomic attempts are not counted"
    assert isinstance(row.finished_at, datetime)


@pytest.mark.parametrize("transition", ["attempt", "completion"])
def test_a_transition_matching_no_active_row_is_damage(ready: Adapter, transition):
    with pytest.raises(MetadataDamagedError, match="exactly one ACTIVE row"):
        if transition == "attempt":
            ready.update_active_attempt(
                migration_id="absent", fingerprint=FP_A, language=Language.SQL
            )
        else:
            ready.complete_active_row(migration_id="absent")
    assert not ready.in_engine_transaction, "the failed transition rolled back"


# --- engine-owned SQL binding ---------------------------------------------------------

def test_bind_narrowing_and_missing_bind_detection(ready: Adapter):
    """Shared SQL is built from a superset of binds; a typo must still fail."""
    rows = ready._fetch(
        f"SELECT count(*) FROM {ready.metadata_name(HISTORY_TABLE)} "
        f"WHERE {ready.metadata_column('migration_id')} = :migration_id",
        {"migration_id": "nothing", "unused": "ignored"},
    )
    assert rows[0][0] == 0
    with pytest.raises(UsageError, match="missing bind values for: migration_id"):
        ready._fetch(
            f"SELECT count(*) FROM {ready.metadata_name(HISTORY_TABLE)} "
            f"WHERE {ready.metadata_column('migration_id')} = :migration_id",
            {"typo": "x"},
        )


class _Dialect:
    """The two attributes ``Adapter._render`` reads, and nothing else.

    Calling the unbound method keeps this a unit test of the renderer rather than
    of any one adapter, and lets it use a dialect no shipped adapter declares.
    """

    def __init__(self, paramstyle: str, now_expression: str) -> None:
        self.paramstyle = paramstyle
        self.now_expression = now_expression


@pytest.mark.parametrize("paramstyle,now,expected", [
    # A cast in the timestamp expression must not be mistaken for a placeholder.
    ("pyformat", "now()::timestamptz", "(%(migration_id)s, now()::timestamptz)"),
    ("pyformat", "clock_timestamp()", "(%(migration_id)s, clock_timestamp())"),
    ("named", "SYSTIMESTAMP", "(:migration_id, SYSTIMESTAMP)"),
])
def test_placeholders_are_translated_before_the_timestamp_expression(
    paramstyle, now, expected
):
    rendered = Adapter._render(
        _Dialect(paramstyle, now), "INSERT INTO t (a, ts) VALUES (:migration_id, {now})"
    )
    assert rendered == f"INSERT INTO t (a, ts) VALUES {expected}"


# --- statement admission --------------------------------------------------------------

def test_forbidden_tokens_are_refused_in_every_context(ready: Adapter):
    policy = ready.statement_policy
    token = "COMMIT" if "COMMIT" in policy.forbidden else sorted(policy.forbidden)[0]
    statement = normalize(f"{token} x" if token != "COMMIT" else "COMMIT")
    for mode, in_batch in ((Mode.ATOMIC, False), (Mode.RESTARTABLE, True),
                           (Mode.RESTARTABLE, False)):
        with pytest.raises(UsageError):
            ready.admit_statement(statement, mode=mode, in_batch=in_batch)


def test_dml_outside_a_batch_is_refused_in_restartable_mode(ready: Adapter):
    statement = normalize("UPDATE probe_table SET col = 1")
    with pytest.raises(UsageError, match="outside a ctx.transaction"):
        ready.admit_statement(statement, mode=Mode.RESTARTABLE, in_batch=False)
    # The same statement is admitted inside a batch and in atomic mode.
    ready.admit_statement(statement, mode=Mode.RESTARTABLE, in_batch=True)
    ready.admit_statement(statement, mode=Mode.ATOMIC, in_batch=False)


def test_queries_are_admitted_outside_a_batch(ready: Adapter):
    ready.admit_statement(
        normalize("SELECT 1 FROM probe_table"), mode=Mode.RESTARTABLE, in_batch=False
    )


@pytest.mark.parametrize("sql", [
    f"DELETE FROM {HISTORY_TABLE}",
    f"DELETE FROM other_schema.{HISTORY_TABLE}",
    f'DELETE FROM "{HISTORY_TABLE.upper()}"',
    f'DELETE FROM "{HISTORY_TABLE}"',
])
def test_reserved_metadata_objects_are_refused(ready: Adapter, sql):
    """Every way of naming the object is a reference, quoted or qualified."""
    with pytest.raises(UsageError, match="reserved metadata object"):
        ready.admit_statement(normalize(sql), mode=Mode.ATOMIC, in_batch=False)
    with pytest.raises(UsageError, match="reserved metadata object"):
        ready.admit_ddl(normalize(f"DROP TABLE {HISTORY_TABLE}"))


#: An application object whose name merely *contains* a reserved one, built by
#: prefixing so the case keeps tracking the constants: "custo" + "m8_history"
#: is "custom8_history". A substring scan refuses these; a token match does not.
_LOOKALIKE_HISTORY = f"custo{HISTORY_TABLE}"
_LOOKALIKE_PROGRESS = f"platfor{PROGRESS_TABLE}"
_LOOKALIKE_META = f"custo{META_TABLE}"


@pytest.mark.parametrize("sql", [
    f"UPDATE {_LOOKALIKE_HISTORY} SET col = 1",
    f"SELECT col FROM {_LOOKALIKE_PROGRESS}",
    # A literal or a comment mentioning the engine table is prose, not a
    # reference; the lexer already separates both from identifiers.
    f"INSERT INTO probe_table (col) VALUES ('see {META_TABLE} for details')",
    f"-- {HISTORY_TABLE} is the engine table; do not touch\nSELECT col FROM probe_table",
])
def test_names_that_merely_contain_a_reserved_name_are_admitted(ready: Adapter, sql):
    ready.admit_statement(normalize(sql), mode=Mode.ATOMIC, in_batch=False)


def test_ddl_naming_a_lookalike_object_is_admitted(ready: Adapter):
    ready.admit_ddl(normalize(f"CREATE TABLE {_LOOKALIKE_META} (id INTEGER)"))


def test_ddl_allow_list_is_enforced(ready: Adapter):
    ready.admit_ddl(normalize("CREATE TABLE probe_ddl (id INTEGER)"))
    with pytest.raises(UsageError, match="DDL allow-list"):
        ready.admit_ddl(normalize("SELECT 1 FROM probe_table"))


def test_required_object_declarations_are_admitted_or_refused_consistently(ready: Adapter):
    """An adapter that refuses declarations must also report them as failures."""
    from migr8.manifest import RequiredObject

    declared = (RequiredObject(type="PACKAGE", name="PKG_PROBE"),)
    capable = ready.capabilities().oracle_style_required_objects
    if capable:
        ready.admit_required_objects(declared)
        result = ready.check_required_objects(declared)
        assert result.failures, "a missing declared object must fail the check"
    else:
        with pytest.raises(UnsupportedCapabilityError):
            ready.admit_required_objects(declared)
        assert ready.check_required_objects(declared).failures
    assert ready.check_required_objects(()).failures == ()


# --- error classification ---------------------------------------------------------------

def test_a_real_server_error_is_classified_as_definite(ready: Adapter):
    """Only a definite failure may be SERVER_REJECTION; this one is definite."""
    try:
        ready._fetch("SELECT * FROM a_table_that_does_not_exist_anywhere", {})
    except Exception as exc:
        assert ready.classify_exception(exc) is OutcomeClass.SERVER_REJECTION
    else:  # pragma: no cover
        pytest.fail("selecting from a missing table should have raised")


def test_an_unrecognised_exception_is_treated_as_unknown(ready: Adapter):
    class Mystery(Exception):
        pass

    assert ready.classify_exception(Mystery()) is OutcomeClass.COMMUNICATION_FAILURE


# --- reporting --------------------------------------------------------------------------

def test_capabilities_and_descriptions_are_populated(ready: Adapter):
    caps = ready.capabilities()
    assert caps.adapter == ready.name
    assert caps.notes, "an adapter must document its own limits"
    assert ready.server_description()
    assert ready.normalized_namespace()
    assert ready.lock_binding()
    verdict, detail = ready.probe_session_liveness(ready.session_identity())
    assert verdict in ("present", "absent", "unknown")
    assert detail
