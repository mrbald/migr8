"""The run latch decides the outcome, on every entry path (spec Section 7.2).

Three rules are tested here. The latch outranks whatever exception finally
reaches the engine, so author code that catches a latched failure cannot
downgrade the run. Every path that reaches the driver on a migration's behalf --
the SQL entry point as well as the Python facade -- classifies its failures the
same way. And so does every other database boundary the run crosses: metadata
reads and writes, the transaction-state probe and ``begin`` are database calls
too, and a lost reply on one of them leaves the same doubt.

The no-SQL-after-uncertainty assertions read the driver, not an adapter method.
A tracer wrapped around one adapter entry point cannot see the metadata
statements, the transaction probe or a rollback, so an empty record from one
would not establish the claim.

As in ``test_unknown_outcome``, the transport loss is simulated by replacing an
adapter method. These are wrapper simulations, not transport evidence; directional
evidence is produced against Oracle and PostgreSQL in ``tests/integration``.
"""

from __future__ import annotations

import pytest
import support

from migr8.errors import Exit

pytestmark = pytest.mark.sqlite_probe


class SimulatedTransportLoss(Exception):
    """Not a ``sqlite3.Error``, so the adapter classifies it as a lost transport."""


@pytest.fixture
def project(tmp_path):
    db = tmp_path / "build" / "probe.db"
    return tmp_path, support.sqlite_config(tmp_path, db_path=db), db


