"""What a failure is allowed to say, and what it must not (spec Section 11.5).

Driver messages quote the data that produced them: PostgreSQL echoes the literal
in ``invalid input syntax for type integer: "..."``, and a bound value lands in
the text. Default diagnostics therefore name a failure by exception type and
engine error code, plus the operation, phase and identity around it.

The canaries below stand in for a bound value. No real credential is used.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
import support

from migr8.diagnostics import RunLog
from migr8.errors import Exit

pytestmark = pytest.mark.sqlite_probe

CANARY = "REVIEW_CANARY_91"


@pytest.fixture
def project(tmp_path):
    db = tmp_path / "build" / "probe.db"
    return tmp_path, support.sqlite_config(tmp_path, db_path=db), db


def _run_with_a_poisoned_driver(root, config, manifest, needle, failure):
    """Run one migration whose driver failure quotes a canary, capturing every surface."""
    from migr8 import adapters
    from migr8.config import load as load_config
    from migr8.engine import Engine
    from migr8.manifest import load as load_manifest
    from migr8.staging import cleanup, stage

    stderr = io.StringIO()
    handler = logging.StreamHandler(stderr)
    logger = logging.getLogger("migr8")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)

    log_path = root / "events.jsonl"
    loaded = load_config(config)
    adapter = adapters.create(loaded)
    real_run = adapter._run

    def poisoned(text, params):
        if needle in text:
            raise failure
        return real_run(text, params)

    adapter._run = poisoned
    capture = stage(load_manifest(manifest))
    log = RunLog("canary-run", path=log_path)
    try:
        report = Engine(config=loaded, adapter=adapter, capture=capture, log=log).run()
    finally:
        log.close()
        cleanup(capture.staging_root)
        logger.removeHandler(handler)
        logger.setLevel(previous)
    return report, log_path.read_text(encoding="utf-8"), stderr.getvalue()


def _one_python_migration(root, body):
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER);\n"})
    support.unit(root, "m2", {"up.py": body})
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
                "language": "python",
                "mode": "restartable",
                "entry": "up.py",
            },
        ],
    )


INSERTING = """\
def migrate(ctx):
    with ctx.transaction():
        ctx.execute("INSERT INTO t (id) VALUES (1)")
"""


class PoisonedRejection(Exception):
    """Stands in for a driver error whose message quotes the offending value."""


def _poison():
    return PoisonedRejection(
        f'invalid input syntax for type integer: "{CANARY}"\n'
        f"LINE 1: INSERT INTO t (id) VALUES ('{CANARY}')"
    )


def test_a_driver_message_does_not_reach_the_report_the_log_or_stderr(project):
    root, config, db = project
    manifest = _one_python_migration(root, INSERTING)
    report, events, stderr = _run_with_a_poisoned_driver(
        root, config, manifest, "INSERT INTO t", _poison()
    )

    assert not report.ok
    assert CANARY not in report.to_json()
    assert CANARY not in events
    assert CANARY not in stderr
    # Enough is kept to locate the failure.
    assert report.failed_migration == "victim"
    assert report.phase is not None
    terminal = json.loads(events.splitlines()[-1])
    assert terminal["migration"] == "victim"
    assert "PoisonedRejection" in terminal["detail"]


def test_a_canary_in_an_exception_chain_does_not_reach_the_report(project):
    """The cause of a wrapped failure is not rendered either."""
    root, config, db = project
    body = """\
def migrate(ctx):
    try:
        with ctx.transaction():
            ctx.execute("INSERT INTO t (id) VALUES (1)")
    except Exception as exc:
        raise RuntimeError("wrapping failure") from exc
