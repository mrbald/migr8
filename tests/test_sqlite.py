"""Real SQLite execution: atomic, restartable, progress and initialization.

These are real SQLite files and real transactions, not mocks.  The adapter's
stated limits still apply: the namespace lock is a POSIX file lock rather than a
database lock, and there is no server transaction identity.

Every test here runs once per supported journal mode.  The two modes commit
differently -- DELETE removes a rollback journal, WAL appends and syncs a commit
record -- so the transaction, checkpoint, damage and restart cases are evidence
for one mode only unless they are run in both.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import support

from migr8.adapters.sqlite import SUPPORTED_JOURNAL_MODES
from migr8.errors import Exit

pytestmark = pytest.mark.sqlite


@pytest.fixture(params=SUPPORTED_JOURNAL_MODES)
def project(request, tmp_path):
    """A workspace with a config whose database lives under ``tmp_path``."""
    db = tmp_path / "build" / "probe.db"
    config = support.sqlite_config(tmp_path, db_path=db, journal_mode=request.param)
    return tmp_path, config, db


# --- initialization ---------------------------------------------------------------


def _objects(db: Path) -> list[str]:
    if not db.exists():
        return []
    return [
        row[0]
        for row in support.db_query(
            db, "SELECT name FROM sqlite_master WHERE name LIKE 'm8_%' ORDER BY name"
        )
    ]


def _one_migration(root):
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    return support.manifest(
        root,
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


def test_recover_on_an_absent_namespace_creates_nothing(project):
    """Spec Section 10.1: missing ACTIVE state fails before metadata mutation.

    The refusal has to come first. Oracle's initialization DDL commits
    independently, so a namespace initialized on the way to an exit 2 could not
    be undone afterwards.
    """
    root, config, db = project
    manifest = _one_migration(root)
    assert support.migrate(config, manifest, recover="create-t") == Exit.VALIDATION
    assert _objects(db) == []
    assert support.db_query(db, "SELECT name FROM sqlite_master WHERE name = 't'") == []


def test_recover_on_an_incomplete_namespace_completes_nothing(project):
    """A prefix left by an interrupted initialization holds no ACTIVE identity either."""
    from migr8.adapters.sqlite import _DDL
    from migr8.model import HISTORY_TABLE

    root, config, db = project
    manifest = _one_migration(root)
    db.parent.mkdir(parents=True, exist_ok=True)
    support.db_exec(db, _DDL[HISTORY_TABLE])
    assert support.migrate(config, manifest, recover="create-t") == Exit.VALIDATION
    # The prefix is untouched: nothing was created and no marker was written.
    assert _objects(db) == [HISTORY_TABLE]
    assert support.db_query(db, "SELECT name FROM sqlite_master WHERE name = 't'") == []


def test_plain_migrate_still_completes_an_incomplete_namespace(project):
    """The refusal is specific to --recover; ordinary initialization is recoverable."""
    from migr8.adapters import metadata as md
    from migr8.adapters.sqlite import _DDL
    from migr8.model import HISTORY_TABLE

    root, config, db = project
    manifest = _one_migration(root)
    db.parent.mkdir(parents=True, exist_ok=True)
    support.db_exec(db, _DDL[HISTORY_TABLE])
    assert support.migrate(config, manifest) == Exit.OK
    assert set(_objects(db)) == set(md.CREATION_ORDER)


def test_recover_with_the_wrong_id_still_reaches_the_admission_rules(project):
    """An initialized namespace keeps the existing identity checks."""
    root, config, db = project
    manifest = _one_migration(root)
    assert support.migrate(config, manifest) == Exit.OK
    report = support.migrate_report(config, manifest, recover="not-that-one")
    assert report.exit_code == Exit.VALIDATION
    assert "does not match the active migration" in (
        report.message or ""
    ) or "requires an ACTIVE restartable migration" in (report.message or "")


def test_first_migrate_initializes_and_applies(project):
    root, config, db = project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    manifest = support.manifest(
        root,
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
    assert support.migrate(config, manifest) == Exit.OK
    assert support.history(db) == [
        (1, "create-t", "SUCCESS", "restartable", "sql", 1, *support.history(db)[0][6:]),
    ]
    assert support.db_query(db, "SELECT COUNT(*) FROM t") == [(0,)]
    meta = support.db_query(db, "SELECT layout_version, adapter, lock_provider FROM m8_meta")
    assert meta == [(1, "sqlite", "file")]


def test_read_only_commands_do_not_initialize(project):
    root, config, db = project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    manifest = support.manifest(
        root,
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
    report = support.report_for("status", config, manifest)
    assert report.exit_code == Exit.NOT_INITIALIZED
    assert report.metadata_state == "absent"
    assert [m.state for m in report.migrations] == ["PENDING"]
    assert support.db_query(db, "SELECT name FROM sqlite_master WHERE type='table'") == []

    report = support.report_for("validate", config, manifest)
    assert report.exit_code == Exit.NOT_INITIALIZED
    assert support.db_query(db, "SELECT name FROM sqlite_master WHERE type='table'") == []


def test_incomplete_initialization_is_completed_not_recreated(project):
    root, config, db = project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    manifest = support.manifest(
        root,
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
    assert support.migrate(config, manifest) == Exit.OK
    # Remove only the marker row: history is empty of nothing, so this looks like
    # an interrupted initialization that may be completed.
    support.db_exec(db, "DELETE FROM m8_history")
    support.db_exec(db, "DELETE FROM m8_meta")
    support.db_exec(db, "DROP TABLE t")
    assert support.migrate(config, manifest) == Exit.OK
    assert [row[1] for row in support.history(db)] == ["create-t"]


def test_populated_history_without_a_marker_is_damage(project):
    root, config, db = project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    manifest = support.manifest(
        root,
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
    assert support.migrate(config, manifest) == Exit.OK
    support.db_exec(db, "DELETE FROM m8_meta")
    assert support.migrate(config, manifest) == Exit.METADATA_DAMAGED
    report = support.report_for("status", config, manifest)
    assert report.exit_code == Exit.METADATA_DAMAGED
    assert "marker is absent" in report.problem


def test_missing_object_after_completed_initialization_is_damage(project):
    root, config, db = project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    manifest = support.manifest(
        root,
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
    assert support.migrate(config, manifest) == Exit.OK
    support.db_exec(db, "DROP TABLE m8_progress")
    assert support.migrate(config, manifest) == Exit.METADATA_DAMAGED
    report = support.report_for("validate", config, manifest)
    assert "m8_progress" in report.problem


def test_missing_one_active_index_after_initialization_is_damage(project):
    root, config, db = project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    manifest = support.manifest(
        root,
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
    assert support.migrate(config, manifest) == Exit.OK
    support.db_exec(db, "DROP INDEX m8_history_one_active")
    assert support.migrate(config, manifest) == Exit.METADATA_DAMAGED


def test_incompatible_column_layout_is_damage(project):
    root, config, db = project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    manifest = support.manifest(
        root,
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
    assert support.migrate(config, manifest) == Exit.OK
    support.db_exec(db, "ALTER TABLE m8_history ADD COLUMN surprise TEXT")
    assert support.migrate(config, manifest) == Exit.METADATA_DAMAGED


# --- atomic ------------------------------------------------------------------------


def _atomic_project(root: Path, sql: str):
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    support.unit(root, "m2", {"up.sql": sql})
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
            {"id": "work", "path": "m2", "language": "sql", "mode": "atomic", "entry": "up.sql"},
        ],
    )


def test_atomic_work_and_history_commit_together(project):
    root, config, db = project
    manifest = _atomic_project(root, "INSERT INTO t (id) VALUES (1);")
    assert support.migrate(config, manifest) == Exit.OK
    assert support.db_query(db, "SELECT id FROM t") == [(1,)]
    assert [row[2] for row in support.history(db)] == ["SUCCESS", "SUCCESS"]
    assert support.history(db)[1][5] is None  # atomic attempts are not counted


def test_atomic_statement_error_leaves_no_history_row_and_no_work(project):
    root, config, db = project
    manifest = _atomic_project(root, "INSERT INTO t (id) VALUES ('x'), (NULL);")
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    assert [row[1] for row in support.history(db)] == ["create-t"]
    assert support.db_query(db, "SELECT COUNT(*) FROM t") == [(0,)]


def test_atomic_query_only_migration_succeeds(project):
    root, config, db = project
    manifest = _atomic_project(root, "SELECT COUNT(*) FROM t;")
    assert support.migrate(config, manifest) == Exit.OK
    assert [row[2] for row in support.history(db)] == ["SUCCESS", "SUCCESS"]


def test_atomic_python_cannot_reach_transaction_or_ddl(project):
    root, config, db = project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    support.unit(
        root,
        "m2",
        {
            "migration.py": (
                "def migrate(ctx):\n"
                "    assert not hasattr(ctx, 'transaction')\n"
                "    assert not hasattr(ctx, 'ddl')\n"
                "    assert not hasattr(ctx, 'progress')\n"
                "    assert ctx.attempt is None\n"
                "    ctx.execute('INSERT INTO t (id) VALUES (?)', (7,))\n"
            )
        },
    )
    manifest = support.manifest(
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
                "id": "work",
                "path": "m2",
                "language": "python",
                "mode": "atomic",
                "entry": "migration.py",
            },
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    assert support.db_query(db, "SELECT id FROM t") == [(7,)]


def test_atomic_python_rejects_transaction_control_statements(project):
    root, config, db = project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    support.unit(
        root,
        "m2",
        {
            "migration.py": (
                "def migrate(ctx):\n"
                "    ctx.execute('INSERT INTO t (id) VALUES (1)')\n"
                "    ctx.execute('COMMIT')\n"
            )
        },
    )
    manifest = support.manifest(
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
                "id": "work",
                "path": "m2",
                "language": "python",
                "mode": "atomic",
                "entry": "migration.py",
            },
        ],
    )
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    assert [row[1] for row in support.history(db)] == ["create-t"]
    assert support.db_query(db, "SELECT COUNT(*) FROM t") == [(0,)]


# --- restartable -------------------------------------------------------------------

BATCHED = """\
def migrate(ctx):
    last = int(ctx.progress.get("last_id", "0"))
    while True:
        with ctx.transaction() as tx:
            rows = tx.query(
                "SELECT id FROM src WHERE id > ? ORDER BY id LIMIT ?", (last, 2)
            )
            if not rows:
                break
            ids = [row[0] for row in rows]
            tx.executemany("INSERT INTO dst (id) VALUES (?)", [(i,) for i in ids])
            last = max(ids)
            ctx.progress.set("last_id", str(last))
        ctx.log("batch", last_id=last)