def _three_migrations(root, *, victim_language, victim_mode, victim_body):
    """A victim migration between a table it can use and a migration that must not run."""
    entry = "up.sql" if victim_language == "sql" else "up.py"
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER);\n"})
    support.unit(root, "m2", {entry: victim_body})
    support.unit(root, "m3", {"up.sql": "INSERT INTO t (id) VALUES (99);\n"})
    return support.manifest(
        root,
        [
            {
                "id": "create-t",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
            {
                "id": "victim",
                "path": "m2",
                "language": victim_language,
                "mode": victim_mode,
                "entry": entry,
            },
            {"id": "later", "path": "m3", "language": "sql", "mode": "atomic", "entry": "up.sql"},
        ],
    )


class _TracingConnection:
    """A sqlite3 connection that records every statement the adapter submits.

    The tracer sits at the driver, below every adapter entry point, so metadata
    statements, the transaction probe, ``BEGIN``, ``COMMIT`` and ``ROLLBACK``
    are all visible. It also arms a break: some database boundaries carry no
    statement of their own, so the break is triggered by a statement already
    submitted rather than by the failing call's arguments.
    """

    def __init__(self, real, record: dict, armed_by):
        needle, occurrence = armed_by if isinstance(armed_by, tuple) else (armed_by, 1)
        self._real, self._record = real, record
        self._needle, self._remaining = needle, occurrence

    def __getattr__(self, name):
        return getattr(self._real, name)

    def _note(self, statement: str) -> None:
        if self._record["failed"]:
            self._record["sql_after"].append(" ".join(statement.split()))
        elif self._needle is not None and self._needle in statement:
            self._remaining -= 1
            self._record["armed"] = self._remaining <= 0

    def execute(self, sql, *args):
        self._note(str(sql))
        return self._real.execute(sql, *args)

    def executemany(self, sql, *args):
        self._note(str(sql))
        return self._real.executemany(sql, *args)

    def commit(self):
        self._note("<driver commit>")
        return self._real.commit()

    def rollback(self):
        self._note("<driver rollback>")
        return self._real.rollback()


def _observe(record: dict, adapter, armed_by=None) -> None:
    """Trace the driver and record which teardown the run chose."""
    record.setdefault("failed", False)
    record.setdefault("armed", armed_by is None)
    record.setdefault("sql_after", [])
    record.setdefault("teardown", None)
    real_connect, real_close, real_discard = adapter.connect, adapter.close, adapter.discard

    def traced_connect():
        real_connect()
        adapter._conn = _TracingConnection(adapter._conn, record, armed_by)

    def traced_close():
        record["teardown"] = "close"
        return real_close()

    def traced_discard():
        record["teardown"] = "discard"
        return real_discard()

    adapter.connect = traced_connect
    adapter.close, adapter.discard = traced_close, traced_discard


def _break(record: dict, adapter, method: str, needle: str, failure: type[BaseException]):
    """Make one adapter method fail when its argument matches, and trace what follows."""
    record.clear()
    _observe(record, adapter)
    original = getattr(adapter, method)

    def broken(*args, **kwargs):
        subject = getattr(args[0], "text", args[0]) if args else ""
        if needle in str(subject):
            record["failed"] = True
            raise failure("simulated loss of the reply")
        return original(*args, **kwargs)

    setattr(adapter, method, broken)


def _break_after(record: dict, adapter, method: str, *, armed_by, failure):
    """Make one adapter method fail on its first call after *armed_by* is submitted.

    ``armed_by`` is a statement substring, optionally paired with which
    occurrence of it to arm on, or ``None`` to break the first call of all. This
    is how the boundaries that take no statement of their own -- the
    transaction-state probe, ``begin`` -- are reached.
    """
    record.clear()
    _observe(record, adapter, armed_by)
    original = getattr(adapter, method)

    def broken(*args, **kwargs):
        if record["armed"] and not record["failed"]:
            record["failed"] = True
            raise failure("simulated loss of the reply")
        return original(*args, **kwargs)

    setattr(adapter, method, broken)


# --- F1: a caught latched failure cannot be downgraded -------------------------------


ATOMIC_CATCH_AND_RETURN = """\
def migrate(ctx):
    ctx.execute("INSERT INTO t (id) VALUES (1)")
    try:
        ctx.query("SELECT id FROM t")
    except Exception:
        pass
"""

ATOMIC_CATCH_AND_RETHROW = """\
def migrate(ctx):
    ctx.execute("INSERT INTO t (id) VALUES (1)")
    try:
        ctx.query("SELECT id FROM t")
    except Exception:
        raise RuntimeError("swallowed and replaced with my own failure")
"""

RESTARTABLE_CATCH_AND_RETURN = """\
def migrate(ctx):
    with ctx.transaction():
        ctx.execute("INSERT INTO t (id) VALUES (1)")
    try:
        ctx.query("SELECT id FROM t")
    except Exception:
        pass
"""

RESTARTABLE_CATCH_AND_RETHROW = """\
def migrate(ctx):
    with ctx.transaction():
        ctx.execute("INSERT INTO t (id) VALUES (1)")
    try:
        ctx.query("SELECT id FROM t")
    except Exception:
        raise RuntimeError("swallowed and replaced with my own failure")
"""


@pytest.mark.parametrize(
    ("mode", "body"),
    [
        ("atomic", ATOMIC_CATCH_AND_RETURN),
        ("atomic", ATOMIC_CATCH_AND_RETHROW),
        ("restartable", RESTARTABLE_CATCH_AND_RETURN),
        ("restartable", RESTARTABLE_CATCH_AND_RETHROW),
    ],
    ids=["atomic-return", "atomic-rethrow", "restartable-return", "restartable-rethrow"],
)
def test_a_caught_unknown_outcome_still_ends_the_run_as_unknown(project, mode, body):
    root, config, db = project
    manifest = _three_migrations(root, victim_language="python", victim_mode=mode, victim_body=body)
    state: dict = {}
    report = support.migrate_report(
        config,
        manifest,
        adapter_hook=lambda a: _break(
            state, a, "query", "SELECT id FROM t", SimulatedTransportLoss
        ),
    )
    assert report.exit_code == Exit.UNKNOWN_OUTCOME
    assert report.connection_discarded
    assert state["teardown"] == "discard"
    # No SQL at all after an unknown outcome: no rollback, no probe, no cleanup.
    assert state["sql_after"] == []
    # The victim never reaches SUCCESS and the migration after it never starts.
    assert report.executed == ["create-t"]
    recorded = {row[1]: row[2] for row in support.history(db)}
    assert recorded.get("victim") != "SUCCESS"
    assert "later" not in recorded


def test_an_ordinary_failure_is_still_an_ordinary_failure(project):
    """The latch changes nothing when nothing was latched."""
    root, config, db = project
    body = 'def migrate(ctx):\n    raise ValueError("plain failure")\n'
    manifest = _three_migrations(
        root, victim_language="python", victim_mode="atomic", victim_body=body
    )
    report = support.migrate_report(config, manifest)
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert not report.connection_discarded
    assert [row[1] for row in support.history(db)] == ["create-t"]


def test_a_contract_violation_is_still_reported_as_one(project):
    """Exit 8 survives author code replacing the exception with its own."""
    root, config, db = project
    body = """\
def migrate(ctx):
    ctx.execute("INSERT INTO t (id) VALUES (1)")
"""
    manifest = _three_migrations(
        root, victim_language="python", victim_mode="atomic", victim_body=body
    )

    def hook(adapter):
        # Break the transaction underneath the migration, as a stray commit would.
        real_execute = adapter.execute

        def execute(statement, params):
            result = real_execute(statement, params)
            adapter.commit()
            return result

        adapter.execute = execute

    report = support.migrate_report(config, manifest, adapter_hook=hook)
    assert report.exit_code == Exit.CONTRACT_VIOLATION
    assert not report.connection_discarded
    assert "victim" not in {row[1] for row in support.history(db)}


# --- F2: every entry path classifies the same way ------------------------------------


SQL_DDL_ENTRY = "CREATE TABLE widget (id INTEGER PRIMARY KEY);\n"

PY_DDL_ENTRY = """\
def migrate(ctx):
    ctx.ddl("CREATE TABLE widget (id INTEGER PRIMARY KEY)")
"""


@pytest.mark.parametrize(("language", "body"), [("sql", SQL_DDL_ENTRY), ("python", PY_DDL_ENTRY)])
@pytest.mark.parametrize("failure", [SimulatedTransportLoss, KeyboardInterrupt])
def test_a_lost_reply_during_ddl_is_unknown_on_both_entry_paths(project, language, body, failure):
    """Spec Section 7.2: a lost reply, or an interruption, inside commit-capable DDL."""
    root, config, db = project
    manifest = _three_migrations(
        root, victim_language=language, victim_mode="restartable", victim_body=body
    )
    state: dict = {}
    report = support.migrate_report(
        config,
        manifest,
        adapter_hook=lambda a: _break(state, a, "_run", "CREATE TABLE widget", failure),
    )
    assert report.exit_code == Exit.UNKNOWN_OUTCOME
    assert report.connection_discarded
    assert state["teardown"] == "discard"
    assert state["sql_after"] == []
    assert report.phase == "restartable_ddl"
    assert report.failed_migration == "victim"
    assert "later" not in {row[1] for row in support.history(db)}


def test_a_lost_reply_in_an_atomic_sql_entry_is_unknown(project):
    """A statement the engine's transaction owns can still lose its transport."""
    root, config, db = project
    manifest = _three_migrations(
        root,
        victim_language="sql",
        victim_mode="atomic",
        victim_body="INSERT INTO t (id) VALUES (1);\n",
    )
    state: dict = {}
    report = support.migrate_report(
        config,
        manifest,
        adapter_hook=lambda a: _break(
            state, a, "_run", "INSERT INTO t (id) VALUES (1)", SimulatedTransportLoss
        ),
    )
    assert report.exit_code == Exit.UNKNOWN_OUTCOME
    assert state["sql_after"] == []
    assert "victim" not in {row[1] for row in support.history(db)}


@pytest.mark.parametrize(
    ("mode", "body"),
    [
        (
            "atomic",
            'def migrate(ctx):\n    ctx.execute("INSERT INTO t (id) VALUES (1)")\n',
        ),
        (
            "restartable",
            "def migrate(ctx):\n"
            "    with ctx.transaction():\n"
            '        ctx.execute("INSERT INTO t (id) VALUES (1)")\n',
        ),
    ],
    ids=["atomic", "inside-a-batch"],
)
def test_an_interruption_outside_a_commit_capable_call_stays_exit_three(project, mode, body):
    """Spec Section 7.2: nothing durable is in doubt, so the ordinary rules apply."""
    root, config, db = project
    manifest = _three_migrations(root, victim_language="python", victim_mode=mode, victim_body=body)
    state: dict = {}
    report = support.migrate_report(
        config,
        manifest,
        adapter_hook=lambda a: _break(
            state, a, "_run", "INSERT INTO t (id) VALUES (1)", KeyboardInterrupt
        ),
    )
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert not report.connection_discarded
    assert state["teardown"] == "close"
    assert "victim" not in {row[1] for row in support.history(db) if row[2] == "SUCCESS"}


def test_a_known_rejection_is_not_promoted_to_unknown(project):
    """A definite server rejection keeps exit 3 and a normal close."""
    root, config, db = project
    manifest = _three_migrations(
        root,
        victim_language="sql",
        victim_mode="restartable",
        victim_body="CREATE TABLE t (id INTEGER);\n",  # t already exists
    )
    report = support.migrate_report(config, manifest)
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert not report.connection_discarded
    assert {row[1]: row[2] for row in support.history(db)}["victim"] == "ACTIVE"


# --- R1: every database boundary latches, not just migration execution ---------------

BATCH_CATCH_AND_RETURN = """\
def migrate(ctx):
    try:
        with ctx.transaction():
            ctx.execute("INSERT INTO t (id) VALUES (1)")
    except Exception:
        pass
    ctx.query("SELECT id FROM t")
"""

BATCH_CATCH_AND_RETHROW = """\
def migrate(ctx):
    try:
        with ctx.transaction():
            ctx.execute("INSERT INTO t (id) VALUES (1)")
    except Exception:
        raise RuntimeError("swallowed and replaced with my own failure")
"""

#: The second ACTIVE admission is the victim's, so the next call to the broken
#: boundary is the one the victim's own batch makes.  ``create-t`` is admitted
#: the same way first, and the statement text is identical, so the occurrence
#: count is what separates them.
VICTIM_ADMITTED = ('INSERT INTO "m8_history"', 2)


@pytest.mark.parametrize(
    "body", [BATCH_CATCH_AND_RETURN, BATCH_CATCH_AND_RETHROW], ids=["return", "rethrow"]
)
@pytest.mark.parametrize(
    ("boundary", "method"),
    [
        ("transaction-state probe", "_has_open_transaction"),
        ("begin", "_do_begin"),
    ],
)
def test_a_lost_reply_at_batch_entry_is_unknown(project, body, boundary, method):
    """Entering a batch asks the database twice before any author SQL runs.

    Neither call carries migration SQL, and on Oracle the transaction-state
    probe is a real round trip, so a lost reply on either leaves the same doubt
    as one during execution (spec Section 7.2).
    """
    root, config, db = project
    manifest = _three_migrations(
        root, victim_language="python", victim_mode="restartable", victim_body=body
    )
    state: dict = {}
    report = support.migrate_report(
        config,
        manifest,
        adapter_hook=lambda a: _break_after(
            state, a, method, armed_by=VICTIM_ADMITTED, failure=SimulatedTransportLoss
        ),
    )
    assert state["failed"], f"the {boundary} was never reached"
    assert report.exit_code == Exit.UNKNOWN_OUTCOME
    assert report.connection_discarded
    assert state["teardown"] == "discard"
    assert state["sql_after"] == []
    assert report.failed_migration is None or report.failed_migration == "victim"
    recorded = {row[1]: row[2] for row in support.history(db)}
    assert recorded.get("victim") == "ACTIVE"
    assert "later" not in recorded


def test_a_lost_reply_writing_the_success_row_is_unknown(project):
    """The review's first probe: a lost reply during the victim's SUCCESS insertion.

    Engine-owned metadata SQL reaches the same driver as migration SQL. Reading
    this as an ordinary failure would roll back and close a connection whose
    commit may already have been durable.
    """
    root, config, db = project
    manifest = _three_migrations(
        root,
        victim_language="python",
        victim_mode="atomic",
        victim_body='def migrate(ctx):\n    ctx.execute("INSERT INTO t (id) VALUES (1)")\n',
    )
    state: dict = {}
    report = support.migrate_report(
        config,
        manifest,
        adapter_hook=lambda a: _break_after(
            state,
            a,
            "_metadata_execute",
            armed_by="INSERT INTO t (id) VALUES (1)",
            failure=SimulatedTransportLoss,
        ),
    )
    assert state["failed"]
    assert report.exit_code == Exit.UNKNOWN_OUTCOME
    assert report.connection_discarded
    assert state["teardown"] == "discard"
    # No rollback either: the driver saw nothing at all after the failure.
    assert state["sql_after"] == []
    assert "victim" not in {row[1] for row in support.history(db)}


def test_a_lost_reply_reading_the_snapshot_is_unknown(project):
    """Planning reads metadata over the same transport execution uses."""
    root, config, db = project
    manifest = _three_migrations(
        root,
        victim_language="sql",
        victim_mode="atomic",
        victim_body="INSERT INTO t (id) VALUES (1);\n",
    )
    state: dict = {}
    report = support.migrate_report(
        config,
        manifest,
        adapter_hook=lambda a: _break_after(
            state, a, "_read_snapshot", armed_by=None, failure=SimulatedTransportLoss
        ),
    )
    assert report.exit_code == Exit.UNKNOWN_OUTCOME
    assert report.connection_discarded
    assert state["teardown"] == "discard"
    assert state["sql_after"] == []
    assert report.phase == "metadata_read"
    assert support.history(db) == []


def test_a_lost_reply_inspecting_the_namespace_is_unknown(project):
    """Inspection runs before anything is created and is a database call too."""
    root, config, db = project
    manifest = _three_migrations(
        root,
        victim_language="sql",
        victim_mode="atomic",
        victim_body="INSERT INTO t (id) VALUES (1);\n",
    )
    state: dict = {}
    report = support.migrate_report(
        config,
        manifest,
        adapter_hook=lambda a: _break_after(
            state, a, "_objects_present", armed_by=None, failure=SimulatedTransportLoss
        ),
    )
    assert report.exit_code == Exit.UNKNOWN_OUTCOME
    assert report.connection_discarded
    assert state["teardown"] == "discard"
    assert state["sql_after"] == []
    assert report.phase == "metadata_inspection"
    assert (
        not db.exists()
        or support.db_query(db, "SELECT name FROM sqlite_master WHERE name LIKE 'm8_%'") == []
    )


def test_a_definite_rejection_at_a_metadata_boundary_stays_exit_three(project):
    """The guard classifies; it does not promote every metadata failure."""
    import sqlite3

    root, config, db = project
    manifest = _three_migrations(
        root,
        victim_language="python",
        victim_mode="atomic",
        victim_body='def migrate(ctx):\n    ctx.execute("INSERT INTO t (id) VALUES (1)")\n',
    )
    state: dict = {}
    report = support.migrate_report(
        config,
        manifest,
        adapter_hook=lambda a: _break_after(
            state,
            a,
            "_metadata_execute",
            armed_by="INSERT INTO t (id) VALUES (1)",
            failure=sqlite3.OperationalError,
        ),
    )
    assert state["failed"]
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert not report.connection_discarded
    assert state["teardown"] == "close"
    assert "victim" not in {row[1] for row in support.history(db)}
