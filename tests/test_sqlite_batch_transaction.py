"""A SQLite batch whose transaction a statement rolled back (spec Sections 5.2 and 7.1).

``INSERT OR ROLLBACK`` and an ``ON CONFLICT ROLLBACK`` constraint roll back the
whole transaction while the statement error reaches author code, which may
catch it.  Every later statement and checkpoint in the batch would then
autocommit on its own, so the facade refuses them and the run stops with exit 3,
the migration ACTIVE.
"""

from __future__ import annotations

import sqlite3

import pytest
import support

from migr8 import adapters
from migr8.adapters.base import BatchTransactionState, Boundary, OutcomeClass
from migr8.config import load as load_config
from migr8.errors import Exit
from migr8.latch import RunLatch

pytestmark = pytest.mark.sqlite

#: Each variant ends the transaction on a duplicate of the seeded row 0.
VARIANTS = {
    "insert-or-rollback": (
        "CREATE TABLE orders (id INTEGER PRIMARY KEY)",
        "INSERT OR ROLLBACK INTO orders (id) VALUES (0)",
    ),
    "on-conflict-rollback": (
        "CREATE TABLE orders (id INTEGER PRIMARY KEY ON CONFLICT ROLLBACK)",
        "INSERT INTO orders (id) VALUES (0)",
    ),
}

#: The conflicting statement runs on the first attempt only, so the rerun
#: shows what the batch writes when its transaction stays open.
SWALLOW = """\
def migrate(ctx):
    if ctx.progress.get("batch1"):
        return
    with ctx.transaction() as tx:
        tx.execute("INSERT INTO orders (id) VALUES (1)")
        if ctx.attempt == 1:
            try:
                tx.execute("{conflict}")
            except Exception as exc:
                ctx.log("duplicate ignored", error=type(exc).__name__)
        tx.execute("INSERT INTO orders (id) VALUES (2)")
        ctx.progress.set("batch1", "done")
"""


@pytest.fixture
def project(tmp_path):
    db = tmp_path / "build" / "probe.db"
    config = support.sqlite_config(tmp_path, db_path=db)
    return tmp_path, config, db


