"""PostgreSQL integration: the second adapter's own transactional behaviour.

These validate the state machine and PostgreSQL behaviour. They do not
substitute for Oracle's DDL, PL/SQL, lock or commit-outcome evidence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import support

from migr8.errors import Exit

pytestmark = [pytest.mark.postgres]

ENTRY = Path(__file__).resolve().parents[2] / "migr8"
TIMEOUT = 120


# --- initialization ----------------------------------------------------------------


def test_initialization_creates_and_validates_the_layout(pg_project, pg_query):
    root, config, schema = pg_project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE orders (id integer PRIMARY KEY)"})
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            }
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    assert pg_query(
        f"SELECT layout_version, adapter, lock_provider, lock_binding, target_namespace "
        f"FROM {schema}.m8_meta"
    ) == [(1, "postgres", "advisory", "advisory:4711", schema)]
    assert sorted(
        row[0]
        for row in pg_query("SELECT tablename FROM pg_tables WHERE schemaname = %s", (schema,))
    ) == ["m8_history", "m8_meta", "m8_progress", "orders"]


def test_one_active_partial_unique_index_is_enforced_by_the_database(pg_project, pg_query):
    root, config, schema = pg_project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE orders (id integer PRIMARY KEY)"})
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            }
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    import psycopg

    def insert_active(seq, name):
        pg_query(
            f"INSERT INTO {schema}.m8_history (seq, migration_id, fingerprint, "
            "first_fingerprint, language, mode, status, attempt, started_at, "
            "tool_version) VALUES (%s, %s, 'fp1:0', 'fp1:0', 'sql', 'restartable', "
            "'ACTIVE', 1, clock_timestamp(), 'test')",
            (seq, name),
        )

    insert_active(2, "second")
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_active(3, "third")


def test_read_only_commands_do_not_initialize(pg_project, pg_query):
    root, config, schema = pg_project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE orders (id integer)"})
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            }
        ],
    )
    report = support.report_for("status", config, manifest)
    assert report.exit_code == Exit.NOT_INITIALIZED
    assert pg_query("SELECT count(*) FROM pg_tables WHERE schemaname = %s", (schema,)) == [(0,)]


def test_synchronous_commit_is_verified_by_session_read_back(pg_project):
    root, config, schema = pg_project
    from migr8 import adapters
    from migr8.config import load as load_config

    adapter = adapters.create(load_config(config))
    adapter.connect()
    try:
        notes = " ".join(adapter.capabilities().notes)
        assert "VERIFIED 'on' by session read-back" in notes
    finally:
        adapter.close()


def test_populated_history_without_a_marker_is_damage(pg_project, pg_query):
    root, config, schema = pg_project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE orders (id integer)"})
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            }
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    pg_query(f"DELETE FROM {schema}.m8_meta")
    assert support.migrate(config, manifest) == Exit.METADATA_DAMAGED


def test_incompatible_column_layout_is_damage(pg_project, pg_query):
    root, config, schema = pg_project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE orders (id integer)"})
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            }
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    pg_query(f"ALTER TABLE {schema}.m8_history ADD COLUMN surprise text")
    assert support.migrate(config, manifest) == Exit.METADATA_DAMAGED


# --- atomic with transactional DDL ---------------------------------------------------


def test_transactional_ddl_is_admitted_in_atomic_mode(pg_project, pg_query):
    """PostgreSQL DDL is transactional, so the adapter admits it explicitly."""
    root, config, schema = pg_project
    support.unit(
        root, "m1", {"up.sql": ("CREATE TABLE orders (id integer PRIMARY KEY, region text)")}
    )
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "atomic",
                "entry": "up.sql",
            }
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    assert pg_query(
        "SELECT count(*) FROM pg_tables WHERE schemaname = %s AND tablename = 'orders'",
        (schema,),
    ) == [(1,)]
    assert pg_query(f"SELECT mode FROM {schema}.m8_history") == [("atomic",)]


def test_atomic_ddl_failure_rolls_the_whole_transaction_back(pg_project, pg_query):
    """Unlike Oracle, a failed DDL here leaves nothing behind."""
    root, config, schema = pg_project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE orders (id integer PRIMARY KEY)"})
    support.unit(
        root,
        "m2",
        {
            "migration.py": (
                "def migrate(ctx):\n"
                "    ctx.execute('CREATE TABLE extra (id integer)')\n"
                "    ctx.execute('CREATE TABLE orders (id integer)')\n"
            )
        },
    )
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "atomic",
                "entry": "up.sql",
            },
            {
                "id": "ddl-fail",
                "path": "m2",
                "language": "python",
                "mode": "atomic",
                "entry": "migration.py",
            },
        ],
    )
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    assert pg_query(
        "SELECT count(*) FROM pg_tables WHERE schemaname = %s AND tablename = 'extra'",
        (schema,),
    ) == [(0,)]
    assert pg_query(f"SELECT migration_id FROM {schema}.m8_history") == [("create-orders",)]


def test_postgres_itself_refuses_a_commit_inside_a_do_block(pg_project, pg_query):
    """PostgreSQL stops this before the tripwire has to: the work rolls back."""
    root, config, schema = pg_project
    support.unit(
        root, "m1", {"up.sql": ("CREATE TABLE orders (id integer PRIMARY KEY, region text)")}
    )
    support.unit(
        root,
        "m2",
        {
            "up.sql": (
                "DO $$ BEGIN\n"
                "  INSERT INTO orders (id, region) VALUES (1, 'EU');\n"
                "  COMMIT;\n"
                "END $$"
            )
        },
    )
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "atomic",
                "entry": "up.sql",
            },
            {
                "id": "violator",
                "path": "m2",
                "language": "sql",
                "mode": "atomic",
                "entry": "up.sql",
            },
        ],
    )
    report = support.migrate_report(config, manifest)
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert pg_query("SELECT count(*) FROM orders") == [(0,)]
    assert pg_query(f"SELECT migration_id FROM {schema}.m8_history") == [("create-orders",)]


def test_transaction_identity_guard_is_real(pg_project):
    """Drive the adapter's guard directly: a committed transaction loses its id."""
    root, config, schema = pg_project
    from migr8 import adapters
    from migr8.config import load as load_config

    adapter = adapters.create(load_config(config))
    adapter.connect()
    try:
        adapter.begin()
        established = adapter.establish_transaction_identity()
        assert established and adapter.read_transaction_identity() == established
        adapter.commit()
        assert adapter.read_transaction_identity() is None
        adapter.begin()
        again = adapter.establish_transaction_identity()
        assert again != established
    finally:
        adapter.close()