"""
    manifest = _one_python_migration(root, body)
    report, events, stderr = _run_with_a_poisoned_driver(
        root, config, manifest, "INSERT INTO t", _poison()
    )
    assert not report.ok
    assert CANARY not in report.to_json()
    assert CANARY not in events
    assert CANARY not in stderr


def test_a_canary_in_the_first_migration_does_not_reach_the_report(project):
    """A failure in the migration that prepares the namespace reports the same way."""
    root, config, db = project
    manifest = _one_python_migration(root, INSERTING)
    report, events, stderr = _run_with_a_poisoned_driver(
        root, config, manifest, "CREATE TABLE t", _poison()
    )
    assert not report.ok
    assert CANARY not in report.to_json()
    assert CANARY not in events
    assert CANARY not in stderr


def test_the_description_names_the_type_and_the_engine_error_code():
    """Spec Section 11.5: a code identifies the fault without carrying the data."""
    import sqlite3

    from migr8.adapters.sqlite_probe import SqliteProbeAdapter

    probe = SqliteProbeAdapter.__new__(SqliteProbeAdapter)
    try:
        sqlite3.connect(":memory:").execute(f"SELECT {CANARY}")
    except sqlite3.Error as exc:
        described = probe.describe_exception(exc)
    assert "sqlite3.OperationalError" in described
    assert "SQLITE_ERROR" in described
    assert CANARY not in described


def test_an_adapter_without_a_code_for_an_exception_still_names_its_type():
    from migr8.adapters.sqlite_probe import SqliteProbeAdapter

    probe = SqliteProbeAdapter.__new__(SqliteProbeAdapter)
    exc = PoisonedRejection(CANARY)
    assert probe.error_code(exc) is None
    described = probe.describe_exception(exc)
    assert "PoisonedRejection" in described
    assert CANARY not in described


def test_a_safe_description_keeps_engine_text_and_drops_everything_else():
    """The CLI has no adapter, so this is the description its handlers use.

    An engine-authored error is written for an operator and passes through; any
    other exception is named by module and type, because its message may quote
    the data that produced it.
    """
    from migr8.errors import MigrationFailedError, describe_safely

    engine_authored = MigrationFailedError(
        "the batch was rejected", phase="restartable_batch", migration_id="victim"
    )
    described = describe_safely(engine_authored)
    assert "the batch was rejected" in described
    assert "phase=restartable_batch" in described and "migration=victim" in described

    described = describe_safely(PoisonedRejection(CANARY))
    assert described.endswith("PoisonedRejection")
    assert CANARY not in described


def test_the_sqlite_probe_reports_its_symbolic_result_code(project):
    root, config, db = project
    support.unit(root, "m1", {"up.sql": "INSERT INTO absent (id) VALUES (1);\n"})
    manifest = support.manifest(
        root,
        [{"id": "broken", "path": "m1", "language": "sql", "mode": "atomic", "entry": "up.sql"}],
    )
    report = support.migrate_report(config, manifest)
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert "sqlite3.OperationalError" in (report.message or "")
    assert "SQLITE_ERROR" in (report.message or "")
    # The driver's text named the missing object; the report names the code.
    assert "absent" not in (report.message or "")


# --- SQLSTATE classification (spec Section 7.2) ---------------------------------------


class TestPostgresClassification:
    """Receiving an error code does not by itself establish a definite outcome."""

    @pytest.fixture(autouse=True)
    def _adapter(self):
        psycopg = pytest.importorskip("psycopg")
        from migr8.adapters.postgres import PostgresAdapter

        self.psycopg = psycopg
        self.adapter = PostgresAdapter.__new__(PostgresAdapter)

    def _classify(self, error):
        from migr8.adapters.base import OutcomeClass

        return self.adapter.classify_exception(error), OutcomeClass

    def test_statement_completion_unknown_is_not_a_rejection(self):
        """40003 is the server saying it does not know whether the statement ran.

        https://www.postgresql.org/docs/17/errcodes-appendix.html
        """
        error = self.psycopg.errors.StatementCompletionUnknown()
        assert error.sqlstate == "40003"
        outcome, OutcomeClass = self._classify(error)
        assert outcome is OutcomeClass.COMMUNICATION_FAILURE

    def test_the_connection_exception_class_stays_indefinite(self):
        outcome, OutcomeClass = self._classify(self.psycopg.errors.ConnectionException())
        assert outcome is OutcomeClass.COMMUNICATION_FAILURE

    @pytest.mark.parametrize(
        "name", ["UniqueViolation", "SerializationFailure", "DeadlockDetected", "SyntaxError"]
    )
    def test_an_ordinary_rejection_is_still_definite(self, name):
        """A neighbouring class-40 code must not be swept up with 40003."""
        outcome, OutcomeClass = self._classify(getattr(self.psycopg.errors, name)())
        assert outcome is OutcomeClass.SERVER_REJECTION

    def test_an_unclassified_exception_is_indefinite(self):
        outcome, OutcomeClass = self._classify(RuntimeError("no sqlstate at all"))
        assert outcome is OutcomeClass.COMMUNICATION_FAILURE

    def test_a_client_side_refusal_is_definite(self):
        outcome, OutcomeClass = self._classify(self.psycopg.ProgrammingError("refused locally"))
        assert outcome is OutcomeClass.SERVER_REJECTION


# --- the boundaries outside Engine.run() (spec Section 11.5) -------------------------


@pytest.fixture
def captured_migr8_log():
    """Everything the ``migr8`` loggers write, at the level ``--verbose`` selects."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("migr8")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield stream
    logger.removeHandler(handler)
    logger.setLevel(previous)


