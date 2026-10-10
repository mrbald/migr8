"""``status`` against a live server when a pending unit does not compile.

The offline suite covers the report logic on SQLite (``tests/test_status_preflight.py``).
These two check that the history read behind it works on the server adapters.
NOT RUN by the offline gates: they need the disposable services of ``testenv/dbctl.sh``.
"""

from __future__ import annotations

import json

import pytest
import support

from migr8.errors import Exit

BROKEN = "def migrate(ctx)\n    pass\n"


def _two_applied_then_broken(root, config, create_sql: str, insert_sql: str):
    support.unit(root, "m1", {"up.sql": create_sql})
    support.unit(root, "m2", {"up.sql": insert_sql})
    two = [
        {
            "id": "create-t",
            "path": "m1",
            "language": "sql",
            "mode": "restartable",
            "entry": "up.sql",
        },
        {"id": "seed-t", "path": "m2", "language": "sql", "mode": "atomic", "entry": "up.sql"},
    ]
    manifest = support.manifest(root, two)
    assert support.migrate(config, manifest) == Exit.OK
    support.unit(root, "m3", {"migration.py": BROKEN})
    broken = {
        "id": "broken-py",
        "path": "m3",
        "language": "python",
        "mode": "restartable",
        "entry": "migration.py",
    }
    return support.manifest(root, [*two, broken])


def _assert_history_and_problem(config, manifest, capsys):
    capsys.readouterr()
    code = support.run_cli(
        ["status", "--json", "--config", str(config), "--manifest", str(manifest)]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == Exit.VALIDATION
    assert report["success_count"] == 2
    states = {item["id"]: item["state"] for item in report["migrations"]}
    assert states == {"create-t": "SUCCESS", "seed-t": "SUCCESS", "broken-py": "PENDING"}
    assert [item["kind"] for item in report["problems"]] == ["preflight"]
    broken = next(item for item in report["migrations"] if item["id"] == "broken-py")
    assert "does not compile" in broken["problem"]


@pytest.mark.postgres
def test_postgres_status_reports_history_with_a_pending_unit_that_does_not_compile(
    pg_project, capsys
):
    root, config, _schema = pg_project
    manifest = _two_applied_then_broken(
        root, config, "CREATE TABLE t (id integer PRIMARY KEY)", "INSERT INTO t (id) VALUES (1)"
    )
    _assert_history_and_problem(config, manifest, capsys)


@pytest.mark.oracle
def test_oracle_status_reports_history_with_a_pending_unit_that_does_not_compile(
    oracle_project, capsys
):
    root, config, _schema = oracle_project
    manifest = _two_applied_then_broken(
        root,
        config,
        "CREATE TABLE t (id NUMBER(10) PRIMARY KEY);\n",
        "INSERT INTO t (id) VALUES (1)",
    )
    _assert_history_and_problem(config, manifest, capsys)
