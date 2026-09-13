"""The offline plan lint and the Python compilation check (spec Section 11.2).

Two contracts are under test.  Every command compiles the Python units before it
reaches a database, so a migration that does not parse fails preflight instead of
being admitted, marked ACTIVE and then failing on import.  And
``validate --offline`` performs that whole check with no connection, no secret
and no namespace, which is what a pipeline can run before a target exists.

What passing does not establish is on the other side of the line and is asserted
nowhere here: that the SQL is valid on the server, that the privileges are there,
or that a restartable migration converges.
"""

from __future__ import annotations

import json

import pytest
import support

from migr8.errors import Exit

pytestmark = pytest.mark.sqlite

BROKEN = "def migrate(ctx)\n    pass\n"
WORKING = 'def migrate(ctx):\n    with ctx.transaction() as tx:\n        tx.execute("SELECT 1")\n'


@pytest.fixture
def project(tmp_path):
    """One SQL migration and one Python migration, with the database unwritten."""
    db = tmp_path / "build" / "probe.db"
    config = support.sqlite_config(tmp_path, db_path=db)
    support.unit(tmp_path, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    support.unit(tmp_path, "m2", {"migration.py": WORKING})
    manifest = support.manifest(
        tmp_path,
        [
            {
                "id": "create-t",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
            {
                "id": "backfill",
                "path": "m2",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
        ],
    )
    return tmp_path, config, manifest, db


def cli(command: str, config, manifest, *extra: str) -> int:
    return support.run_cli([command, "--config", str(config), "--manifest", str(manifest), *extra])


# --- Python that does not compile --------------------------------------------------


def test_a_python_unit_that_does_not_compile_never_reaches_the_database(project):
    """The reviewed defect: validation passed, execution left the migration ACTIVE."""
    root, config, manifest, db = project
    (root / "m2" / "migration.py").write_text(BROKEN, encoding="utf-8")

    assert cli("validate", config, manifest, "--offline") == Exit.VALIDATION
    assert cli("validate", config, manifest) == Exit.VALIDATION
    assert cli("migrate", config, manifest) == Exit.VALIDATION
    assert not db.exists(), "preflight failed, so nothing connected and nothing was written"


def test_a_helper_that_does_not_compile_is_refused_too(project):
    """The entry imports its helpers, so a helper that does not parse fails the unit."""
    root, config, manifest, _db = project
    support.write(root / "m2" / "assumptions.py", "def check(:\n")
    report = support.report_for_offline(config, manifest)
    assert report.exit_code == Exit.VALIDATION
    assert "assumptions.py" in (report.problem or "")


def test_the_reported_location_names_the_file_and_the_line(project):
    root, config, manifest, _db = project
    (root / "m2" / "migration.py").write_text("def migrate(ctx):\n    x = (1\n", encoding="utf-8")
    report = support.report_for_offline(config, manifest)
    assert "migration.py:" in (report.problem or "")
    assert "backfill" in (report.problem or "")


def test_a_source_with_a_null_byte_is_refused(project):
    root, config, manifest, _db = project
    (root / "m2" / "migration.py").write_bytes(b"def migrate(ctx):\n    pass\x00\n")
    assert support.report_for_offline(config, manifest).exit_code == Exit.VALIDATION


def test_a_python_unit_that_compiles_passes(project):
    _root, config, manifest, _db = project
    assert support.report_for_offline(config, manifest).exit_code == Exit.OK


# --- what offline does, and what it does not do ------------------------------------


def test_offline_connects_to_nothing(project):
    """No database file, no lock file: connecting is what creates both."""
    root, config, manifest, db = project
    assert cli("validate", config, manifest, "--offline") == Exit.OK
    assert not db.exists()
    assert not db.with_suffix(db.suffix + ".m8lock").exists()


def test_offline_neither_imports_migration_code_nor_writes_bytecode(project):
    """Compilation is not execution, and it leaves no artefact beside the unit."""
    root, config, manifest, _db = project
    marker = root / "imported"
    support.write(
        root / "m2" / "migration.py",
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\n\n"
        "def migrate(ctx):\n    pass\n",
    )
    assert cli("validate", config, manifest, "--offline") == Exit.OK
    assert not marker.exists()
    assert not (root / "m2" / "__pycache__").exists()


def test_the_report_lists_what_was_checked(project):
    _root, config, manifest, _db = project
    report = support.report_for_offline(config, manifest)
    assert report.checks
    assert any("Python compilation" in item for item in report.checks)
    assert report.server == "not connected"
    assert report.metadata_state == "not read"
    assert [item.state for item in report.migrations] == ["UNREAD", "UNREAD"]


def test_the_json_report_is_machine_readable(project):
    _root, config, manifest, _db = project
    document = json.loads(support.report_for_offline(config, manifest).to_json())
    assert document["command"] == "validate --offline"
    assert document["exit_code"] == 0
    assert [entry["id"] for entry in document["migrations"]] == ["create-t", "backfill"]


def test_a_statement_the_backend_refuses_fails_offline(tmp_path):
    """Backend admission is part of the lint: the adapter is selected, not connected."""
    config = support.sqlite_config(tmp_path)
    support.unit(tmp_path, "m1", {"up.sql": "VACUUM;\n"})
    manifest = support.manifest(
        tmp_path,
        [{"id": "vac", "path": "m1", "language": "sql", "mode": "atomic", "entry": "up.sql"}],
    )
    assert support.report_for_offline(config, manifest).exit_code != Exit.OK


# --- the approved plan artifact ----------------------------------------------------


def approve(tmp_path, config, manifest):
    """Write the approved plan artifact the way a pipeline keeps one."""
    path = tmp_path / "approved.json"
    path.write_text(support.report_for_offline(config, manifest).to_json(), encoding="utf-8")
    return path


def test_an_unchanged_plan_matches_its_approved_artifact(project):
    root, config, manifest, _db = project
    baseline = approve(root, config, manifest)
    assert cli("validate", config, manifest, "--offline", "--baseline", str(baseline)) == Exit.OK


def test_an_edited_published_unit_is_refused(project):
    """The manifest alone cannot show this: the unit and its fingerprint agree."""
    root, config, manifest, _db = project
    baseline = approve(root, config, manifest)
    support.write(root / "m1" / "up.sql", "CREATE TABLE t (id INTEGER PRIMARY KEY, x TEXT);\n")
    report = support.report_for_offline(config, manifest, baseline=baseline)
    assert report.exit_code == Exit.VALIDATION
    assert "fingerprint" in (report.problem or "")


def test_a_reordered_plan_is_refused(project):
    root, config, manifest, _db = project
    baseline = approve(root, config, manifest)
    support.manifest(
        root,
        [
            {
                "id": "backfill",
                "path": "m2",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
            {
                "id": "create-t",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
        ],
    )
    report = support.report_for_offline(config, manifest, baseline=baseline)
    assert report.exit_code == Exit.VALIDATION
    assert "approved plan" in (report.problem or "")


def test_a_removed_published_migration_is_refused(project):
    root, config, manifest, _db = project
    baseline = approve(root, config, manifest)
    support.manifest(
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
    report = support.report_for_offline(config, manifest, baseline=baseline)
    assert report.exit_code == Exit.VALIDATION
    assert "nothing here" in (report.problem or "")


def test_new_work_after_the_approved_plan_is_allowed(project):
    root, config, manifest, _db = project
    baseline = approve(root, config, manifest)
    support.unit(root, "m3", {"up.sql": "CREATE TABLE u (id INTEGER PRIMARY KEY);\n"})
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
                "id": "backfill",
                "path": "m2",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
            {
                "id": "create-u",
                "path": "m3",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
        ],
    )
    assert support.report_for_offline(config, manifest, baseline=baseline).exit_code == Exit.OK


def test_the_baseline_also_guards_the_online_command(project):
    """The comparison is the plan's, so it runs before the connection either way."""
    root, config, manifest, _db = project
    baseline = approve(root, config, manifest)
    assert cli("migrate", config, manifest) == Exit.OK
    support.write(root / "m1" / "up.sql", "CREATE TABLE t (id INTEGER PRIMARY KEY, x TEXT);\n")
    assert cli("validate", config, manifest, "--baseline", str(baseline)) == Exit.VALIDATION


@pytest.mark.parametrize(
    "content",
    ['{"migrations": "not a list of entries"}', '{"migrations": [1, 2]}', "[]", "not json at all"],
)
def test_a_baseline_that_is_not_a_plan_report_is_a_usage_error(project, content):
    root, config, manifest, _db = project
    baseline = root / "approved.json"
    baseline.write_text(content, encoding="utf-8")
    report = support.report_for_offline(config, manifest, baseline=baseline)
    assert report.exit_code == Exit.USAGE
    assert "baseline plan" in (report.problem or "")


def test_a_missing_baseline_is_a_usage_error(project):
    root, config, manifest, _db = project
    missing = root / "nowhere.json"
    assert cli("validate", config, manifest, "--offline", "--baseline", str(missing)) == Exit.USAGE