def test_engine_reports_a_contract_violation_when_the_transaction_changes(pg_project, pg_query):
    """A routine that commits mid-migration is caught before SUCCESS is written.

    The commit is injected by wrapping the adapter's own ``execute``, which is a
    stand-in for a non-compliant stored routine. The commit itself is real.
    """
    root, config, schema = pg_project
    support.unit(
        root, "m1", {"up.sql": ("CREATE TABLE orders (id integer PRIMARY KEY, region text)")}
    )
    support.unit(root, "m2", {"up.sql": "INSERT INTO orders (id, region) VALUES (1, 'EU')"})
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "atomic",
                "entry": "up.sql",
            },
            {
                "id": "violator",
                "path": "m2",
                "language": "sql",
                "mode": "atomic",
                "entry": "up.sql",
            },
        ],
    )

    def hook(adapter):
        real_execute = adapter.execute

        def execute(statement, params):
            result = real_execute(statement, params)
            if "orders" in statement.text and "INSERT" in statement.text.upper():
                adapter.commit()
            return result

        adapter.execute = execute

    report = support.migrate_report(config, manifest, adapter_hook=hook)
    assert report.exit_code == Exit.CONTRACT_VIOLATION
    assert "broke its transaction" in report.message
    assert "manual remediation" in report.message
    # The row is durable because the injected commit really committed; no
    # success row was written for it.
    assert pg_query("SELECT count(*) FROM orders") == [(1,)]
    assert pg_query(f"SELECT migration_id FROM {schema}.m8_history") == [("create-orders",)]


# --- restartable --------------------------------------------------------------------

BACKFILL = """\
def migrate(ctx):
    last = int(ctx.progress.get("last_id", "0"))
    while True:
        with ctx.transaction() as tx:
            rows = tx.query(
                "SELECT id FROM orders WHERE id > %s AND region IS NULL "
                "ORDER BY id LIMIT %s",
                (last, 4),
            )
            if not rows:
                break
            ids = [row[0] for row in rows]
            changed = tx.executemany(
                "UPDATE orders SET region = %s WHERE id = %s AND region IS NULL",
                [("EU", key) for key in ids],
            )
            if changed != len(ids):
                raise RuntimeError("batch membership changed")
            last = max(ids)
            ctx.progress.set("last_id", str(last))
        ctx.log("batch committed", last_id=last)
"""


