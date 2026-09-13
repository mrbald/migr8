"""One owner decides what a command returns (spec Sections 11.1 and 11.5).

A ``migrate`` that made work durable has already told the operator so, on stdout
and in history. Everything after that -- closing the event log, writing a final
record -- is diagnostic, and diagnostics do not get to change the answer. The
other half of the same rule is that setting diagnostics up happens before any
database work, so a log file that cannot be opened fails with an exit code
instead of escaping the handlers that exist to produce one.
"""

from __future__ import annotations

import json

import pytest
import support

from migr8.diagnostics import RunLog
from migr8.errors import Exit

pytestmark = pytest.mark.sqlite


@pytest.fixture
def project(tmp_path):
    db = tmp_path / "build" / "probe.db"
    support.unit(tmp_path, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    support.unit(tmp_path, "m2", {"up.sql": "INSERT INTO t (id) VALUES (1);\n"})
    support.manifest(
        tmp_path,
        [
            {
                "id": "create-t",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
            {"id": "fill-t", "path": "m2", "language": "sql", "mode": "atomic", "entry": "up.sql"},
        ],
    )
    support.sqlite_config(tmp_path, db_path=db)
    return tmp_path, db


class _HandleThatFailsToClose:
    """Writes land; closing does not. A real log file can end a run this way."""

    def __init__(self, real):
        self._real = real

    def write(self, text: str) -> int:
        return self._real.write(text)

    def close(self) -> None:
        self._real.close()
        raise OSError("simulated failure closing the run log")


@pytest.fixture
def log_close_fails(monkeypatch):
    original = RunLog.__post_init__

    def patched(self) -> None:
        original(self)
        if self._handle is not None:
            self._handle = _HandleThatFailsToClose(self._handle)

    monkeypatch.setattr(RunLog, "__post_init__", patched)


# --- diagnostic teardown after an acknowledged outcome ------------------------------


def test_a_failing_log_close_does_not_change_an_acknowledged_success(
    project, log_close_fails, capsys
):
    root, db = project
    code = support.run_cli(
        ["migrate", "--json", "--log-file", str(root / "events.jsonl")], cwd=root
    )
    report = json.loads(capsys.readouterr().out)

    assert code == Exit.OK
    assert report["exit_code"] == 0 and report["outcome"] == "ok"
    assert report["executed"] == ["create-t", "fill-t"]
    # The database agrees with the report the operator was given.
    assert [row[2] for row in support.history(db)] == ["SUCCESS", "SUCCESS"]
    assert support.db_query(db, "SELECT id FROM t") == [(1,)]


def test_a_failing_log_close_does_not_change_a_reported_failure(project, log_close_fails, capsys):
    """A handled failure keeps its own exit code too."""
    root, db = project
    (root / "m2" / "up.sql").write_text("INSERT INTO absent (id) VALUES (1);\n")
    code = support.run_cli(
        ["migrate", "--json", "--log-file", str(root / "events.jsonl")], cwd=root
    )
    report = json.loads(capsys.readouterr().out)
    assert code == Exit.MIGRATION_FAILED
    assert report["exit_code"] == int(Exit.MIGRATION_FAILED)


def test_a_log_that_stops_writing_does_not_change_an_unknown_outcome(project, tmp_path):
    """Diagnostics fail independently: reporting exit 4 issues no further SQL.

    The event log is written while the engine reports the outcome, so this is
    where a diagnostic fault could plausibly reach a result that is already
    decided.
    """
    from migr8 import adapters
    from migr8.config import load as load_config
    from migr8.engine import Engine
    from migr8.manifest import load as load_manifest
    from migr8.staging import cleanup, stage

    class LostReply(Exception):
        """Not a sqlite3.Error, so the adapter reads it as a lost transport."""

    class _HandleThatFailsToWrite:
        def write(self, text: str) -> int:
            raise OSError("simulated failure writing the run log")

        def close(self) -> None:
            return None

    root, db = project
    config = load_config(root / "migr8.toml")
    manifest = load_manifest(root / "manifest.toml")
    adapter = adapters.create(config)

    statements: list[str] = []
    broken = {"failed": False}
    real_execute = adapter._metadata_execute
    real_connect = adapter.connect

    def traced_connect():
        real_connect()
        real_conn = adapter._conn

        class _Traced:
            def __getattr__(self, name):
                return getattr(real_conn, name)

            def execute(self, sql, *args):
                if broken["failed"]:
                    statements.append(str(sql))
                return real_conn.execute(sql, *args)

        adapter._conn = _Traced()

    def broken_execute(sql, params):
        # The first history INSERT admits ``create-t``; the second writes the
        # SUCCESS row for the atomic ``fill-t``, and that is the reply to lose.
        if str(sql).startswith('INSERT INTO "m8_history"'):
            if broken.get("armed"):
                broken["failed"] = True
                raise LostReply("simulated loss of the reply")
            broken["armed"] = True
        return real_execute(sql, params)

    adapter.connect = traced_connect
    adapter._metadata_execute = broken_execute

    log = RunLog("terminal-result-run")
    log._handle = _HandleThatFailsToWrite()
    capture = stage(manifest)
    try:
        report = Engine(config=config, adapter=adapter, capture=capture, log=log).run()
    finally:
        cleanup(capture.staging_root)

    assert broken["failed"]
    assert report.exit_code == Exit.UNKNOWN_OUTCOME
    assert report.connection_discarded
    assert statements == []
    assert "fill-t" not in {row[1] for row in support.history(db)}


# --- a defined result before any database work --------------------------------------


def test_a_log_file_that_cannot_be_opened_fails_before_the_database(project, capsys):
    root, db = project
    blocker = root / "not-a-directory"
    blocker.write_text("")
    code = support.run_cli(["migrate", "--log-file", str(blocker / "events.jsonl")], cwd=root)
    captured = capsys.readouterr()

    assert code == Exit.USAGE
    assert "the run log cannot be opened" in captured.err
    assert "run: " in captured.err
    # Nothing connected, so nothing was created.
    assert not db.exists()


def test_a_log_file_that_cannot_be_opened_is_reported_as_json_too(project, capsys):
    root, db = project
    blocker = root / "not-a-directory"
    blocker.write_text("")
    code = support.run_cli(
        ["migrate", "--json", "--log-file", str(blocker / "events.jsonl")], cwd=root
    )
    report = json.loads(capsys.readouterr().out)

    assert code == Exit.USAGE
    assert report["outcome"] == "usage_error"
    assert report["phase"] == "log_setup"
    assert report["run_id"]
    assert not db.exists()


# --- handled failures render the same way everywhere --------------------------------


def test_a_missing_configuration_reports_a_run_id_in_json(project, capsys):
    root, db = project
    code = support.run_cli(["migrate", "--json", "--config", str(root / "absent.toml")], cwd=root)
    report = json.loads(capsys.readouterr().out)

    assert code == Exit.USAGE
    assert report["exit_code"] == int(Exit.USAGE)
    assert report["outcome"] == "usage_error"
    assert report["run_id"]
    assert "absent.toml" in report["message"]


def test_a_missing_configuration_reports_a_run_id_on_stderr(project, capsys):
    root, db = project
    code = support.run_cli(["migrate", "--config", str(root / "absent.toml")], cwd=root)
    captured = capsys.readouterr()

    assert code == Exit.USAGE
    assert captured.out == ""
    assert "absent.toml" in captured.err
    assert "run: " in captured.err


@pytest.mark.parametrize("command", ["migrate", "validate", "status"])
def test_every_command_reports_a_handled_failure_the_same_way(project, capsys, command):
    root, db = project
    code = support.run_cli([command, "--json", "--manifest", str(root / "absent.toml")], cwd=root)
    report = json.loads(capsys.readouterr().out)
    assert code == Exit.USAGE
    assert report["run_id"] and report["outcome"] == "usage_error"
