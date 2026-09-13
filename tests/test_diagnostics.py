"""Root-cause diagnostics and interruption safety.

These cover what an operator needs after a failure they did not watch: a
correlation id, an ordered event log, a machine-readable outcome, and an
interruption that is classified against the durable state rather than ending in
a traceback.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import support

from migr8.adapters.base import Boundary
from migr8.diagnostics import RunLog, default_log_path
from migr8.errors import Exit
from migr8.testing import hooks

pytestmark = pytest.mark.sqlite_probe

ENTRY = Path(__file__).resolve().parents[1] / "migr8"


@pytest.fixture
def project(tmp_path):
    db = tmp_path / "build" / "probe.db"
    config = support.sqlite_config(tmp_path, db_path=db)
    return tmp_path, config, db


def _simple(root: Path) -> Path:
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    support.unit(root, "m2", {"up.sql": "INSERT INTO t (id) VALUES (1);"})
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
            {"id": "seed-t", "path": "m2", "language": "sql", "mode": "atomic", "entry": "up.sql"},
        ],
    )


def cli(args: list[str], cwd: Path, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ENTRY), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        **kwargs,
    )


# --- the event log -----------------------------------------------------------------


def test_no_log_file_means_no_file(project):
    root, config, db = project
    _simple(root)
    assert cli(["migrate"], root).returncode == Exit.OK
    assert not list(root.glob("*.jsonl"))


def test_event_log_records_the_phases_in_order(project):
    root, config, db = project
    _simple(root)
    log_path = root / "run.jsonl"
    assert cli(["migrate", "--log-file", str(log_path)], root).returncode == Exit.OK

    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    names = [event["event"] for event in events]
    assert names[0] == "run_start"
    assert names[-1] == "run_end"
    for expected in (
        "preflight_passed",
        "connected",
        "lock_acquired",
        "plan",
        "migration_start",
        "migration_done",
    ):
        assert expected in names, expected

    # Every event carries the same correlation id and a monotonic elapsed time.
    run_ids = {event["run"] for event in events}
    assert len(run_ids) == 1
    elapsed = [event["elapsed"] for event in events]
    assert elapsed == sorted(elapsed)

    start = events[names.index("run_start")]
    assert start["command"] == "migrate"
    assert start["adapter"] == "sqlite-probe"
    assert start["units"] == 2
    plan = events[names.index("plan")]
    assert plan["pending"] == ["create-t", "seed-t"]
    assert events[-1]["outcome"] == "ok"


def test_event_log_names_the_failing_migration_and_phase(project):
    root, config, db = project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    support.unit(root, "m2", {"up.sql": "INSERT INTO absent_table (id) VALUES (1);"})
    support.manifest(
        root,
        [
            {
                "id": "create-t",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
            {"id": "broken", "path": "m2", "language": "sql", "mode": "atomic", "entry": "up.sql"},
        ],
    )
    log_path = root / "run.jsonl"
    result = cli(["migrate", "--log-file", str(log_path)], root)
    assert result.returncode == Exit.MIGRATION_FAILED

    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    end = events[-1]
    assert end["event"] == "run_end"
    assert end["outcome"] == "migration_failed"
    assert end["migration"] == "broken"
    assert end["phase"] == "migration_execution"
    assert "absent_table" in end["detail"]
    # The run id is printed so the operator can find that log entry.
    assert end["run"] in result.stderr


def test_author_log_lines_land_in_the_same_log(project):
    root, config, db = project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    support.unit(
        root,
        "m2",
        {
            "migration.py": (
                "def migrate(ctx):\n"
                "    with ctx.transaction() as tx:\n"
                "        tx.execute('INSERT INTO t (id) VALUES (1)')\n"
                "        ctx.progress.set('last', '1')\n"
                "    ctx.log('checkpointed', last_id=1, rows=1)\n"
            )
        },
    )
    support.manifest(
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
                "id": "fill",
                "path": "m2",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
        ],
    )
    log_path = root / "run.jsonl"
    assert cli(["migrate", "--log-file", str(log_path)], root).returncode == Exit.OK
    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    authored = [e for e in events if e["event"] == "migration_log"]
    assert len(authored) == 1
    assert authored[0]["message"] == "checkpointed"
    assert authored[0]["migration"] == "fill"
    assert authored[0]["attempt"] == 1
    assert authored[0]["last_id"] == 1


def test_log_file_can_come_from_the_environment(project):
    root, config, db = project
    _simple(root)
    log_path = root / "from-env.jsonl"
    env = {**os.environ, "MIGR8_LOG_FILE": str(log_path)}
    assert cli(["migrate"], root, env=env).returncode == Exit.OK
    assert log_path.is_file()


def test_log_appends_across_runs(project):
    root, config, db = project
    _simple(root)
    log_path = root / "run.jsonl"
    assert cli(["migrate", "--log-file", str(log_path)], root).returncode == Exit.OK
    first = len(log_path.read_text().splitlines())
    assert cli(["migrate", "--log-file", str(log_path)], root).returncode == Exit.OK
    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(events) > first
    assert len({event["run"] for event in events}) == 2


def test_redacted_fields_never_reach_the_log(tmp_path):
    log_path = tmp_path / "run.jsonl"
    log = RunLog("abc123", log_path)
    log.event("probe", password="hunter2", dsn="host=secret", migration="m", params=(1,))
    log.close()
    written = log_path.read_text()
    assert "hunter2" not in written
    assert "host=secret" not in written
    record = json.loads(written)
    assert record["migration"] == "m"
    assert "params" not in record


def test_default_log_path_prefers_the_explicit_argument(tmp_path, monkeypatch):
    monkeypatch.setenv("MIGR8_LOG_FILE", str(tmp_path / "env.jsonl"))
    assert default_log_path(tmp_path / "explicit.jsonl") == tmp_path / "explicit.jsonl"
    assert default_log_path(None) == tmp_path / "env.jsonl"
    monkeypatch.delenv("MIGR8_LOG_FILE")
    assert default_log_path(None) is None


# --- the machine-readable outcome --------------------------------------------------


def test_migrate_json_outcome_on_success(project):
    root, config, db = project
    _simple(root)
    result = cli(["migrate", "--json"], root)
    assert result.returncode == Exit.OK
    report = json.loads(result.stdout)
    assert report["outcome"] == "ok"
    assert report["exit_code"] == 0
    assert report["executed"] == ["create-t", "seed-t"]
    assert report["adapter"] == "sqlite-probe"
    assert report["namespace"].endswith("probe.db")
    assert report["failed_migration"] is None
    assert report["duration_seconds"] >= 0
    assert len(report["run_id"]) == 32


def test_migrate_json_outcome_names_the_failure(project):
    root, config, db = project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    support.unit(root, "m2", {"up.sql": "INSERT INTO absent_table (id) VALUES (1);"})
    support.manifest(
        root,
        [
            {
                "id": "create-t",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
            {"id": "broken", "path": "m2", "language": "sql", "mode": "atomic", "entry": "up.sql"},
        ],
    )
    result = cli(["migrate", "--json"], root)
    assert result.returncode == Exit.MIGRATION_FAILED
    report = json.loads(result.stdout)
    assert report["outcome"] == "migration_failed"
    assert report["exit_code"] == 3
    assert report["failed_migration"] == "broken"
    assert report["phase"] == "migration_execution"
    assert report["executed"] == ["create-t"]
    assert report["connection_discarded"] is False


def test_migrate_json_outcome_carries_the_recovery_command(project):
    root, config, db = project
    support.unit(
        root, "m1", {"migration.py": ("def migrate(ctx):\n    raise RuntimeError('stop')\n")}
    )
    support.manifest(
        root,
        [
            {
                "id": "stuck",
                "path": "m1",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            }
        ],
    )
    assert cli(["migrate"], root).returncode == Exit.MIGRATION_FAILED
    support.unit(root, "m1", {"migration.py": "def migrate(ctx):\n    pass\n"})
    result = cli(["migrate", "--json"], root)
    assert result.returncode == Exit.VALIDATION
    report = json.loads(result.stdout)
    assert report["outcome"] == "validation_failed"
    assert report["recovery_command"] == "migr8 migrate --recover stuck"


def test_read_only_reports_carry_the_run_id(project):
    root, config, db = project
    _simple(root)
    assert cli(["migrate"], root).returncode == Exit.OK
    for command in ("status", "validate"):
        result = cli([command, "--json"], root)
        assert result.returncode == Exit.OK
        assert len(json.loads(result.stdout)["run_id"]) == 32
        assert "run:" in cli([command], root).stdout


# --- interruption ------------------------------------------------------------------

SLOW = """\
import pathlib
import time