def _backfill_project(root, body=BACKFILL):
    support.unit(
        root, "m1", {"up.sql": ("CREATE TABLE orders (id integer PRIMARY KEY, region text)")}
    )
    support.unit(
        root,
        "m2",
        {
            "up.sql": (
                "INSERT INTO orders (id, region) SELECT g, NULL FROM generate_series(1, 10) g"
            )
        },
    )
    support.unit(root, "m3", {"migration.py": body})
    return support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "atomic",
                "entry": "up.sql",
            },
            {"id": "seed", "path": "m2", "language": "sql", "mode": "atomic", "entry": "up.sql"},
            {
                "id": "backfill",
                "path": "m3",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
        ],
    )


def test_restartable_batches_commit_with_their_checkpoint(pg_project, pg_query):
    root, config, schema = pg_project
    manifest = _backfill_project(root)
    assert support.migrate(config, manifest) == Exit.OK
    assert pg_query("SELECT count(*) FROM orders WHERE region = 'EU'") == [(10,)]
    assert pg_query(f"SELECT count(*) FROM {schema}.m8_progress") == [(0,)]


def test_failure_retains_active_and_the_checkpoint(pg_project, pg_query):
    root, config, schema = pg_project
    failing = BACKFILL.replace(
        '        ctx.log("batch committed", last_id=last)',
        "        if last >= 8:\n"
        '            raise RuntimeError("stop")\n'
        '        ctx.log("batch committed", last_id=last)',
    )
    manifest = _backfill_project(root, failing)
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    assert pg_query("SELECT count(*) FROM orders WHERE region = 'EU'") == [(8,)]
    assert pg_query(f"SELECT prog_key, prog_value FROM {schema}.m8_progress") == [("last_id", "8")]
    support.unit(root, "m3", {"migration.py": BACKFILL})
    assert support.migrate(config, manifest) == Exit.VALIDATION
    assert support.migrate(config, manifest, recover="backfill") == Exit.OK
    assert pg_query("SELECT count(*) FROM orders WHERE region = 'EU'") == [(10,)]


SWALLOWED_ERROR = """\
import psycopg


def migrate(ctx):
    with ctx.transaction() as tx:
        tx.execute("UPDATE orders SET region = 'EU' WHERE id = 1")
        ctx.progress.set("last_id", "1")
        try:
            tx.execute("INSERT INTO orders (id) VALUES (2)")
        except psycopg.errors.UniqueViolation:
            ctx.log("duplicate ignored")
"""


def test_a_batch_that_caught_a_statement_error_is_not_committed(pg_project, pg_query):
    """The server aborted the transaction, so COMMIT would only roll it back."""
    root, config, schema = pg_project
    manifest = _backfill_project(root, SWALLOWED_ERROR)
    report = support.migrate_report(config, manifest)
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert pg_query(
        f"SELECT status, attempt FROM {schema}.m8_history WHERE migration_id = 'backfill'"
    ) == [("ACTIVE", 1)]
    assert pg_query("SELECT count(*) FROM orders WHERE region IS NOT NULL") == [(0,)]
    assert pg_query(f"SELECT count(*) FROM {schema}.m8_progress") == [(0,)]


CAUGHT_AT_BATCH_EXIT = """\
import psycopg


def migrate(ctx):
    try:
        with ctx.transaction() as tx:
            tx.execute("UPDATE orders SET region = 'EU' WHERE id = 1")
            ctx.progress.set("last_id", "1")
            try:
                tx.execute("INSERT INTO orders (id) VALUES (2)")
            except psycopg.errors.UniqueViolation:
                ctx.log("duplicate ignored")
    except Exception as exc:
        ctx.log("batch failure ignored", error=type(exc).__name__)
"""


def test_catching_the_batch_exit_failure_does_not_let_the_migration_complete(pg_project, pg_query):
    root, config, schema = pg_project
    manifest = _backfill_project(root, CAUGHT_AT_BATCH_EXIT)
    report = support.migrate_report(config, manifest)
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert "batch transaction could not be checked before commit" in report.message
    assert pg_query(
        f"SELECT status, attempt FROM {schema}.m8_history WHERE migration_id = 'backfill'"
    ) == [("ACTIVE", 1)]
    # The seed's ten rows, none updated by the batch.
    assert pg_query("SELECT count(*), count(region) FROM orders") == [(10, 0)]
    assert pg_query(f"SELECT count(*) FROM {schema}.m8_progress") == [(0,)]


