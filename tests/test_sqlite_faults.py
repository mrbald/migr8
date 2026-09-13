"""Process death, contention and unwritable storage against real SQLite files.

The runner is killed with SIGKILL at each engine boundary, in a real process,
and the next run has to reach exactly the state the protocol defines.  A killed
process is not a power failure: the operating system still holds and writes
whatever the process had handed it, so what these establish is the engine's
recovery across a lost process, not durability across lost power.  Nothing here
simulates a full filesystem either; the storage failures are an unwritable
database and an unwritable directory, which is what an operator actually meets.

Each case runs in both supported journal modes, because recovery after a kill is
exactly where they differ: DELETE rolls back a hot journal, WAL replays a log.
"""

from __future__ import annotations

import os
import shutil
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import support

from migr8.adapters.base import Boundary
from migr8.adapters.sqlite import SUPPORTED_JOURNAL_MODES
from migr8.errors import Exit
from migr8.testing import hooks

pytestmark = pytest.mark.sqlite

ENTRY = Path(__file__).resolve().parents[1] / "migr8"
TIMEOUT = 60

#: Root ignores the permission bits, so a database made unwritable is still
#: written and the case under test cannot arise.  Containers routinely run as
#: root, and a skip that says why is better than a failure that looks like one.
needs_permissions = pytest.mark.skipif(
    os.geteuid() == 0, reason="running as root, which ignores the permission bits this asserts"
)

#: Installed in the runner through the documented out-of-process hook variables.
#: It kills the process at one engine boundary, which is the only way to stop a
#: real runner exactly there without the runner cooperating.
KILL_HOOK = '''\
"""Kill this process at one engine boundary.  Test harness only."""

import os
import signal

from migr8.testing import hooks

BOUNDARY = os.environ["MIGR8_KILL_BOUNDARY"]
PHASE = os.environ["MIGR8_KILL_PHASE"]


def _kill(boundary, phase):
    if boundary == BOUNDARY and phase == PHASE:
        os.kill(os.getpid(), signal.SIGKILL)


hooks.register(_kill)
'''

BATCHED = """\
ROWS = [1, 2, 3, 4, 5, 6]
BATCH = 2


def migrate(ctx):
    done = int(ctx.progress.get("done", "0"))
    while done < len(ROWS):
        batch = ROWS[done : done + BATCH]
        with ctx.transaction() as tx:
            tx.executemany("INSERT INTO dst (id) VALUES (?)", [(key,) for key in batch])
            done += len(batch)
            ctx.progress.set("done", str(done))
"""


