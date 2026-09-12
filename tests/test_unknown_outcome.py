"""Unknown operation outcomes and the run latch (spec Section 7.2).

These tests induce the failure by replacing the adapter's own ``commit`` method,
so they exercise the real engine boundary, the real latch and the real facade.
They are **wrapper simulations, not transport evidence**: no packet is lost and
no server session dies.  Real directional commit-acknowledgement evidence is
produced against Oracle and PostgreSQL in ``tests/integration``.
"""

from __future__ import annotations

import pytest

import support
from migr8.adapters.base import Boundary, OutcomeClass
from migr8.errors import ContractViolationError, Exit, UnknownOutcomeError
from migr8.latch import LatchState, RunLatch
from migr8.testing import hooks

pytestmark = pytest.mark.sqlite_probe


class SimulatedTransportLoss(Exception):
    """Not a ``sqlite3.Error``, so the adapter classifies it as a lost transport."""


# --- the latch itself ---------------------------------------------------------------

def test_latch_is_single_shot_and_not_clearable():
    latch = RunLatch()
    assert latch.state is LatchState.OPEN
    first = UnknownOutcomeError("lost", operation="commit")
    latch.latch_unknown(first)
    assert latch.unknown and latch.latched
    second = UnknownOutcomeError("other", operation="commit")
    latch.latch_unknown(second)
    assert latch.error is first  # the first outcome is the one reported
    with pytest.raises(UnknownOutcomeError) as info:
        latch.check()
    assert info.value is first


def test_contract_violation_latch_is_distinct_from_unknown():
    latch = RunLatch()
    latch.latch_violation(ContractViolationError("broke the transaction"))
    assert latch.latched and not latch.unknown
    assert latch.state is LatchState.CONTRACT_VIOLATION


def test_unknown_outcome_is_catchable_but_the_latch_is_not_clearable():
    """The spec anticipates author code catching this; the latch is the defence."""
    latch = RunLatch()
    error = UnknownOutcomeError("lost", operation="commit")
    latch.latch_unknown(error)
    try:
        latch.check()
    except Exception:  # noqa: BLE001 - deliberately the way author code would
        pass
    with pytest.raises(UnknownOutcomeError):
        latch.check()


# --- engine behaviour ----------------------------------------------------------------

@pytest.fixture
def project(tmp_path):
    db = tmp_path / "build" / "probe.db"
    config = support.sqlite_config(tmp_path, db_path=db)
    return tmp_path, config, db


def _batch_project(root, body: str):
    support.unit(root, "m1", {"up.sql": "CREATE TABLE dst (id INTEGER PRIMARY KEY);\n"})
    support.unit(root, "m2", {"migration.py": body})
    return support.manifest(root, [
        {"id": "dst", "path": "m1", "language": "sql", "mode": "restartable",
         "entry": "up.sql"},
        {"id": "copy", "path": "m2", "language": "python", "mode": "restartable",
         "entry": "migration.py"},
    ])


def _fail_commit(adapter, *, boundary: str, occurrence: int = 1, durable: bool = True):
    """Arm a commit failure at one engine-owned boundary.

    Synchronisation uses the engine's own boundary hooks, because a signal from
    migration code is not enough: further driver calls can precede the COMMIT.

    ``durable=True`` performs the real COMMIT and then suppresses the
    acknowledgement; ``durable=False`` never reaches the server.
    """
    state = {"seen": 0, "armed": False, "fired": False, "rollbacks_after_latch": 0}
    real_commit = adapter.commit
    real_rollback = adapter.rollback

    def on_boundary(seen_boundary: str, phase: str) -> None:
        if seen_boundary == boundary and phase == hooks.BEFORE_COMMIT:
            state["seen"] += 1
            if state["seen"] == occurrence:
                state["armed"] = True

    hooks.register(on_boundary)

    def commit():
        if state["armed"]:
            state["armed"] = False
            state["fired"] = True
            if durable:
                real_commit()
            raise SimulatedTransportLoss(
                f"simulated loss of acknowledgement at {boundary} #{occurrence}"
            )
        return real_commit()

    def rollback():
        if adapter.latch is not None and adapter.latch.latched:
            state["rollbacks_after_latch"] += 1
        return real_rollback()

    adapter.commit = commit
    adapter.rollback = rollback
    return state


