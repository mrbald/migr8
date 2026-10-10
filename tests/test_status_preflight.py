"""``status`` reports the history even when a pending unit fails preflight.

``validate`` keeps its order: preflight first, with exit 2 before any connection.
``status`` reads the history, attaches the preflight problem and exits with that
problem's own code, unless a database condition coexists, in which case the
database condition's code decides and both problems are reported.
"""

from __future__ import annotations

import json

import pytest
import support

from migr8.adapters.sqlite import SqliteAdapter
from migr8.errors import Exit

pytestmark = pytest.mark.sqlite

BROKEN = "def migrate(ctx)\n    pass\n"
FAILS = "def migrate(ctx):\n    raise RuntimeError('stop')\n"

SQL_ENTRIES = [
    {"id": "create-t", "path": "m1", "language": "sql", "mode": "restartable", "entry": "up.sql"},
    {"id": "seed-t", "path": "m2", "language": "sql", "mode": "atomic", "entry": "up.sql"},
]
PYTHON_ENTRY = {
    "id": "py",
    "path": "m3",
    "language": "python",
    "mode": "restartable",
    "entry": "migration.py",
}


@pytest.fixture
def root(tmp_path):
    support.sqlite_config(tmp_path)
    support.unit(tmp_path, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    support.unit(tmp_path, "m2", {"up.sql": "INSERT INTO t (id) VALUES (1);\n"})
    return tmp_path


def use_manifest(root, entries, name="manifest.toml"):
    return support.manifest(root, entries, name=name)


def status(root, *extra, capsys):
    code = support.run_cli(["status", *extra], cwd=root)
    return code, capsys.readouterr().out


def test_status_lists_the_history_and_the_pending_unit_that_does_not_compile(root, capsys):
    use_manifest(root, SQL_ENTRIES)
    assert support.run_cli(["migrate"], cwd=root) == Exit.OK
    support.unit(root, "m3", {"migration.py": BROKEN})
    use_manifest(root, [*SQL_ENTRIES, PYTHON_ENTRY])
    capsys.readouterr()

    code, out = status(root, "--json", capsys=capsys)
    report = json.loads(out)

    assert code == Exit.VALIDATION
    assert report["exit_code"] == 2
    assert report["success_count"] == 2
    assert report["pending_count"] == 1
    states = {item["id"]: item["state"] for item in report["migrations"]}
    assert states == {"create-t": "SUCCESS", "seed-t": "SUCCESS", "py": "PENDING"}
    invalid = next(item for item in report["migrations"] if item["id"] == "py")
    assert "does not compile" in invalid["problem"]
    assert all(item["problem"] is None for item in report["migrations"] if item["id"] != "py")
    assert report["problem_kind"] == "preflight"
    assert [item["kind"] for item in report["problems"]] == ["preflight"]
    assert report["problems"][0]["exit_code"] == 2
    assert report["problems"][0]["migration_id"] == "py"


def test_the_text_form_shows_the_rows_and_the_problem(root, capsys):
    use_manifest(root, SQL_ENTRIES)
    assert support.run_cli(["migrate"], cwd=root) == Exit.OK
    support.unit(root, "m3", {"migration.py": BROKEN})
    use_manifest(root, [*SQL_ENTRIES, PYTHON_ENTRY])
    capsys.readouterr()

    code, out = status(root, capsys=capsys)

    assert code == Exit.VALIDATION
    assert "history:   2 successful, 1 pending" in out
    assert "fails preflight: migration 'py' does not compile" in out
    assert "preflight: migration 'py' does not compile" in out
    assert out.rstrip().endswith("exit: 2")


def test_an_unsupported_capability_keeps_its_own_exit_code(root, capsys):
    use_manifest(root, SQL_ENTRIES)
    assert support.run_cli(["migrate"], cwd=root) == Exit.OK
    support.unit(root, "m5", {"up.sql": "INSERT INTO t (id) VALUES (2);\n"})
    unsupported = {
        "id": "needs-valid",
        "path": "m5",
        "language": "sql",
        "mode": "atomic",
        "entry": "up.sql",
        "require_valid": [{"name": "SOME_PROC", "type": "PROCEDURE"}],
    }
    use_manifest(root, [*SQL_ENTRIES, unsupported])
    capsys.readouterr()

    code, out = status(root, "--json", capsys=capsys)
    report = json.loads(out)

    assert code == Exit.USAGE
    assert report["exit_code"] == 1
    assert report["success_count"] == 2
    assert report["problems"][0]["kind"] == "preflight"
    assert report["problems"][0]["exit_code"] == 1


def failing_active_project(root):
    """Two applied units, then a Python unit that fails and stays ACTIVE."""
    support.unit(root, "m3", {"migration.py": FAILS})
    use_manifest(root, [*SQL_ENTRIES, PYTHON_ENTRY])
    assert support.run_cli(["migrate"], cwd=root) == Exit.MIGRATION_FAILED


def test_an_active_row_and_an_invalid_pending_unit_are_both_reported(root, capsys):
    failing_active_project(root)
    support.unit(root, "m4", {"migration.py": BROKEN})
    later = {**PYTHON_ENTRY, "id": "later", "path": "m4"}
    use_manifest(root, [*SQL_ENTRIES, PYTHON_ENTRY, later])
    capsys.readouterr()

    code, out = status(root, "--json", capsys=capsys)
    report = json.loads(out)

    assert code == Exit.VALIDATION
    assert report["active_id"] == "py"
    states = {item["id"]: item["state"] for item in report["migrations"]}
    assert states["py"] == "ACTIVE"
    assert states["later"] == "PENDING"
    assert next(i for i in report["migrations"] if i["id"] == "later")["problem"]
    assert [item["kind"] for item in report["problems"]] == ["preflight"]


def test_a_database_condition_decides_the_code_and_both_problems_are_reported(root, capsys):
    failing_active_project(root)
    # The ACTIVE unit's source is amended, which is a recovery-required condition,
    # and a later pending unit does not compile.
    support.unit(root, "m3", {"migration.py": FAILS + "# amended\n"})
    support.unit(root, "m4", {"migration.py": BROKEN})
    later = {**PYTHON_ENTRY, "id": "later", "path": "m4"}
    use_manifest(root, [*SQL_ENTRIES, PYTHON_ENTRY, later])
    capsys.readouterr()

    code, out = status(root, "--json", capsys=capsys)
    report = json.loads(out)

    assert code == Exit.VALIDATION
    assert report["problem_kind"] == "recovery required"
    assert report["recovery_command"] == "migr8 migrate --recover py"
    assert [item["kind"] for item in report["problems"]] == ["recovery required", "preflight"]
    assert report["problems"][1]["migration_id"] == "later"

    code, text = status(root, capsys=capsys)
    assert code == Exit.VALIDATION
    assert "recovery required:" in text
    assert "also preflight: migration 'later' does not compile" in text


def test_exit_6_wins_over_a_preflight_problem_and_both_are_reported(root, capsys):
    support.unit(root, "m3", {"migration.py": BROKEN})
    use_manifest(root, [*SQL_ENTRIES, PYTHON_ENTRY])

    code, out = status(root, "--json", capsys=capsys)
    report = json.loads(out)

    assert code == Exit.NOT_INITIALIZED
    assert report["problem_kind"] == "not initialized"
    assert [item["kind"] for item in report["problems"]] == ["not initialized", "preflight"]
    assert report["problems"][1]["exit_code"] == 2
    assert [item["id"] for item in report["migrations"]] == ["create-t", "seed-t", "py"]


def test_exit_7_wins_over_a_preflight_problem(root, capsys):
    use_manifest(root, SQL_ENTRIES)
    assert support.run_cli(["migrate"], cwd=root) == Exit.OK
    support.db_exec(root / "build" / "probe.db", "DELETE FROM m8_meta")
    support.unit(root, "m3", {"migration.py": BROKEN})
    use_manifest(root, [*SQL_ENTRIES, PYTHON_ENTRY])
    capsys.readouterr()

    code, out = status(root, "--json", capsys=capsys)
    report = json.loads(out)

    assert code == Exit.METADATA_DAMAGED
    assert report["problem_kind"] == "metadata damaged"
    assert report["problems"][-1]["kind"] == "preflight"
    assert len(report["problems"]) == 2


def test_validate_still_stops_at_preflight_before_connecting(root, capsys, monkeypatch):
    support.unit(root, "m3", {"migration.py": BROKEN})
    use_manifest(root, [*SQL_ENTRIES, PYTHON_ENTRY])

    def refuse(self):
        raise AssertionError("validate connected before preflight finished")

    monkeypatch.setattr(SqliteAdapter, "connect", refuse)

    code = support.run_cli(["validate", "--json"], cwd=root)
    report = json.loads(capsys.readouterr().out)

    assert code == Exit.VALIDATION
    assert "does not compile" in report["message"]
    assert "migrations" not in report


def test_a_clean_status_has_no_problems(root, capsys):
    use_manifest(root, SQL_ENTRIES)
    assert support.run_cli(["migrate"], cwd=root) == Exit.OK
    capsys.readouterr()

    code, out = status(root, "--json", capsys=capsys)
    report = json.loads(out)

    assert code == Exit.OK
    assert report["problems"] == []
    assert report["problem"] is None