def plan(root: Path) -> Path:
    """Three migrations: a DDL step, an atomic step and a checkpointed one."""
    # A restartable migration is re-entered from its entry point, so its SQL says
    # so: the runner may be killed after the DDL commits and before the SUCCESS row.
    support.unit(
        root, "m1", {"up.sql": "CREATE TABLE IF NOT EXISTS dst (id INTEGER PRIMARY KEY);\n"}
    )
    support.unit(root, "m2", {"up.sql": "INSERT INTO dst (id) VALUES (100);\n"})
    support.unit(root, "m3", {"migration.py": BATCHED})
    return support.manifest(
        root,
        [
            {
                "id": "ddl",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
            {"id": "atomic", "path": "m2", "language": "sql", "mode": "atomic", "entry": "up.sql"},
            {
                "id": "batched",
                "path": "m3",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
        ],
    )


@pytest.fixture(params=SUPPORTED_JOURNAL_MODES)
def project(request, tmp_path):
    db = tmp_path / "build" / "probe.db"
    support.sqlite_config(tmp_path, db_path=db, journal_mode=request.param)
    plan(tmp_path)
    return tmp_path, db


def run(root: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ENTRY), *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        env={**os.environ, **(env or {})},
    )


def kill_at(root: Path, boundary: str, phase: str) -> subprocess.CompletedProcess:
    """Run ``migrate`` in a process that kills itself at one boundary."""
    support.write(root / "killhook.py", KILL_HOOK)
    return run(
        root,
        "migrate",
        env={
            "PYTHONPATH": str(root),
            hooks.ENABLE_ENV: hooks.ENABLE_TOKEN,
            hooks.MODULE_ENV: "killhook",
            "MIGR8_KILL_BOUNDARY": boundary,
            "MIGR8_KILL_PHASE": phase,
        },
    )


def rows(db: Path, table: str) -> list:
    if not db.exists():
        return []
    return [row[0] for row in support.db_query(db, f"SELECT id FROM {table} ORDER BY id")]


# --- process death at each durable boundary ----------------------------------------

BOUNDARIES = [
    Boundary.METADATA_OBJECT_CREATED,
    Boundary.INITIALIZATION_COMPLETE,
    Boundary.RESTARTABLE_ADMISSION,
    Boundary.RESTARTABLE_BATCH,
    Boundary.RESTARTABLE_COMPLETION,
    Boundary.ATOMIC_COMPLETION,
]


@pytest.mark.parametrize("phase", [hooks.BEFORE_COMMIT, hooks.AFTER_COMMIT])
@pytest.mark.parametrize("boundary", BOUNDARIES, ids=[item.value for item in BOUNDARIES])
def test_a_killed_runner_leaves_a_state_the_next_run_completes(project, boundary, phase):
    root, db = project
    killed = kill_at(root, boundary.value, phase)
    assert killed.returncode == -signal.SIGKILL, killed.stderr

    # Nothing is held afterwards: the lock died with the process.
    again = run(root, "migrate")
    assert again.returncode == Exit.OK, again.stderr

    assert [(row[0], row[1], row[2]) for row in support.history(db)] == [
        (1, "ddl", "SUCCESS"),
        (2, "atomic", "SUCCESS"),
        (3, "batched", "SUCCESS"),
    ]
    # Exactly once each: the checkpoint decides where the rerun resumes, so work
    # committed before the kill is not repeated and work after it is not lost.
    assert rows(db, "dst") == [1, 2, 3, 4, 5, 6, 100]


def test_a_kill_after_the_atomic_commit_leaves_that_migration_successful(project):
    """The two sides of one commit, asserted rather than accepted either way."""
    root, db = project
    assert kill_at(root, Boundary.ATOMIC_COMPLETION.value, hooks.AFTER_COMMIT).returncode == -9
    recorded = {row[1]: row[2] for row in support.history(db)}
    assert recorded["atomic"] == "SUCCESS"
    assert rows(db, "dst") == [100]


def test_a_kill_before_the_atomic_commit_leaves_neither_the_work_nor_the_row(project):
    root, db = project
    assert kill_at(root, Boundary.ATOMIC_COMPLETION.value, hooks.BEFORE_COMMIT).returncode == -9
    assert "atomic" not in {row[1] for row in support.history(db)}
    assert rows(db, "dst") == []


def test_a_kill_between_batches_keeps_the_checkpoint_and_its_data_together(project):
    root, db = project
    assert kill_at(root, Boundary.RESTARTABLE_BATCH.value, hooks.AFTER_COMMIT).returncode == -9
    checkpoint = {entry[1]: entry[2] for entry in support.progress(db)}
    assert checkpoint["done"] == "2"
    assert rows(db, "dst") == [1, 2, 100]
    assert {row[1]: row[2] for row in support.history(db)}["batched"] == "ACTIVE"


def test_the_lock_file_of_a_killed_runner_does_not_block_the_next_run(project):
    """A POSIX advisory lock is released by the kernel when the process ends."""
    root, db = project
    assert kill_at(root, Boundary.RESTARTABLE_BATCH.value, hooks.AFTER_COMMIT).returncode == -9
    assert db.with_name(db.name + ".m8lock").exists()
    assert run(root, "migrate").returncode == Exit.OK


# --- an application sharing the database -------------------------------------------


def test_an_application_reader_sees_the_state_before_an_uncommitted_batch(project):
    """A reader is never shown a batch that has not committed, in either mode."""
    root, db = project
    assert run(root, "migrate").returncode == Exit.OK

    reader = sqlite3.connect(db, isolation_level=None, timeout=1)
    writer = sqlite3.connect(db, isolation_level=None, timeout=1)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO dst (id) VALUES (900)")
        assert reader.execute("SELECT count(*) FROM dst WHERE id = 900").fetchone() == (0,)
        writer.execute("COMMIT")
        assert reader.execute("SELECT count(*) FROM dst WHERE id = 900").fetchone() == (1,)
    finally:
        reader.close()
        writer.close()


def test_an_application_writer_holding_the_database_fails_the_run_with_no_history(project):
    """The busy timeout expires and the run fails; it does not wait forever or half-apply."""
    root, db = project
    assert run(root, "migrate").returncode == Exit.OK
    support.write(root / "m4" / "up.sql", "INSERT INTO dst (id) VALUES (200);\n")
    support.manifest(
        root,
        [
            {
                "id": "ddl",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
            {"id": "atomic", "path": "m2", "language": "sql", "mode": "atomic", "entry": "up.sql"},
            {
                "id": "batched",
                "path": "m3",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
            {"id": "more", "path": "m4", "language": "sql", "mode": "atomic", "entry": "up.sql"},
        ],
    )

    blocker = sqlite3.connect(db, isolation_level=None, timeout=1)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        result = run(root, "migrate")
        assert result.returncode == Exit.MIGRATION_FAILED, result.stdout + result.stderr
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert "more" not in {row[1] for row in support.history(db)}
    assert 200 not in rows(db, "dst")
    # The failure is reported, and the next run applies the same migration.
    assert run(root, "migrate").returncode == Exit.OK
    assert 200 in rows(db, "dst")


# --- a filesystem that fills -------------------------------------------------------

#: A filesystem small enough to fill, supplied by whoever runs the suite.  A
#: temporary directory is not one: only a real bounded filesystem produces the
#: ENOSPC this needs, and `docs/ACCEPTANCE.md` records the command that makes one.
SMALL_FS = os.environ.get("MIGR8_SMALL_FS")

FILLS = """\
PAD = "x" * 900
BATCH = 200
TOTAL = 20000


def migrate(ctx):
    done = int(ctx.progress.get("done", "0"))
    while done < TOTAL:
        with ctx.transaction() as tx:
            tx.executemany(
                "INSERT INTO big (id, pad) VALUES (?, ?)",
                [(key, PAD) for key in range(done, done + BATCH)],
            )
            done += BATCH
            ctx.progress.set("done", str(done))
"""


@pytest.mark.slow
@pytest.mark.skipif(
    not SMALL_FS, reason="set MIGR8_SMALL_FS to a small writable filesystem to run this"
)
def test_a_filesystem_that_fills_leaves_the_checkpoint_and_its_data_together(tmp_path):
    """A real ENOSPC, not an injected error: the batch that could not commit is absent.

    Where the failure lands decides the exit code, and both answers are defined:
    a statement that cannot write is an ordinary failure, and a commit that fails
    on storage is an unknown outcome, because SQLite settles it at the next open
    rather than in the reply.  What must hold either way is that the durable
    checkpoint and the durable rows agree, and that the namespace is still
    readable afterwards.
    """
    db = Path(SMALL_FS) / "fills.db"
    for stray in Path(SMALL_FS).glob("fills.db*"):
        stray.unlink()
    support.sqlite_config(tmp_path, db_path=db, journal_mode="delete")
    support.unit(
        tmp_path,
        "m1",
        {"up.sql": "CREATE TABLE IF NOT EXISTS big (id INTEGER PRIMARY KEY, pad TEXT NOT NULL);\n"},
    )
    support.unit(tmp_path, "m2", {"migration.py": FILLS})
    support.manifest(
        tmp_path,
        [
            {
                "id": "create",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
            {
                "id": "fill",
                "path": "m2",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
        ],
    )

    result = run(tmp_path, "migrate")
    assert result.returncode in (Exit.MIGRATION_FAILED, Exit.UNKNOWN_OUTCOME), result.stderr
    assert "SQLITE_FULL" in result.stderr or "disk" in result.stderr.lower(), result.stderr

    stored = support.db_query(db, "SELECT prog_value FROM m8_progress WHERE prog_key = 'done'")
    written = support.db_query(db, "SELECT count(*) FROM big")[0][0]
    assert stored, "the migration stopped before its first checkpoint"
    assert written == int(stored[0][0])
    assert {row[1]: row[2] for row in support.history(db)}["fill"] == "ACTIVE"
    assert run(tmp_path, "status").returncode == Exit.OK


# --- backup and restore ------------------------------------------------------------


def test_a_backup_taken_through_sqlite_restores_to_a_working_namespace(project):
    """The documented procedure, rehearsed: back up through SQLite, restore in place.

    The backup API copies a consistent database while connections are open; a
    file copy of the main file alone can miss a WAL. The restore goes back to the
    same canonical path, because that path is what `m8_meta` records.
    """
    root, db = project
    assert run(root, "migrate").returncode == Exit.OK
    before = support.history(db)
    data = rows(db, "dst")

    backup = root / "backup.db"
    source = sqlite3.connect(db)
    target = sqlite3.connect(backup)
    try:
        with target:
            source.backup(target)
    finally:
        source.close()
        target.close()

    for name in (db.name, db.name + "-wal", db.name + "-shm"):
        stray = db.with_name(name)
        if stray.exists():
            stray.unlink()
    shutil.copy(backup, db)

    assert run(root, "status").returncode == Exit.OK
    assert run(root, "validate").returncode == Exit.OK
    assert support.history(db) == before
    assert rows(db, "dst") == data
    # And the restored namespace still takes new work.
    assert run(root, "migrate").returncode == Exit.OK


def test_a_restore_to_another_path_is_refused_by_the_binding(project):
    """`m8_meta` records the canonical path, so a copy elsewhere is a different namespace."""
    root, db = project
    assert run(root, "migrate").returncode == Exit.OK
    elsewhere = root / "elsewhere" / "probe.db"
    elsewhere.parent.mkdir()
    shutil.copy(db, elsewhere)
    support.sqlite_config(root, db_path=elsewhere, name="elsewhere.toml")
    result = run(root, "status", "--config", "elsewhere.toml")
    assert result.returncode == Exit.USAGE
    assert "binding mismatch" in result.stdout + result.stderr


# --- storage the runner cannot write -----------------------------------------------


@needs_permissions
def test_a_read_only_database_file_fails_the_run_and_changes_nothing(project):
    root, db = project
    assert run(root, "migrate").returncode == Exit.OK
    before = support.history(db)
    support.write(root / "m4" / "up.sql", "INSERT INTO dst (id) VALUES (300);\n")
    support.manifest(
        root,
        [
            {
                "id": "ddl",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
            {"id": "atomic", "path": "m2", "language": "sql", "mode": "atomic", "entry": "up.sql"},
            {
                "id": "batched",
                "path": "m3",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
            {"id": "more", "path": "m4", "language": "sql", "mode": "atomic", "entry": "up.sql"},
        ],
    )
    db.chmod(0o444)
    try:
        result = run(root, "migrate")
        assert result.returncode != Exit.OK
    finally:
        db.chmod(0o644)
    assert support.history(db) == before


def test_a_database_path_that_is_a_directory_is_refused(tmp_path):
    """A usage error naming the path, not an unexpected failure about the database."""
    (tmp_path / "build" / "probe.db").mkdir(parents=True)
    support.sqlite_config(tmp_path, db_path=tmp_path / "build" / "probe.db")
    plan(tmp_path)
    result = run(tmp_path, "migrate")
    assert result.returncode == Exit.USAGE
    assert "cannot be opened" in result.stderr
    assert "unexpected failure" not in result.stderr


@needs_permissions
def test_a_database_path_that_cannot_be_created_is_refused(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    support.sqlite_config(tmp_path, db_path=blocked / "sub" / "probe.db")
    plan(tmp_path)
    try:
        result = run(tmp_path, "migrate")
        assert result.returncode == Exit.USAGE
        assert "cannot be used" in result.stderr
    finally:
        blocked.chmod(0o700)