def test_classification_treats_an_unrecognised_error_as_a_lost_transport(project):
    root, config, db = project
    from migr8 import adapters
    from migr8.config import load as load_config

    adapter = adapters.create(load_config(config))
    assert adapter.classify_exception(SimulatedTransportLoss()) \
        is OutcomeClass.COMMUNICATION_FAILURE


BODY = """\
def migrate(ctx):
    last = int(ctx.progress.get("last_id", "0"))
    for value in (1, 2, 3):
        if value <= last:
            continue
        with ctx.transaction() as tx:
            tx.execute("INSERT INTO dst (id) VALUES (?)", (value,))
            ctx.progress.set("last_id", str(value))
"""


def test_lost_batch_acknowledgement_exits_four_and_stops(project):
    """Request never delivered: the batch's work must be absent afterwards."""
    root, config, db = project
    manifest = _batch_project(root, BODY)
    state: dict = {}

    def hook(adapter):
        state["injector"] = _fail_commit(
            adapter, boundary=Boundary.RESTARTABLE_BATCH, occurrence=2, durable=False
        )

    report = support.migrate_report(config, manifest, adapter_hook=hook)
    injector = state["injector"]
    assert report.exit_code == Exit.UNKNOWN_OUTCOME
    assert report.connection_discarded
    assert "unknown outcome" in report.message
    # The adapter was discarded rather than closed, and no cleanup SQL ran.
    assert injector["fired"]
    assert injector["rollbacks_after_latch"] == 0
    # First batch committed; the second never reached the server.
    assert support.db_query(db, "SELECT id FROM dst ORDER BY id") == [(1,)]
    assert support.progress(db) == [("copy", "last_id", "1")]
    assert [r[2] for r in support.history(db)] == ["SUCCESS", "ACTIVE"]


def test_durable_batch_with_lost_response_exits_four_and_keeps_the_work(project):
    """Commit durable, acknowledgement withheld: the work must be present."""
    root, config, db = project
    manifest = _batch_project(root, BODY)
    state: dict = {}
    report = support.migrate_report(
        config, manifest,
        adapter_hook=lambda a: state.__setitem__("injector", _fail_commit(
            a, boundary=Boundary.RESTARTABLE_BATCH, occurrence=2, durable=True)),
    )
    assert report.exit_code == Exit.UNKNOWN_OUTCOME
    assert state["injector"]["rollbacks_after_latch"] == 0
    assert support.db_query(db, "SELECT id FROM dst ORDER BY id") == [(1,), (2,)]
    assert support.progress(db) == [("copy", "last_id", "2")]


def test_a_fresh_run_reconciles_after_an_unknown_outcome(project):
    root, config, db = project
    manifest = _batch_project(root, BODY)
    assert support.migrate_report(
        config, manifest, adapter_hook=lambda a: _fail_commit(
            a, boundary=Boundary.RESTARTABLE_BATCH, occurrence=2, durable=True),
    ).exit_code == Exit.UNKNOWN_OUTCOME
    # A new invocation reacquires the lock and converges from the durable state.
    assert support.migrate(config, manifest) == Exit.OK
    assert support.db_query(db, "SELECT id FROM dst ORDER BY id") == [(1,), (2,), (3,)]
    assert support.progress(db) == []


CATCHING_BODY = """\
def migrate(ctx):
    try:
        with ctx.transaction() as tx:
            tx.execute("INSERT INTO dst (id) VALUES (1)")
            ctx.progress.set("last_id", "1")
    except Exception:
        pass
    # The run is latched; this must not be allowed to proceed.
    with ctx.transaction() as tx:
        tx.execute("INSERT INTO dst (id) VALUES (999)")
"""