def _project(root, create: str, body: str):
    support.unit(root, "m1", {"up.sql": create})
    support.unit(root, "m2", {"up.sql": "INSERT INTO orders (id) VALUES (0)"})
    support.unit(root, "m3", {"migration.py": body})
    return support.manifest(
        root,
        [
            {
                "id": "create",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
            {"id": "seed", "path": "m2", "language": "sql", "mode": "atomic", "entry": "up.sql"},
            {
                "id": "swallow",
                "path": "m3",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
        ],
    )


def _state(db):
    history = [(row[1], row[2], row[5]) for row in support.history(db)]
    orders = support.db_query(db, "SELECT id FROM orders ORDER BY id")
    return history, support.progress(db), orders


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_a_rolled_back_batch_stops_the_run_and_the_rerun_inserts_the_rows(project, variant):
    root, config, db = project
    create, conflict = VARIANTS[variant]
    manifest = _project(root, create, SWALLOW.format(conflict=conflict))

    report = support.migrate_report(config, manifest)
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert (
        "the database rolled the batch transaction back before ctx.execute(); "
        "the batch is not committed"
    ) in report.message
    assert "manual remediation" not in report.message
    history, progress, orders = _state(db)
    assert history[-1] == ("swallow", "ACTIVE", 1)
    # Neither the later INSERT nor the checkpoint committed on its own.
    assert progress == []
    assert orders == [(0,)]

    assert support.migrate(config, manifest) == Exit.OK
    history, progress, orders = _state(db)
    assert history[-1] == ("swallow", "SUCCESS", 2)
    assert orders == [(0,), (1,), (2,)]


ENDS_AT_EXIT = """\
def migrate(ctx):
    with ctx.transaction() as tx:
        tx.execute("INSERT INTO orders (id) VALUES (1)")
        ctx.progress.set("batch1", "done")
        try:
            tx.execute("INSERT OR ROLLBACK INTO orders (id) VALUES (0)")
        except Exception:
            pass
{tail}
"""


def test_a_batch_rolled_back_without_another_call_fails_at_batch_exit(project):
    root, config, db = project
    manifest = _project(root, VARIANTS["insert-or-rollback"][0], ENDS_AT_EXIT.format(tail=""))
    report = support.migrate_report(config, manifest)
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert (
        "the database rolled the batch transaction back before the batch ended; "
        "the batch is not committed"
    ) in report.message
    history, progress, orders = _state(db)
    assert history[-1] == ("swallow", "ACTIVE", 1)
    assert progress == []
    assert orders == [(0,)]


UNCAUGHT = """\
def migrate(ctx):
    with ctx.transaction() as tx:
        tx.execute("INSERT INTO orders (id) VALUES (1)")
        ctx.progress.set("batch1", "done")
        tx.execute("INSERT OR ROLLBACK INTO orders (id) VALUES (0)")
"""


def test_an_uncaught_conflict_that_rolls_the_batch_back_is_an_ordinary_failure(project):
    root, config, db = project
    manifest = _project(root, VARIANTS["insert-or-rollback"][0], UNCAUGHT)
    report = support.migrate_report(config, manifest)
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert "sqlite3.IntegrityError" in report.message
    assert "rolled the batch transaction back" not in report.message
    history, progress, orders = _state(db)
    assert history[-1] == ("swallow", "ACTIVE", 1)
    assert progress == []
    assert orders == [(0,)]


#: The author swallows the conflict, makes another call in the dead batch,
#: catches the refusal outside the batch, opens a second batch, records what
#: that raises, and returns normally.
CONTINUES_AFTER_ROLLBACK = """\
import pathlib


def migrate(ctx):
    try:
        with ctx.transaction() as tx:
            tx.execute("INSERT INTO orders (id) VALUES (1)")
            try:
                tx.execute("INSERT OR ROLLBACK INTO orders (id) VALUES (0)")
            except Exception:
                pass
            tx.execute("INSERT INTO orders (id) VALUES (2)")
    except Exception:
        pass
    try:
        with ctx.transaction() as tx:
            tx.execute("INSERT INTO orders (id) VALUES (5)")
    except Exception as exc:
        pathlib.Path({second!r}).write_text(str(exc))
"""


def test_a_call_after_the_rollback_latches_the_run_and_refuses_a_second_batch(project):
    root, config, db = project
    second = root / "second.txt"
    body = CONTINUES_AFTER_ROLLBACK.format(second=str(second))
    manifest = _project(root, VARIANTS["insert-or-rollback"][0], body)

    report = support.migrate_report(config, manifest)

    assert report.exit_code == Exit.MIGRATION_FAILED
    expected = (
        "the database rolled the batch transaction back before ctx.execute(); "
        "the batch is not committed"
    )
    assert expected in report.message
    # The second batch was refused with the latched error before it began.
    assert second.read_text() == expected
    history, progress, orders = _state(db)
    assert [row for row in history if row[0] == "swallow"] == [("swallow", "ACTIVE", 1)]
    assert progress == []
    assert orders == [(0,)]


def test_commit_with_no_transaction_active_is_a_definite_rejection(project):
    root, config, db = project
    adapter = adapters.create(load_config(config))
    adapter.latch = RunLatch()
    adapter.connect()
    try:
        adapter.begin()
        assert adapter.batch_transaction_state() is BatchTransactionState.OPEN
        adapter._db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        adapter._db.execute("INSERT INTO t (id) VALUES (1)")
        with pytest.raises(sqlite3.IntegrityError):
            adapter._db.execute("INSERT OR ROLLBACK INTO t (id) VALUES (1)")
        assert adapter.batch_transaction_state() is BatchTransactionState.ROLLED_BACK
        with pytest.raises(sqlite3.OperationalError) as info:
            adapter.durable_commit(Boundary.RESTARTABLE_BATCH)
        assert adapter.classify_exception(info.value) is OutcomeClass.SERVER_REJECTION
        assert not adapter.latch.latched
    finally:
        adapter.close()