READY = pathlib.Path({ready!r})


def migrate(ctx):
    last = int(ctx.progress.get("last", "0"))
    if last < 1:
        with ctx.transaction() as tx:
            tx.execute("INSERT INTO t (id) VALUES (1)")
            ctx.progress.set("last", "1")
    READY.write_text("working")
    time.sleep(60)
"""


def _slow_project(root: Path, ready: Path) -> None:
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    support.unit(root, "m2", {"migration.py": SLOW.format(ready=str(ready))})
    support.manifest(
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
                "id": "slow",
                "path": "m2",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
        ],
    )


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_interruption_between_commits_rolls_back_and_retains_active(project, sig):
    """Neither Ctrl-C nor a supervisor's SIGTERM may end in a traceback."""
    root, config, db = project
    ready = root / "ready"
    _slow_project(root, ready)
    log_path = root / "run.jsonl"
    process = subprocess.Popen(
        [sys.executable, str(ENTRY), "migrate", "--log-file", str(log_path)],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert time.monotonic() < deadline, "the migration never started working"
            assert process.poll() is None, "the migration exited early"
            time.sleep(0.05)
        process.send_signal(sig)
        _out, err = process.communicate(timeout=60)
    finally:
        if process.poll() is None:  # pragma: no cover - defensive
            process.kill()

    assert process.returncode == Exit.MIGRATION_FAILED, err
    assert "Traceback" not in err
    assert "interrupted" in err
    assert "remains ACTIVE" in err

    # The first batch is durable and the migration is still ACTIVE, so a rerun
    # converges.
    assert support.db_query(db, "SELECT id FROM t ORDER BY id") == [(1,)]
    rows = {row[1]: row[2] for row in support.history(db)}
    assert rows == {"create-t": "SUCCESS", "slow": "ACTIVE"}

    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    end = events[-1]
    assert end["event"] == "run_end"
    assert end["outcome"] == "migration_failed"
    assert end["phase"] == "interrupted"


def test_interruption_during_a_commit_is_an_unknown_outcome(project):
    """A commit in flight may already be durable, so it must not be called clean."""
    root, config, db = project
    _simple(root)

    state: dict = {}

    def hook(adapter):
        real_commit = adapter.commit
        armed = {"value": False}

        def on_boundary(boundary, phase):
            if boundary == Boundary.ATOMIC_COMPLETION and phase == hooks.BEFORE_COMMIT:
                armed["value"] = True

        hooks.register(on_boundary)

        def commit():
            if armed["value"]:
                armed["value"] = False
                state["fired"] = True
                raise KeyboardInterrupt("simulated Ctrl-C while committing")
            return real_commit()

        adapter.commit = commit

    report = support.migrate_report(config, root / "manifest.toml", adapter_hook=hook)
    assert state.get("fired")
    assert report.exit_code == Exit.UNKNOWN_OUTCOME
    assert report.connection_discarded
    assert "unknown outcome" in report.message
    assert report.phase == "atomic_completion"


def test_an_unexpected_engine_fault_still_produces_a_defined_outcome(project):
    """A defect must not leave the operator with a traceback and no exit code."""
    root, config, db = project
    _simple(root)

    def hook(adapter):
        def broken_lock() -> None:
            raise ZeroDivisionError("injected defect")

        adapter.acquire_lock = broken_lock

    report = support.migrate_report(config, root / "manifest.toml", adapter_hook=hook)
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert report.phase == "internal_error"
    assert "ZeroDivisionError" in report.message
    assert "rolled back" in report.message