def test_a_deferred_constraint_failing_at_batch_commit_exits_three(pg_project, pg_query):
    root, config, schema = pg_project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE parent (id integer PRIMARY KEY)"})
    support.unit(
        root,
        "m2",
        {
            "up.sql": (
                "CREATE TABLE child (id integer PRIMARY KEY, parent_id integer "
                "REFERENCES parent (id) DEFERRABLE INITIALLY DEFERRED)"
            )
        },
    )
    support.unit(
        root,
        "m3",
        {
            "migration.py": (
                "def migrate(ctx):\n"
                "    with ctx.transaction() as tx:\n"
                '        tx.execute("INSERT INTO child (id, parent_id) VALUES (1, 99)")\n'
                '        ctx.progress.set("linked", "1")\n'
            )
        },
    )
    manifest = support.manifest(
        root,
        [
            {"id": "parent", "path": "m1", "language": "sql", "mode": "atomic", "entry": "up.sql"},
            {"id": "child", "path": "m2", "language": "sql", "mode": "atomic", "entry": "up.sql"},
            {
                "id": "link",
                "path": "m3",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
        ],
    )
    report = support.migrate_report(config, manifest)
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert "batch commit was rejected" in report.message
    assert "SQLSTATE 23503" in report.message
    assert pg_query(f"SELECT status FROM {schema}.m8_history WHERE migration_id = 'link'") == [
        ("ACTIVE",)
    ]
    assert pg_query("SELECT count(*) FROM child") == [(0,)]
    assert pg_query(f"SELECT count(*) FROM {schema}.m8_progress") == [(0,)]


def test_commit_refuses_an_aborted_transaction(pg_project):
    """Adapter level: a COMMIT on an aborted transaction raises a definite rejection."""
    import psycopg

    from migr8 import adapters
    from migr8.adapters.base import BatchTransactionState, Boundary, OutcomeClass
    from migr8.config import load as load_config
    from migr8.errors import ContractViolationError
    from migr8.latch import RunLatch

    root, config, schema = pg_project
    adapter = adapters.create(load_config(config))
    adapter.latch = RunLatch()
    adapter.connect()
    try:
        adapter.begin()
        with pytest.raises(psycopg.errors.DivisionByZero):
            adapter._db.execute("SELECT 1/0")
        assert adapter.batch_transaction_state() is BatchTransactionState.OPEN
        with pytest.raises(psycopg.errors.InFailedSqlTransaction) as info:
            adapter.durable_commit(Boundary.RESTARTABLE_BATCH)
        assert adapter.classify_exception(info.value) is OutcomeClass.SERVER_REJECTION
        assert not adapter.latch.latched
        # The rollback that follows a latched violation does not pass the latch.
        adapter.latch.latch_violation(ContractViolationError("test violation"))
        adapter.rollback()
        assert adapter.batch_transaction_state() is BatchTransactionState.ENDED
    finally:
        adapter.close()


HIDDEN_BOUNDARY = """\
def migrate(ctx):
    with ctx.transaction() as tx:
        tx.execute("UPDATE orders SET region = 'EU' WHERE id = 1")
        ctx.progress.set("last_id", "1")
    with ctx.transaction() as tx:
        tx.execute("UPDATE orders SET region = 'EU' WHERE id = 2")
"""


def test_a_hidden_transaction_boundary_inside_a_batch_is_a_violation(pg_project, pg_query):
    """The boundary is injected by wrapping the adapter's ``execute``; the commit is real."""
    root, config, schema = pg_project
    manifest = _backfill_project(root, HIDDEN_BOUNDARY)

    def hook(adapter):
        real_execute = adapter.execute

        def execute(statement, params):
            result = real_execute(statement, params)
            if "id = 1" in statement.text:
                adapter._db.execute("COMMIT")
                adapter._db.execute("BEGIN")
            return result

        adapter.execute = execute

    report = support.migrate_report(config, manifest, adapter_hook=hook)
    assert report.exit_code == Exit.CONTRACT_VIOLATION
    assert "broke its transaction" in report.message
    assert "batch transaction" in report.message
    assert "manual remediation" in report.message
    assert pg_query(f"SELECT status FROM {schema}.m8_history WHERE migration_id = 'backfill'") == [
        ("ACTIVE",)
    ]
    # The update before the hidden commit is durable; the checkpoint after it
    # was rolled back, and the second batch never ran.
    assert pg_query("SELECT id FROM orders WHERE region = 'EU'") == [(1,)]
    assert pg_query(f"SELECT count(*) FROM {schema}.m8_progress") == [(0,)]


def test_create_index_concurrently_belongs_to_a_restartable_migration(pg_project, pg_query):
    """It cannot run in a transaction block, so the adapter runs it outside one."""
    root, config, schema = pg_project
    support.unit(
        root, "m1", {"up.sql": ("CREATE TABLE orders (id integer PRIMARY KEY, region text)")}
    )
    support.unit(
        root,
        "m2",
        {
            "migration.py": (
                "def migrate(ctx):\n"
                "    existing = ctx.query(\n"
                '        "SELECT count(*) FROM pg_indexes '
                "WHERE indexname = 'orders_region_idx'\"\n"
                "    )[0][0]\n"
                "    if not existing:\n"
                "        ctx.ddl('CREATE INDEX CONCURRENTLY "
                "orders_region_idx ON orders (region)')\n"
            )
        },
    )
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "atomic",
                "entry": "up.sql",
            },
            {
                "id": "concurrent-index",
                "path": "m2",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    assert pg_query(
        "SELECT indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = 'orders_region_idx'"
    ) == [(True,)]


def test_concurrent_index_creation_is_refused_in_atomic_mode(pg_project, pg_query):
    root, config, schema = pg_project
    support.unit(
        root, "m1", {"up.sql": ("CREATE TABLE orders (id integer PRIMARY KEY, region text)")}
    )
    support.unit(
        root, "m2", {"up.sql": ("CREATE INDEX CONCURRENTLY orders_region_idx ON orders (region)")}
    )
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "atomic",
                "entry": "up.sql",
            },
            {"id": "bad", "path": "m2", "language": "sql", "mode": "atomic", "entry": "up.sql"},
        ],
    )
    assert support.migrate(config, manifest) == Exit.USAGE