"""


def _batch_project(root: Path, body: str = BATCHED, rows: int = 5):
    values = ", ".join(f"({n})" for n in range(1, rows + 1))
    support.unit(
        root, "m1", {"up.sql": ("CREATE TABLE IF NOT EXISTS src (id INTEGER PRIMARY KEY);\n")}
    )
    support.unit(root, "m2", {"up.sql": f"INSERT INTO src (id) VALUES {values};"})
    support.unit(
        root, "m3", {"up.sql": ("CREATE TABLE IF NOT EXISTS dst (id INTEGER PRIMARY KEY);\n")}
    )
    support.unit(root, "m4", {"migration.py": body})
    return support.manifest(
        root,
        [
            {
                "id": "src",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
            {"id": "seed", "path": "m2", "language": "sql", "mode": "atomic", "entry": "up.sql"},
            {
                "id": "dst",
                "path": "m3",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
            {
                "id": "copy",
                "path": "m4",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
        ],
    )


def test_restartable_batches_commit_with_their_checkpoint(project):
    root, config, db = project
    manifest = _batch_project(root)
    assert support.migrate(config, manifest) == Exit.OK
    assert support.db_query(db, "SELECT id FROM dst ORDER BY id") == [(n,) for n in range(1, 6)]
    assert support.progress(db) == []  # completion deletes progress
    row = next(r for r in support.history(db) if r[1] == "copy")
    assert row[2] == "SUCCESS" and row[5] == 1


def test_failure_mid_way_retains_active_and_the_checkpoint(project):
    root, config, db = project
    failing = BATCHED.replace(
        '        ctx.log("batch", last_id=last)',
        "        if last >= 4:\n"
        '            raise RuntimeError("stop after the second batch")\n'
        '        ctx.log("batch", last_id=last)',
    )
    manifest = _batch_project(root, failing)
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    assert support.db_query(db, "SELECT id FROM dst ORDER BY id") == [(1,), (2,), (3,), (4,)]
    assert support.progress(db) == [("copy", "last_id", "4")]
    row = next(r for r in support.history(db) if r[1] == "copy")
    assert row[2] == "ACTIVE" and row[5] == 1

    # An unchanged retry resumes from the checkpoint and completes.
    support.unit(root, "m4", {"migration.py": BATCHED})
    assert support.migrate(config, manifest) == Exit.VALIDATION  # source changed
    report = support.report_for("status", config, manifest)
    assert report.exit_code == Exit.VALIDATION
    assert report.recovery_command.endswith("--recover copy")
    assert support.migrate(config, manifest, recover="copy") == Exit.OK
    assert support.db_query(db, "SELECT id FROM dst ORDER BY id") == [(n,) for n in range(1, 6)]
    row = next(r for r in support.history(db) if r[1] == "copy")
    assert row[2] == "SUCCESS" and row[5] == 2
    assert row[6] != row[7]  # current fingerprint changed, first one retained


def test_unchanged_retry_resumes_without_the_recover_flag(project):
    root, config, db = project
    marker = root / "fail-once"
    marker.write_text("1")
    body = BATCHED.replace(
        '        ctx.log("batch", last_id=last)',
        "        import pathlib\n"
        f"        flag = pathlib.Path({str(marker)!r})\n"
        "        if flag.exists() and last >= 4:\n"
        "            flag.unlink()\n"
        '            raise RuntimeError("stop once")\n'
        '        ctx.log("batch", last_id=last)',
    )
    manifest = _batch_project(root, body)
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    assert support.progress(db) == [("copy", "last_id", "4")]
    # Identical source: a plain retry is admitted as attempt 2.
    assert support.migrate(config, manifest) == Exit.OK
    row = next(r for r in support.history(db) if r[1] == "copy")
    assert row[2] == "SUCCESS" and row[5] == 2
    assert row[6] == row[7]


def test_the_attempt_the_author_sees_is_the_attempt_that_was_recorded(project):
    """``ctx.attempt`` is derived, not read back, so it must track the row.

    The engine computes the new attempt from the snapshot it read under the
    namespace lock rather than selecting it again after the update; this pins
    the two together across several attempts.
    """
    root, config, db = project
    seen = root / "attempts.txt"
    body = (
        "def migrate(ctx):\n"
        "    import pathlib\n"
        f"    log = pathlib.Path({str(seen)!r})\n"
        "    with log.open('a') as handle:\n"
        "        handle.write(f'{ctx.attempt}\\n')\n"
        "    if ctx.attempt < 3:\n"
        "        raise RuntimeError('not yet')\n"
    )
    manifest = _batch_project(root, body)

    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    assert support.migrate(config, manifest) == Exit.OK

    assert seen.read_text().split() == ["1", "2", "3"]
    row = next(r for r in support.history(db) if r[1] == "copy")
    assert row[2] == "SUCCESS"
    assert row[5] == 3, "the recorded attempt and the reported attempt must agree"


def test_rerunning_completed_work_is_a_no_op(project):
    root, config, db = project
    manifest = _batch_project(root)
    assert support.migrate(config, manifest) == Exit.OK
    before = support.history(db)
    assert support.migrate(config, manifest) == Exit.OK
    assert support.history(db) == before


def test_progress_write_outside_a_batch_is_rejected(project):
    root, config, db = project
    body = "def migrate(ctx):\n    ctx.progress.set('k', 'v')\n"
    manifest = _batch_project(root, body)
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    assert support.progress(db) == []


def test_direct_dml_outside_a_batch_is_rejected(project):
    root, config, db = project
    body = "def migrate(ctx):\n    ctx.execute('INSERT INTO dst (id) VALUES (1)')\n"
    manifest = _batch_project(root, body)
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    assert support.db_query(db, "SELECT COUNT(*) FROM dst") == [(0,)]


def test_reads_outside_a_batch_are_allowed(project):
    root, config, db = project
    body = (
        "def migrate(ctx):\n"
        "    rows = ctx.query('SELECT COUNT(*) FROM src')\n"
        "    assert rows == [(5,)]\n"
        "    assert ctx.progress.get('missing') is None\n"
        "    assert ctx.progress.get('missing', 'fallback') == 'fallback'\n"
    )
    manifest = _batch_project(root, body)
    assert support.migrate(config, manifest) == Exit.OK


def test_nested_batches_are_rejected(project):
    root, config, db = project
    body = (
        "def migrate(ctx):\n"
        "    with ctx.transaction():\n"
        "        with ctx.transaction():\n"
        "            pass\n"
    )
    manifest = _batch_project(root, body)
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED


def test_batch_rollback_discards_data_and_checkpoint_together(project):
    root, config, db = project
    body = (
        "def migrate(ctx):\n"
        "    with ctx.transaction() as tx:\n"
        "        tx.executemany('INSERT INTO dst (id) VALUES (?)', [(1,), (2,)])\n"
        "        ctx.progress.set('last_id', '2')\n"
        "        raise RuntimeError('abandon the batch')\n"
    )
    manifest = _batch_project(root, body)
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    assert support.db_query(db, "SELECT COUNT(*) FROM dst") == [(0,)]
    assert support.progress(db) == []


def test_ddl_is_available_only_in_restartable_mode(project):
    root, config, db = project
    body = (
        "def migrate(ctx):\n"
        "    ctx.ddl('CREATE TABLE made_by_ddl (id INTEGER PRIMARY KEY)')\n"
        "    with ctx.transaction() as tx:\n"
        "        tx.execute('INSERT INTO made_by_ddl (id) VALUES (1)')\n"
        "        ctx.progress.set('done', 'yes')\n"
    )
    manifest = _batch_project(root, body)
    assert support.migrate(config, manifest) == Exit.OK
    assert support.db_query(db, "SELECT id FROM made_by_ddl") == [(1,)]


def test_ddl_inside_a_batch_is_rejected(project):
    root, config, db = project
    body = (
        "def migrate(ctx):\n"
        "    with ctx.transaction():\n"
        "        ctx.ddl('CREATE TABLE nope (id INTEGER)')\n"
    )
    manifest = _batch_project(root, body)
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    names = support.db_query(db, "SELECT name FROM sqlite_master WHERE name='nope'")
    assert names == []


def test_migration_cannot_modify_reserved_metadata_objects(project):
    root, config, db = project
    body = "def migrate(ctx):\n    ctx.ddl('DROP TABLE m8_progress')\n"
    manifest = _batch_project(root, body)
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    assert support.db_query(db, "SELECT COUNT(*) FROM sqlite_master WHERE name='m8_progress'") == [
        (1,)
    ]


def test_ctx_sql_reads_only_fingerprinted_unit_files(project):
    root, config, db = project
    manifest = _batch_project(root, BATCHED)
    support.unit(
        root,
        "m4",
        {
            "migration.py": (
                "def migrate(ctx):\n"
                "    text = ctx.sql('queries/insert.sql')\n"
                "    with ctx.transaction() as tx:\n"
                "        tx.execute(text, (99,))\n"
                "        ctx.progress.set('done', 'yes')\n"
            ),
            "queries/insert.sql": "INSERT INTO dst (id) VALUES (?);\n",
        },
    )
    assert support.migrate(config, manifest) == Exit.OK
    assert support.db_query(db, "SELECT id FROM dst") == [(99,)]


@pytest.mark.parametrize("bad", ["/etc/passwd", "../outside.sql", "missing.sql"])
def test_ctx_sql_rejects_escaping_and_unknown_paths(project, bad):
    root, config, db = project
    body = f"def migrate(ctx):\n    ctx.sql({bad!r})\n"
    manifest = _batch_project(root, body)
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED


def test_open_transaction_on_return_is_refused_and_rolled_back(project):
    """The engine does not silently commit forgotten work."""
    root, config, db = project
    body = (
        "def migrate(ctx):\n"
        "    cm = ctx.transaction()\n"
        "    tx = cm.__enter__()\n"
        "    tx.execute('INSERT INTO dst (id) VALUES (1)')\n"
        "    # deliberately never exit the context manager\n"
    )
    manifest = _batch_project(root, body)
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    assert support.db_query(db, "SELECT COUNT(*) FROM dst") == [(0,)]
    row = next(r for r in support.history(db) if r[1] == "copy")
    assert row[2] == "ACTIVE"


def test_progress_key_and_value_limits(project):
    root, config, db = project
    body = (
        "def migrate(ctx):\n"
        "    with ctx.transaction():\n"
        "        try:\n"
        "            ctx.progress.set('k', '')\n"
        "        except Exception as exc:\n"
        "            assert 'non-empty' in str(exc), exc\n"
        "        else:\n"
        "            raise AssertionError('empty value must be rejected')\n"
        "        try:\n"
        "            ctx.progress.set('x' * 129, 'v')\n"
        "        except Exception as exc:\n"
        "            assert 'exceeds 128' in str(exc), exc\n"
        "        else:\n"
        "            raise AssertionError('long key must be rejected')\n"
        "        try:\n"
        "            ctx.progress.set('k', 'v' * 4001)\n"
        "        except Exception as exc:\n"
        "            assert 'exceeds 4000' in str(exc), exc\n"
        "        else:\n"
        "            raise AssertionError('long value must be rejected')\n"
        "        ctx.progress.set('k', 'v')\n"
    )
    manifest = _batch_project(root, body)
    assert support.migrate(config, manifest) == Exit.OK


# --- required objects ---------------------------------------------------------------


def test_the_adapter_rejects_oracle_style_required_objects(project):
    root, config, db = project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-t",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
                "require_valid": [{"name": "PKG", "type": "PACKAGE"}],
            }
        ],
    )
    assert support.migrate(config, manifest) == Exit.USAGE


# --- ordering -----------------------------------------------------------------------


def test_a_failure_stops_the_run_and_later_migrations_do_not_execute(project):
    root, config, db = project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    support.unit(root, "m2", {"up.sql": "INSERT INTO absent_table (id) VALUES (1);"})
    support.unit(root, "m3", {"up.sql": "CREATE TABLE later (id INTEGER PRIMARY KEY);\n"})
    manifest = support.manifest(
        root,
        [
            {"id": "a", "path": "m1", "language": "sql", "mode": "restartable", "entry": "up.sql"},
            {"id": "b", "path": "m2", "language": "sql", "mode": "atomic", "entry": "up.sql"},
            {"id": "c", "path": "m3", "language": "sql", "mode": "restartable", "entry": "up.sql"},
        ],
    )
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    assert [row[1] for row in support.history(db)] == ["a"]
    assert support.db_query(db, "SELECT COUNT(*) FROM sqlite_master WHERE name='later'") == [(0,)]


# --- interrupted initialization -------------------------------------------------


@pytest.mark.parametrize("occurrence", [1, 2, 3, 4])
def test_interruption_after_each_metadata_object_creation_is_completed(project, occurrence):
    """Spec Section 14.2 group 3: each object creation is independently durable.

    The interruption is injected by replacing the adapter's ``commit`` at the
    metadata-object boundary. That is a wrapper simulation of a lost
    acknowledgement, not transport evidence; the transport branches are covered
    against Oracle and PostgreSQL in tests/integration.
    """
    from migr8.adapters.base import Boundary
    from migr8.testing import hooks

    root, config, db = project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    manifest = support.manifest(
        root,
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

    state = {"seen": 0, "armed": False, "fired": False}

    def hook(adapter):
        real_commit = adapter.commit

        def on_boundary(boundary, phase):
            if boundary == Boundary.METADATA_OBJECT_CREATED and phase == hooks.BEFORE_COMMIT:
                state["seen"] += 1
                if state["seen"] == occurrence:
                    state["armed"] = True

        hooks.register(on_boundary)

        def commit():
            if state["armed"]:
                state["armed"] = False
                state["fired"] = True
                raise RuntimeError("simulated loss of acknowledgement")
            return real_commit()

        adapter.commit = commit

    assert support.migrate(config, manifest, adapter_hook=hook) == Exit.UNKNOWN_OUTCOME
    assert state["fired"]
    existing = {
        row[0]
        for row in support.db_query(db, "SELECT name FROM sqlite_master WHERE name LIKE 'm8%'")
    }
    assert len(existing) == occurrence - 1, existing

    # A fresh run completes the permitted incomplete initialization.
    assert support.migrate(config, manifest) == Exit.OK
    assert support.db_query(db, "SELECT layout_version, adapter FROM m8_meta") == [(1, "sqlite")]
    assert [row[1] for row in support.history(db)] == ["create-t"]


def test_interruption_before_the_marker_is_completed_on_the_next_run(project):
    from migr8.adapters.base import Boundary
    from migr8.testing import hooks

    root, config, db = project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    manifest = support.manifest(
        root,
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
    state = {"fired": False}

    def hook(adapter):
        real_commit = adapter.commit
        armed = {"value": False}

        def on_boundary(boundary, phase):
            if boundary == Boundary.INITIALIZATION_COMPLETE and phase == hooks.BEFORE_COMMIT:
                armed["value"] = True

        hooks.register(on_boundary)

        def commit():
            if armed["value"]:
                armed["value"] = False
                state["fired"] = True
                raise RuntimeError("simulated loss of acknowledgement")
            return real_commit()

        adapter.commit = commit

    assert support.migrate(config, manifest, adapter_hook=hook) == Exit.UNKNOWN_OUTCOME
    assert state["fired"]
    # Every object exists but the marker does not, and history is empty.
    names = {
        row[0]
        for row in support.db_query(db, "SELECT name FROM sqlite_master WHERE name LIKE 'm8%'")
    }
    assert names == {"m8_history", "m8_history_one_active", "m8_progress", "m8_meta"}
    assert support.db_query(db, "SELECT COUNT(*) FROM m8_meta") == [(0,)]
    # Read-only commands report this as uninitialized, not damaged.
    report = support.report_for("status", config, manifest)
    assert report.exit_code == Exit.NOT_INITIALIZED
    assert report.metadata_state == "incomplete_compatible"
    assert support.migrate(config, manifest) == Exit.OK


# --- atomic history-insertion failure ---------------------------------------------


def test_error_inserting_successful_history_rolls_back_the_migration_work(project):
    """Spec Section 14.2 group 4. The duplicate insert is injected at the engine
    boundary; the point under test is that the work rolls back with it."""
    root, config, db = project
    manifest = _atomic_project(root, "INSERT INTO t (id) VALUES (1);")

    def hook(adapter):
        real_insert = adapter.insert_success_row

        def insert_success_row(**kwargs):
            real_insert(**kwargs)
            real_insert(**kwargs)  # violates the migration_id primary key

        adapter.insert_success_row = insert_success_row

    report = support.migrate_report(config, manifest, adapter_hook=hook)
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert [row[1] for row in support.history(db)] == ["create-t"]
    assert support.db_query(db, "SELECT COUNT(*) FROM t") == [(0,)]


# --- read-only commands change nothing ---------------------------------------------


def test_read_only_commands_do_not_change_the_journal_mode(tmp_path):
    """Spec Section 13.3: the journal mode is set by migrate alone, never as a
    side effect of status or validate."""
    db = tmp_path / "build" / "probe.db"
    delete_mode = support.sqlite_config(tmp_path, db_path=db, journal_mode="delete")
    support.unit(tmp_path, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    manifest = support.manifest(
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
    assert support.migrate(delete_mode, manifest) == Exit.OK
    assert support.db_query(db, "PRAGMA journal_mode") == [("delete",)]

    wal_mode = support.sqlite_config(tmp_path, db_path=db, journal_mode="wal", name="wal.toml")
    assert support.report_for("status", wal_mode, manifest).exit_code == Exit.OK
    assert support.db_query(db, "PRAGMA journal_mode") == [("delete",)]
    assert support.report_for("validate", wal_mode, manifest).exit_code == Exit.OK
    assert support.db_query(db, "PRAGMA journal_mode") == [("delete",)]

    # Only migrate performs the controlled storage preparation.
    assert support.migrate(wal_mode, manifest) == Exit.OK
    assert support.db_query(db, "PRAGMA journal_mode") == [("wal",)]


def test_read_only_commands_do_not_import_migration_code(project):
    root, config, db = project
    support.unit(
        root,
        "m1",
        {
            "migration.py": (
                "raise RuntimeError("
                "'importing this unit must never happen in validate or status')\n"
                "\n\n"
                "def migrate(ctx):\n    pass\n"
            )
        },
    )
    manifest = support.manifest(
        root,
        [
            {
                "id": "never-imported",
                "path": "m1",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            }
        ],
    )
    # Both read-only commands fingerprint the unit without importing it.
    for command in ("status", "validate"):
        report = support.report_for(command, config, manifest)
        assert report.exit_code == Exit.NOT_INITIALIZED
        assert report.migrations[0].current_fingerprint.startswith("fp1:")
    # migrate does import it, and the import error surfaces there.
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
