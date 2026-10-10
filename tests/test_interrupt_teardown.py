"""An interrupt during teardown never replaces the outcome the run reached.

SIGINT arrives as ``KeyboardInterrupt`` and the CLI turns SIGTERM and SIGHUP into
the same exception.  The interrupts here are injected at fixed points in a real
SQLite run: after a durable commit whose reply is lost, in the connection
discard, in the connection close, in settlement, in staging cleanup, while the
report is written, before the run starts and before the engine is entered.
"""

from __future__ import annotations

import json
import signal
import sys

import pytest
import support

from migr8 import cli
from migr8.adapters import base
from migr8.adapters import sqlite as sq
from migr8.diagnostics import RunLog
from migr8.engine import Engine
from migr8.errors import Exit

pytestmark = pytest.mark.sqlite

SIGNALS = [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]


@pytest.fixture
def project(tmp_path):
    support.simple_sql_project(tmp_path)
    return tmp_path


def run_json(root, capsys, *argv):
    code = support.run_cli(["migrate", "--json", *argv], cwd=root)
    return code, json.loads(capsys.readouterr().out)


def interrupt(signum):
    """Deliver *signum* the way the OS would, through the CLI's handlers."""
    signal.raise_signal(signum)


def lose_the_completion_commit_reply(monkeypatch):
    """Interrupt the first completion commit after it lands, which latches exit 4."""
    armed = {"on": False}
    original_insert = base.Adapter.insert_success_row
    original_commit = sq.SqliteAdapter._do_commit

    def insert(self, **kwargs):
        original_insert(self, **kwargs)
        armed["on"] = True

    def commit(self):
        original_commit(self)  # the commit lands and the reply is lost
        if armed["on"]:
            armed["on"] = False
            interrupt(signal.SIGINT)

    monkeypatch.setattr(base.Adapter, "insert_success_row", insert)
    monkeypatch.setattr(sq.SqliteAdapter, "_do_commit", commit)


@pytest.mark.parametrize("signum", SIGNALS, ids=lambda s: s.name)
def test_an_interrupt_in_discard_keeps_a_latched_exit_4(project, capsys, monkeypatch, signum):
    lose_the_completion_commit_reply(monkeypatch)
    original_discard = sq.SqliteAdapter.discard

    def discard(self):
        original_discard(self)
        interrupt(signum)

    monkeypatch.setattr(sq.SqliteAdapter, "discard", discard)

    code, report = run_json(project, capsys)

    assert code == Exit.UNKNOWN_OUTCOME
    assert report["exit_code"] == 4
    assert report["outcome"] == "outcome_unknown"
    assert "unknown outcome" in report["message"]
    assert "before any migration work began" not in report["message"]
    assert report["connection_discarded"] is True


def test_an_interrupt_inside_settlement_after_a_latched_exit_4_keeps_exit_4(
    project, capsys, monkeypatch
):
    """Recording the failure is interrupted in teardown and again in settlement."""
    lose_the_completion_commit_reply(monkeypatch)

    def fail(self, error, code, *, discarded=False):
        interrupt(signal.SIGTERM)

    monkeypatch.setattr(Engine, "_fail", fail)

    code, report = run_json(project, capsys)

    assert code == Exit.UNKNOWN_OUTCOME
    assert report["exit_code"] == 4
    assert report["outcome"] == "outcome_unknown"
    assert "unknown outcome" in report["message"]
    assert "before any migration work began" not in report["message"]


@pytest.mark.parametrize("signum", SIGNALS, ids=lambda s: s.name)
def test_an_interrupt_in_staging_cleanup_keeps_a_clean_exit_0(project, capsys, monkeypatch, signum):
    original = cli.cleanup

    def cleanup(path):
        original(path)
        interrupt(signum)

    monkeypatch.setattr(cli, "cleanup", cleanup)

    code, report = run_json(project, capsys)

    assert code == Exit.OK
    assert report["outcome"] == "ok"
    assert report["executed"] == ["create-t", "insert-t"]
    assert report["message"] is None
    assert any("staging directory" in warning for warning in report["warnings"])
    rows = support.history(project / "build" / "probe.db")
    assert [row[2] for row in rows] == ["SUCCESS", "SUCCESS"]


@pytest.mark.parametrize("signum", SIGNALS, ids=lambda s: s.name)
def test_an_interrupted_close_discards_the_connection_and_says_so(
    project, capsys, monkeypatch, signum
):
    original_close = sq.SqliteAdapter.close
    discarded = []
    original_discard = sq.SqliteAdapter.discard

    def close(self):
        interrupt(signum)
        original_close(self)

    def discard(self):
        discarded.append(True)
        original_discard(self)

    monkeypatch.setattr(sq.SqliteAdapter, "close", close)
    monkeypatch.setattr(sq.SqliteAdapter, "discard", discard)

    code, report = run_json(project, capsys)

    assert code == Exit.OK
    assert report["outcome"] == "ok"
    assert discarded
    assert report["connection_discarded"] is True
    assert any("closing the connection was interrupted" in w for w in report["warnings"])


def test_an_interrupt_after_a_clean_close_carries_no_discard_note(project, capsys, monkeypatch):
    original_close = sq.SqliteAdapter.close

    def close(self):
        original_close(self)
        interrupt(signal.SIGTERM)

    monkeypatch.setattr(sq.SqliteAdapter, "close", close)

    code, report = run_json(project, capsys)

    assert code == Exit.OK
    assert report["outcome"] == "ok"
    assert report["connection_discarded"] is False
    assert not any("discarded" in w for w in report["warnings"])


class InterruptedStream:
    """Passes the first write through, then interrupts, as a signal during the write would."""

    def __init__(self, real) -> None:
        self._real = real
        self.writes = 0

    def write(self, text: str) -> int:
        self.writes += 1
        written = self._real.write(text)
        if self.writes == 1:
            interrupt(signal.SIGTERM)
        return written

    def flush(self) -> None:
        self._real.flush()


def test_an_interrupt_while_the_report_is_written_writes_it_once(project, capsys, monkeypatch):
    stream = InterruptedStream(sys.stdout)
    monkeypatch.setattr(sys, "stdout", stream)

    code = support.run_cli(["migrate", "--json"], cwd=project)
    report = json.loads(capsys.readouterr().out)

    assert code == Exit.OK
    assert report["outcome"] == "ok"
    assert stream.writes == 1


def test_an_interrupt_before_the_run_starts_says_so(project, capsys, monkeypatch):
    original_event = RunLog.event

    def event(self, name, **fields):
        if name == "run_start":
            interrupt(signal.SIGTERM)
        original_event(self, name, **fields)

    monkeypatch.setattr(RunLog, "event", event)

    code, report = run_json(project, capsys)

    assert code == Exit.MIGRATION_FAILED
    assert "interrupted (KeyboardInterrupt) before it started" in report["message"]
    assert "nothing was sent to the database" in report["message"]
    assert report["connection_discarded"] is False
    assert report["warnings"] == []
    assert not (project / "build" / "probe.db").exists()


def test_an_interrupt_before_the_engine_is_entered_keeps_the_old_message(
    project, capsys, monkeypatch
):
    def stage(manifest):
        interrupt(signal.SIGTERM)

    monkeypatch.setattr(cli, "stage", stage)

    code, report = run_json(project, capsys)

    assert code == Exit.MIGRATION_FAILED
    assert report["message"] == "interrupted before any migration work began"
    assert report["phase"] == "interrupted"