def test_oracle_style_required_objects_are_rejected(pg_project):
    root, config, schema = pg_project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE orders (id integer)"})
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "atomic",
                "entry": "up.sql",
                "require_valid": [{"name": "PKG", "type": "PACKAGE"}],
            }
        ],
    )
    assert support.migrate(config, manifest) == Exit.USAGE


def test_oracle_plsql_is_not_accepted(pg_project):
    root, config, schema = pg_project
    support.unit(root, "m1", {"up.sql": "BEGIN NULL; END;"})
    manifest = support.manifest(
        root,
        [
            {
                "id": "bad",
                "path": "m1",
                "language": "sql",
                "mode": "atomic",
                "entry": "up.sql",
            }
        ],
    )
    assert support.migrate(config, manifest) == Exit.USAGE


def _restartable_entry_manifest(root, entries):
    """A manifest of SQL units, each ``(id, path, mode)`` with entry ``up.sql``."""
    return support.manifest(
        root,
        [
            {"id": uid, "path": path, "language": "sql", "mode": mode, "entry": "up.sql"}
            for uid, path, mode in entries
        ],
    )


def test_a_do_block_applies_as_a_restartable_sql_entry(pg_project, pg_query):
    """A restartable DO file is admitted as a procedural block and runs once."""
    root, config, schema = pg_project
    support.unit(
        root, "m1", {"up.sql": "CREATE TABLE orders (id integer PRIMARY KEY, region text)"}
    )
    support.unit(
        root,
        "m2",
        {
            "up.sql": (
                "DO $$\nBEGIN\n"
                "  INSERT INTO orders (id, region) VALUES (1, 'EU') ON CONFLICT DO NOTHING;\n"
                "END\n$$;\n"
            )
        },
    )
    manifest = _restartable_entry_manifest(
        root, [("create-orders", "m1", "atomic"), ("seed-orders", "m2", "restartable")]
    )
    assert support.migrate(config, manifest) == Exit.OK
    assert pg_query("SELECT id, region FROM orders") == [(1, "EU")]
    assert pg_query(f"SELECT migration_id, status FROM {schema}.m8_history ORDER BY seq") == [
        ("create-orders", "SUCCESS"),
        ("seed-orders", "SUCCESS"),
    ]


