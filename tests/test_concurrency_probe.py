"""Exclusive execution and read-only inspection (spec Sections 8.1, 11.2, 14.2 group 8).

Real cooperating processes against real SQLite files.  The probe's lock is a
POSIX advisory file lock, which is an explicit exception to the production
adapters' database-backed lock, so these tests establish the engine's exclusion
protocol rather than Oracle or PostgreSQL lock behaviour.
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

pytestmark = pytest.mark.sqlite_probe

ENTRY = Path(__file__).resolve().parents[1] / "migr8"
TIMEOUT = 60


def run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ENTRY), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )


def start_cli(args: list[str], cwd: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(ENTRY), *args],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


SLOW_BODY = """\
import pathlib
import time

READY = pathlib.Path({ready!r})
GO = pathlib.Path({go!r})


def migrate(ctx):
    with ctx.transaction() as tx:
        tx.execute("INSERT INTO dst (id) VALUES (1)")
        ctx.progress.set("last_id", "1")
    READY.write_text("holding the lock")
    deadline = time.monotonic() + 45
    while not GO.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    with ctx.transaction() as tx:
        tx.execute("INSERT INTO dst (id) VALUES (2)")
        ctx.progress.set("last_id", "2")
"""


@pytest.fixture
def slow_project(tmp_path):
    ready = tmp_path / "ready"
    go = tmp_path / "go"
    db = tmp_path / "build" / "probe.db"
    support.unit(tmp_path, "m1", {"up.sql": "CREATE TABLE dst (id INTEGER PRIMARY KEY);\n"})
    support.unit(
        tmp_path,
        "m2",
        {
            "migration.py": SLOW_BODY.format(ready=str(ready), go=str(go)),
        },
    )
    support.manifest(
        tmp_path,
        [
            {
                "id": "dst",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
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
    support.sqlite_config(tmp_path, db_path=db, timeout=0)
    return tmp_path, db, ready, go


def _wait_for(path: Path, *, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared")
        time.sleep(0.02)


def test_zero_timeout_contention_is_deterministic(slow_project):
    root, db, ready, go = slow_project
    holder = start_cli(["migrate"], root)
    try:
        _wait_for(ready)
        contender = run_cli(["migrate"], root)
        assert contender.returncode == Exit.LOCK_NOT_ACQUIRED
        assert "holds the probe lock" in contender.stderr
        assert "not evidence that" in contender.stderr
    finally:
        go.write_text("continue")
        holder.wait(timeout=TIMEOUT)
    assert holder.returncode == Exit.OK
    assert support.db_query(db, "SELECT id FROM dst ORDER BY id") == [(1,), (2,)]


def test_status_is_read_only_and_does_not_take_the_migration_lock(slow_project):
    root, db, ready, go = slow_project
    holder = start_cli(["migrate"], root)
    try:
        _wait_for(ready)
        inspect = run_cli(["status", "--json"], root)
        assert inspect.returncode == Exit.OK
        report = json.loads(inspect.stdout)
        assert report["active_id"] == "slow"
        entry = next(m for m in report["migrations"] if m["id"] == "slow")
        assert entry["state"] == "ACTIVE"
        assert entry["attempt"] == 1
        assert entry["recorded_matches_current"] is True
        assert entry["session_liveness"] == "unknown"
        # validate is read-only too and must reach the same conclusion.
        assert run_cli(["validate"], root).returncode == Exit.OK
    finally:
        go.write_text("continue")
        holder.wait(timeout=TIMEOUT)
    assert holder.returncode == Exit.OK


def test_lock_is_held_across_batch_commits(slow_project):
    """The holder has already committed one batch, yet still owns the lock."""
    root, db, ready, go = slow_project
    holder = start_cli(["migrate"], root)
    try:
        _wait_for(ready)
        assert support.db_query(db, "SELECT id FROM dst ORDER BY id") == [(1,)]
        assert run_cli(["migrate"], root).returncode == Exit.LOCK_NOT_ACQUIRED
    finally:
        go.write_text("continue")
        holder.wait(timeout=TIMEOUT)


def test_waiter_acquires_the_lock_and_finds_no_pending_work(tmp_path):
    """A waiter that succeeds after the holder finishes exits 0 (spec Section 11.1)."""
    ready = tmp_path / "ready"
    go = tmp_path / "go"
    db = tmp_path / "build" / "probe.db"
    support.unit(tmp_path, "m1", {"up.sql": "CREATE TABLE dst (id INTEGER PRIMARY KEY);\n"})
    support.unit(
        tmp_path,
        "m2",
        {
            "migration.py": SLOW_BODY.format(ready=str(ready), go=str(go)),
        },
    )
    support.manifest(
        tmp_path,
        [
            {
                "id": "dst",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
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
    support.sqlite_config(tmp_path, db_path=db, timeout=50)

    holder = start_cli(["migrate"], tmp_path)
    _wait_for(ready)
    waiter = start_cli(["migrate"], tmp_path)
    time.sleep(0.3)
    assert waiter.poll() is None, "the waiter should still be blocked on the lock"
    go.write_text("continue")
    holder.wait(timeout=TIMEOUT)
    waiter_out, waiter_err = waiter.communicate(timeout=TIMEOUT)
    assert holder.returncode == Exit.OK
    assert waiter.returncode == Exit.OK
    assert "no pending migrations" in waiter_out
    assert support.db_query(db, "SELECT id FROM dst ORDER BY id") == [(1,), (2,)]


def test_lock_file_is_not_unlinked_and_is_reused(tmp_path):
    root = tmp_path
    db = root / "build" / "probe.db"
    support.unit(root, "m1", {"up.sql": "CREATE TABLE dst (id INTEGER PRIMARY KEY);\n"})
    support.manifest(
        root,
        [
            {
                "id": "dst",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
        ],
    )
    support.sqlite_config(root, db_path=db, timeout=5)
    assert run_cli(["migrate"], root).returncode == Exit.OK
    lock_file = db.with_name(db.name + ".m8lock")
    assert lock_file.exists()
    inode = os.stat(lock_file).st_ino
    assert run_cli(["migrate"], root).returncode == Exit.OK
    assert os.stat(lock_file).st_ino == inode
