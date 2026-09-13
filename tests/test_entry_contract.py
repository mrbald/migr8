"""The entry point is a synchronous ``migrate(ctx) -> None`` (spec Section 9.1).

The engine runs no event loop and consumes no generator. An entry point that
returns deferred work therefore never executes its body, so it must be refused
rather than recorded as a successful migration.

The second half covers the module namespace: a unit's modules are discarded when
it finishes, including helpers imported while ``migrate`` was running, so a
recovery run in the same process reloads the edited source.
"""

from __future__ import annotations

import sys

import pytest
import support

from migr8.errors import Exit, UnitError
from migr8.loader import PACKAGE_PREFIX, load_entry, package_name

pytestmark = pytest.mark.sqlite_probe


@pytest.fixture
def project(tmp_path):
    db = tmp_path / "build" / "probe.db"
    return tmp_path, support.sqlite_config(tmp_path, db_path=db), db


def _project(root, body, *, mode="restartable"):
    support.unit(root, "m1", {"up.py": body})
    support.unit(root, "m2", {"up.sql": "CREATE TABLE later (id INTEGER);\n"})
    return support.manifest(
        root,
        [
            {"id": "victim", "path": "m1", "language": "python", "mode": mode, "entry": "up.py"},
            {
                "id": "later",
                "path": "m2",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
        ],
    )


# --- deferred entry points ------------------------------------------------------------

WORK = '    ctx.ddl("CREATE TABLE widget (id INTEGER PRIMARY KEY)")\n'

ASYNC_DEF = f"async def migrate(ctx):\n{WORK}"
GENERATOR = f"def migrate(ctx):\n{WORK}    yield\n"
ASYNC_GENERATOR = f"async def migrate(ctx):\n{WORK}    yield\n"
WRAPPER_RETURNING_A_GENERATOR = (
    f"def _inner(ctx):\n{WORK}    yield\n\n\ndef migrate(ctx):\n    return _inner(ctx)\n"
)
WRAPPER_RETURNING_A_COROUTINE = (
    f"async def _inner(ctx):\n{WORK}\n\ndef migrate(ctx):\n    return _inner(ctx)\n"
)
RETURNS_A_VALUE = f'def migrate(ctx):\n{WORK}    return "done"\n'
VALID = f"def migrate(ctx):\n{WORK}"


@pytest.mark.parametrize("mode", ["atomic", "restartable"])
@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("async def", ASYNC_DEF),
        ("generator", GENERATOR),
        ("async generator", ASYNC_GENERATOR),
        ("wrapped generator", WRAPPER_RETURNING_A_GENERATOR),
        ("wrapped coroutine", WRAPPER_RETURNING_A_COROUTINE),
    ],
)
def test_deferred_entry_points_are_refused(project, mode, label, body, recwarn):
    root, config, db = project
    manifest = _project(root, body, mode=mode)
    report = support.migrate_report(config, manifest)

    assert not report.ok, f"{label} in {mode} mode was accepted"
    assert report.failed_migration == "victim"
    # The body never ran, no SUCCESS was written, and the next migration is untouched.
    assert support.db_query(db, "SELECT name FROM sqlite_master WHERE name = 'widget'") == []
    recorded = {row[1]: row[2] for row in support.history(db)}
    assert recorded.get("victim") != "SUCCESS"
    assert "later" not in recorded
    # A rejected coroutine is closed rather than left for the collector to warn about.
    assert not [w for w in recwarn.list if "never awaited" in str(w.message)]


def test_a_restartable_rejection_leaves_the_migration_active(project):
    root, config, db = project
    report = support.migrate_report(config, _project(root, ASYNC_DEF))
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert {row[1]: row[2] for row in support.history(db)} == {"victim": "ACTIVE"}


def test_an_atomic_rejection_writes_no_history_row(project):
    root, config, db = project
    report = support.migrate_report(config, _project(root, ASYNC_DEF, mode="atomic"))
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert support.history(db) == []


def test_a_non_none_return_value_is_refused(project):
    root, config, db = project
    report = support.migrate_report(config, _project(root, RETURNS_A_VALUE))
    assert not report.ok
    assert "the entry point returns None" in (report.message or "")
    assert {row[1]: row[2] for row in support.history(db)} == {"victim": "ACTIVE"}


def test_a_synchronous_entry_point_still_runs(project):
    root, config, db = project
    report = support.migrate_report(config, _project(root, VALID))
    assert report.ok
    assert [row[1] for row in support.history(db)] == ["victim", "later"]
    assert support.db_query(db, "SELECT name FROM sqlite_master WHERE name = 'widget'") == [
        ("widget",)
    ]


def test_the_loader_names_the_kind_it_refused(tmp_path):
    support.unit(tmp_path, "m1", {"up.py": ASYNC_DEF})
    with pytest.raises(UnitError, match="an async def function"):
        load_entry(migration_id="victim", staged_dir=tmp_path / "m1", entry="up.py")
    assert [n for n in sys.modules if n.startswith(PACKAGE_PREFIX)] == []


# --- unloading the unit's namespace ---------------------------------------------------

LAZY_HELPER = """\
def migrate(ctx):
    from . import helper

    with ctx.transaction():
        ctx.execute("INSERT INTO t (id) VALUES (%d)" % helper.VALUE)
    if ctx.attempt == 1:
        raise RuntimeError("stop after the first committed batch")
"""


def test_recovery_in_one_process_reloads_a_lazily_imported_helper(project):
    """Spec Section 9.1: a fresh module namespace for each run."""
    root, config, db = project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER);\n"})
    support.unit(root, "m2", {"up.py": LAZY_HELPER, "helper.py": "VALUE = 1\n"})
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
                "id": "lazy",
                "path": "m2",
                "language": "python",
                "mode": "restartable",
                "entry": "up.py",
            },
        ],
    )

    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    assert [n for n in sys.modules if n.startswith(PACKAGE_PREFIX)] == []

    (root / "m2" / "helper.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert support.migrate(config, manifest, recover="lazy") == Exit.OK
    assert support.db_query(db, "SELECT id FROM t ORDER BY id") == [(1,), (2,)]


def test_a_helper_imported_during_execution_is_unloaded_on_success(project):
    root, config, db = project
    body = 'def migrate(ctx):\n    from . import helper\n\n    ctx.log("v", value=helper.VALUE)\n'
    support.unit(root, "m1", {"up.py": body, "helper.py": "VALUE = 1\n"})
    manifest = support.manifest(
        root,
        [
            {
                "id": "lazy",
                "path": "m1",
                "language": "python",
                "mode": "restartable",
                "entry": "up.py",
            }
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    assert [n for n in sys.modules if n.startswith(PACKAGE_PREFIX)] == []


def test_unloading_one_unit_leaves_another_units_modules_alone():
    """Hex-encoded ids nest, so a bare prefix match would unload the wrong unit."""
    short, long = package_name("a"), package_name("ab")
    assert long.startswith(short)
    sys.modules[short] = sys.modules[f"{short}.helper"] = object()  # type: ignore[assignment]
    sys.modules[long] = sys.modules[f"{long}.helper"] = object()  # type: ignore[assignment]
    try:
        from migr8.loader import unload_namespace

        unload_namespace(short)
        assert short not in sys.modules and f"{short}.helper" not in sys.modules
        assert long in sys.modules and f"{long}.helper" in sys.modules
    finally:
        for name in (short, long, f"{short}.helper", f"{long}.helper"):
            sys.modules.pop(name, None)