def test_a_call_applies_as_a_restartable_sql_entry(pg_project, pg_query):
    root, config, schema = pg_project
    # One statement per file: the table, the procedure and the call are separate units.
    support.unit(root, "m2", {"up.sql": "CALL seed_orders();\n"})
    support.unit(
        root, "m0", {"up.sql": "CREATE TABLE orders (id integer PRIMARY KEY, region text)"}
    )
    support.unit(
        root,
        "m1",
        {
            "up.sql": (
                "CREATE PROCEDURE seed_orders() LANGUAGE sql AS $$\n"
                "  INSERT INTO orders (id, region) VALUES (1, 'EU') ON CONFLICT DO NOTHING\n"
                "$$;\n"
            )
        },
    )
    manifest = _restartable_entry_manifest(
        root,
        [
            ("create-orders", "m0", "restartable"),
            ("create-seed", "m1", "restartable"),
            ("call-seed", "m2", "restartable"),
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    assert pg_query("SELECT id, region FROM orders") == [(1, "EU")]


def test_a_do_block_naming_a_reserved_object_is_refused(pg_project, pg_query):
    root, config, schema = pg_project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE orders (id integer PRIMARY KEY)"})
    support.unit(root, "m2", {"up.sql": "DO $$ BEGIN DELETE FROM m8_history; END $$;\n"})
    manifest = _restartable_entry_manifest(
        root, [("create-orders", "m1", "atomic"), ("wipe", "m2", "restartable")]
    )
    assert support.migrate(config, manifest) == Exit.USAGE
    # Preflight refuses the plan before connecting, so nothing was created.
    assert pg_query("SELECT count(*) FROM pg_tables WHERE schemaname = %s", (schema,)) == [(0,)]


# --- concurrency and inspection --------------------------------------------------------

SLOW = """\
import pathlib
import time

READY = pathlib.Path({ready!r})
GO = pathlib.Path({go!r})


def migrate(ctx):
    with ctx.transaction() as tx:
        tx.execute("INSERT INTO orders (id, region) VALUES (100, 'EU')")
        ctx.progress.set("step", "one")
    READY.write_text("holding the advisory lock across a commit")
    deadline = time.monotonic() + 90
    while not GO.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    with ctx.transaction() as tx:
        tx.execute("INSERT INTO orders (id, region) VALUES (101, 'EU')")
        ctx.progress.set("step", "two")
"""


def test_session_advisory_lock_survives_commits_and_excludes_others(pg_project, pg_query):
    root, config, schema = pg_project
    ready, go = root / "ready", root / "go"
    support.unit(
        root, "m1", {"up.sql": ("CREATE TABLE orders (id integer PRIMARY KEY, region text)")}
    )
    support.unit(root, "m2", {"migration.py": SLOW.format(ready=str(ready), go=str(go))})
    support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "atomic",
                "entry": "up.sql",
            },
            {
                "id": "slow",
                "path": "m2",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
        ],
    )
    support.write(
        root / "zero.toml",
        config.read_text().replace("timeout_seconds = 20", "timeout_seconds = 0"),
    )
    env = {**os.environ, "MIGR8_PASSWORD": os.environ["MIGR8_PG_PASSWORD"]}
    holder = subprocess.Popen(
        [sys.executable, str(ENTRY), "migrate"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        deadline = time.monotonic() + 60
        while not ready.exists():
            assert time.monotonic() < deadline, "the holder never reached the marker"
            time.sleep(0.05)
        # One batch has already committed, yet the session-level lock is still held.
        assert pg_query("SELECT id FROM orders ORDER BY id") == [(100,)]
        assert pg_query(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = 4711 AND granted"
        ) == [(1,)]
        contender = subprocess.run(
            [sys.executable, str(ENTRY), "migrate", "--config", "zero.toml"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            env=env,
        )
        assert contender.returncode == Exit.LOCK_NOT_ACQUIRED
        # Read-only inspection does not take the lock.
        inspect = subprocess.run(
            [sys.executable, str(ENTRY), "status", "--json"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            env=env,
        )
        assert inspect.returncode == Exit.OK
        report = json.loads(inspect.stdout)
        entry = next(m for m in report["migrations"] if m["id"] == "slow")
        assert entry["state"] == "ACTIVE"
        assert entry["session_liveness"] == "present"
        assert "reusable" in entry["session_liveness_detail"]
    finally:
        go.write_text("continue")
        holder.wait(timeout=TIMEOUT)
    assert holder.returncode == Exit.OK
    assert pg_query("SELECT id FROM orders ORDER BY id") == [(100,), (101,)]
    assert pg_query(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = 4711"
    ) == [(0,)]