def _initialized(root, config):
    manifest = _one_python_migration(root, INSERTING)
    assert support.migrate(config, manifest) == Exit.OK
    return manifest


def test_a_canary_in_a_metadata_read_does_not_reach_the_status_report(project, capsys):
    """``status`` goes through inspect_metadata(), which Engine.run() does not own."""
    import sqlite3

    from migr8.adapters.sqlite_probe import SqliteProbeAdapter

    root, config, db = project
    _initialized(root, config)
    real_query = SqliteProbeAdapter._metadata_query

    def poisoned(self, sql, params):
        if "m8_meta" in sql:
            # A real SQLite error, so it carries both the canary in its message
            # and the symbolic result code the report is allowed to name.
            sqlite3.connect(":memory:").execute(f"SELECT {CANARY}")
        return real_query(self, sql, params)

    SqliteProbeAdapter._metadata_query = poisoned
    try:
        code = support.run_cli(["status", "--json"], cwd=root)
    finally:
        SqliteProbeAdapter._metadata_query = real_query
    captured = capsys.readouterr()

    assert code == Exit.METADATA_DAMAGED
    assert CANARY not in captured.out
    assert CANARY not in captured.err
    # The fault is still named, by type and by SQLite's own result code.
    assert "sqlite3.OperationalError" in captured.out
    assert "SQLITE_ERROR" in captured.out


def test_a_canary_at_adapter_construction_does_not_reach_stderr_or_the_log(
    project, capsys, captured_migr8_log
):
    """The CLI's unexpected-failure handler is the last thing that runs; it is safe too."""
    from migr8 import cli as cli_module

    root, config, db = project
    _initialized(root, config)
    events = root / "events.jsonl"
    real_create = cli_module.adapters.create

    def poisoned(configuration):
        raise RuntimeError(f"cannot reach host={CANARY}")

    cli_module.adapters.create = poisoned
    try:
        code = support.run_cli(
            ["--verbose", "migrate", "--json", "--log-file", str(events)], cwd=root
        )
    finally:
        cli_module.adapters.create = real_create
    captured = capsys.readouterr()

    assert code == Exit.USAGE
    assert CANARY not in captured.out
    assert CANARY not in captured.err
    assert CANARY not in captured_migr8_log.getvalue()
    assert CANARY not in events.read_text(encoding="utf-8")
    # Named by type, with the run id an operator needs to correlate.
    report = json.loads(captured.out)
    assert "RuntimeError" in report["message"]
    assert report["run_id"]


def test_a_canary_in_a_cleanup_warning_does_not_reach_the_log(project, captured_migr8_log):
    """Closing and rolling back are best-effort, and they report by type as well."""
    root, config, db = project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER);\n"})
    support.unit(root, "m2", {"up.sql": "INSERT INTO absent (id) VALUES (1);\n"})
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
            {"id": "victim", "path": "m2", "language": "sql", "mode": "atomic", "entry": "up.sql"},
        ],
    )

    def hook(adapter):
        real_close, real_rollback = adapter.close, adapter.rollback

        def close():
            real_close()
            raise RuntimeError(f"the session did not close: {CANARY}")

        def rollback():
            real_rollback()
            raise RuntimeError(f"the rollback did not complete: {CANARY}")

        adapter.close, adapter.rollback = close, rollback

    report = support.migrate_report(config, manifest, adapter_hook=hook)
    assert report.exit_code == Exit.MIGRATION_FAILED
    written = captured_migr8_log.getvalue()
    assert CANARY not in written
    assert CANARY not in report.to_json()
    assert "did not complete cleanly" in written


def test_the_run_log_reports_its_own_failure_by_type(captured_migr8_log):
    """A log that stops working says so without quoting what it was handed."""
    log = RunLog("canary-run")

    class _Refusing:
        def write(self, text):
            raise OSError(f"cannot write to {CANARY}")

        def close(self):
            return None

    log._handle = _Refusing()
    log.event("run_end", outcome="ok")
    assert CANARY not in captured_migr8_log.getvalue()
    assert "cannot write the run log" in captured_migr8_log.getvalue()