def test_caught_unknown_outcome_cannot_resume_the_run(project):
    """Spec Section 14.2 group 5: no continuation after a caught unknown outcome."""
    root, config, db = project
    manifest = _batch_project(root, CATCHING_BODY)
    state: dict = {}
    report = support.migrate_report(
        config, manifest,
        adapter_hook=lambda a: state.__setitem__("injector", _fail_commit(
            a, boundary=Boundary.RESTARTABLE_BATCH, occurrence=1, durable=True)),
    )
    assert report.exit_code == Exit.UNKNOWN_OUTCOME
    assert state["injector"]["rollbacks_after_latch"] == 0
    # The first batch is durable; the post-catch work never ran.
    assert support.db_query(db, "SELECT id FROM dst ORDER BY id") == [(1,)]
    assert [r[2] for r in support.history(db)] == ["SUCCESS", "ACTIVE"]


SWALLOW_EVERYTHING = """\
def migrate(ctx):
    for value in (1, 2, 3):
        try:
            with ctx.transaction() as tx:
                tx.execute("INSERT INTO dst (id) VALUES (?)", (value,))
        except BaseException:
            continue
"""


def test_latched_run_cannot_complete_even_if_every_error_is_swallowed(project):
    root, config, db = project
    manifest = _batch_project(root, SWALLOW_EVERYTHING)
    report = support.migrate_report(
        config, manifest, adapter_hook=lambda a: _fail_commit(
            a, boundary=Boundary.RESTARTABLE_BATCH, occurrence=1, durable=True),
    )
    assert report.exit_code == Exit.UNKNOWN_OUTCOME
    assert [r[2] for r in support.history(db)] == ["SUCCESS", "ACTIVE"]
    assert support.db_query(db, "SELECT id FROM dst ORDER BY id") == [(1,)]


def test_lost_admission_acknowledgement_runs_no_migration_code(project):
    """No code runs until ACTIVE admission is acknowledged (spec Section 5.2)."""
    root, config, db = project
    marker = root / "code-ran"
    body = (
        "import pathlib\n\n\n"
        "def migrate(ctx):\n"
        f"    pathlib.Path({str(marker)!r}).write_text('yes')\n"
    )
    manifest = _batch_project(root, body)
    # The second restartable admission is the one that precedes Python code.
    state: dict = {}
    report = support.migrate_report(
        config, manifest,
        adapter_hook=lambda a: state.__setitem__("injector", _fail_commit(
            a, boundary=Boundary.RESTARTABLE_ADMISSION, occurrence=2, durable=False)),
    )
    assert state["injector"]["fired"]
    assert report.exit_code == Exit.UNKNOWN_OUTCOME
    assert not marker.exists()
    assert [r[2] for r in support.history(db)] == ["SUCCESS"]


def test_lost_atomic_completion_acknowledgement_is_not_inferred_as_rollback(project):
    root, config, db = project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    support.unit(root, "m2", {"up.sql": "INSERT INTO t (id) VALUES (5);"})
    manifest = support.manifest(root, [
        {"id": "create", "path": "m1", "language": "sql", "mode": "restartable",
         "entry": "up.sql"},
        {"id": "work", "path": "m2", "language": "sql", "mode": "atomic", "entry": "up.sql"},
    ])
    state: dict = {}
    report = support.migrate_report(
        config, manifest,
        adapter_hook=lambda a: state.__setitem__("injector", _fail_commit(
            a, boundary=Boundary.ATOMIC_COMPLETION, durable=True)),
    )
    assert state["injector"]["fired"]
    assert report.exit_code == Exit.UNKNOWN_OUTCOME
    assert state["injector"]["rollbacks_after_latch"] == 0
    # The commit was durable, so both the work and the SUCCESS row are present.
    assert support.db_query(db, "SELECT id FROM t") == [(5,)]
    assert [r[2] for r in support.history(db)] == ["SUCCESS", "SUCCESS"]
    # A later run sees the success and skips it.
    assert support.migrate(config, manifest) == Exit.OK
