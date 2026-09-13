"""Oracle integration: the specification's primary execution and recovery gate.

These run against a real Oracle server. Nothing here is mocked, and nothing here
certifies Oracle 19c: see docs/ACCEPTANCE.md for the recorded release.
"""

from __future__ import annotations

import pytest
import support

from migr8.errors import Exit

pytestmark = [pytest.mark.oracle]


# --- initialization and metadata -------------------------------------------------


def test_initialization_creates_and_validates_the_layout(oracle_project, oracle_query):
    root, config, schema = oracle_project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE orders (id NUMBER(10) PRIMARY KEY);\n"})
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            }
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    meta = oracle_query(
        "SELECT layout_version, adapter, lock_provider, lock_binding, target_namespace FROM m8_meta"
    )
    assert meta == [(1, "oracle", "dbms_lock", "dbms_lock:4711", schema)]
    tables = sorted(
        row[0]
        for row in oracle_query(
            "SELECT object_name FROM all_objects WHERE owner = :owner AND "
            "object_type = 'TABLE' AND object_name LIKE 'M8%'",
            owner=schema,
        )
    )
    assert tables == ["M8_HISTORY", "M8_META", "M8_PROGRESS"]
    assert oracle_query(
        "SELECT COUNT(*) FROM all_indexes WHERE owner = :owner AND "
        "index_name = 'M8_HISTORY_ONE_ACTIVE'",
        owner=schema,
    ) == [(1,)]
    assert oracle_query('SELECT status, "MODE" FROM m8_history') == [("SUCCESS", "restartable")]


def test_one_active_index_is_a_real_unique_function_based_index(oracle_project, oracle_query):
    root, config, schema = oracle_project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE orders (id NUMBER(10) PRIMARY KEY);\n"})
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            }
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    rows = oracle_query(
        "SELECT uniqueness, status, funcidx_status FROM all_indexes "
        "WHERE owner = :owner AND index_name = 'M8_HISTORY_ONE_ACTIVE'",
        owner=schema,
    )
    assert rows == [("UNIQUE", "VALID", "ENABLED")]

    # The one-active rule is enforced by the database, not only by the engine.
    import oracledb

    def insert_active(seq: int, migration_id: str) -> None:
        oracle_query(
            "INSERT INTO m8_history (seq, migration_id, fingerprint, first_fingerprint, "
            'language, "MODE", status, attempt, started_at, tool_version) VALUES '
            "(:seq, :name, 'fp1:0', 'fp1:0', 'sql', 'restartable', 'ACTIVE', 1, "
            "SYSTIMESTAMP, 'test')",
            seq=seq,
            name=migration_id,
        )

    insert_active(2, "second")
    with pytest.raises(oracledb.IntegrityError):
        insert_active(3, "third")


def test_read_only_commands_do_not_initialize(oracle_project, oracle_query):
    root, config, schema = oracle_project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE orders (id NUMBER(10));\n"})
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            }
        ],
    )
    report = support.report_for("status", config, manifest)
    assert report.exit_code == Exit.NOT_INITIALIZED
    assert oracle_query(
        "SELECT COUNT(*) FROM all_objects WHERE owner = :owner AND object_name LIKE 'M8%'",
        owner=schema,
    ) == [(0,)]


def test_populated_history_without_a_marker_is_damage(oracle_project, oracle_query):
    root, config, schema = oracle_project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE orders (id NUMBER(10));\n"})
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            }
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    oracle_query("DELETE FROM m8_meta")
    assert support.migrate(config, manifest) == Exit.METADATA_DAMAGED


def test_missing_object_after_initialization_is_damage(oracle_project, oracle_query):
    root, config, schema = oracle_project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE orders (id NUMBER(10));\n"})
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            }
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    oracle_query("DROP INDEX m8_history_one_active")
    assert support.migrate(config, manifest) == Exit.METADATA_DAMAGED
    report = support.report_for("validate", config, manifest)
    assert "m8_history_one_active" in report.problem.lower()


def test_lock_binding_mismatch_is_refused(oracle_project, oracle_query):
    root, config, schema = oracle_project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE orders (id NUMBER(10));\n"})
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            }
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    other = support.write(root / "other.toml", config.read_text().replace("id = 4711", "id = 4712"))
    assert support.migrate(other, manifest) == Exit.USAGE
    report = support.report_for("status", other, manifest)
    assert "lock binding mismatch" in report.problem


# --- atomic -----------------------------------------------------------------------


def _orders_project(
    root,
    second_sql: str,
    *,
    mode: str = "atomic",
    language: str = "sql",
    entry: str = "up.sql",
    required=None,
):
    support.unit(
        root,
        "m1",
        {
            "up.sql": (
                "CREATE TABLE orders (\n"
                "  id     NUMBER(10) NOT NULL PRIMARY KEY,\n"
                "  region VARCHAR2(2 CHAR)\n"
                ")"
            )
        },
    )
    support.unit(root, "m2", {entry: second_sql})
    entries = [
        {
            "id": "create-orders",
            "path": "m1",
            "language": "sql",
            "mode": "restartable",
            "entry": "up.sql",
        },
        {"id": "work", "path": "m2", "language": language, "mode": mode, "entry": entry},
    ]
    if required is not None:
        entries[1]["require_valid"] = required
    return support.manifest(root, entries)


def test_atomic_dml_and_history_commit_together(oracle_project, oracle_query):
    root, config, schema = oracle_project
    manifest = _orders_project(root, "INSERT INTO orders (id, region) VALUES (1, 'EU')")
    assert support.migrate(config, manifest) == Exit.OK
    assert oracle_query("SELECT id, region FROM orders") == [(1, "EU")]
    assert oracle_query("SELECT migration_id, status, attempt FROM m8_history ORDER BY seq") == [
        ("create-orders", "SUCCESS", 1),
        ("work", "SUCCESS", None),
    ]


def test_atomic_statement_error_rolls_back_and_writes_no_history(oracle_project, oracle_query):
    root, config, schema = oracle_project
    manifest = _orders_project(
        root, "INSERT INTO orders (id, region) VALUES (1, 'TOO LONG FOR THE COLUMN')"
    )
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    assert oracle_query("SELECT COUNT(*) FROM orders") == [(0,)]
    assert oracle_query("SELECT migration_id FROM m8_history") == [("create-orders",)]


def test_atomic_ddl_is_prohibited(oracle_project, oracle_query):
    """Oracle DDL commits independently, so it can never be part of atomic work."""
    root, config, schema = oracle_project
    manifest = _orders_project(root, "CREATE TABLE sneaky (id NUMBER(10))")
    assert support.migrate(config, manifest) == Exit.USAGE
    assert oracle_query(
        "SELECT COUNT(*) FROM all_objects WHERE owner = :owner AND object_name = 'SNEAKY'",
        owner=schema,
    ) == [(0,)]


def test_atomic_plsql_block_is_admitted(oracle_project, oracle_query):
    root, config, schema = oracle_project
    manifest = _orders_project(
        root,
        (
            "BEGIN\n"
            "  FOR i IN 1 .. 3 LOOP\n"
            "    INSERT INTO orders (id, region) VALUES (i, 'EU');\n"
            "    EXIT WHEN i = 3;\n"
            "  END LOOP;\n"
            "END;"
        ),
    )
    assert support.migrate(config, manifest) == Exit.OK
    assert oracle_query("SELECT COUNT(*) FROM orders") == [(3,)]


def test_transaction_identity_tripwire_detects_a_commit(oracle_project, oracle_query):
    """A PL/SQL block that commits breaks the atomic contract (spec Section 5.1)."""
    root, config, schema = oracle_project
    manifest = _orders_project(
        root, ("BEGIN\n  INSERT INTO orders (id, region) VALUES (1, 'EU');\n  COMMIT;\nEND;")
    )
    report = support.migrate_report(config, manifest)
    assert report.exit_code == Exit.CONTRACT_VIOLATION
    assert "broke its transaction" in report.message
    assert "manual remediation" in report.message
    # The committed row is durable; no success row was written.
    assert oracle_query("SELECT COUNT(*) FROM orders") == [(1,)]
    assert oracle_query("SELECT migration_id FROM m8_history") == [("create-orders",)]


def test_no_further_work_after_a_detected_violation(oracle_project, oracle_query):
    root, config, schema = oracle_project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE orders (id NUMBER(10) PRIMARY KEY)"})
    support.unit(root, "m2", {"up.sql": ("BEGIN INSERT INTO orders (id) VALUES (1); COMMIT; END;")})
    support.unit(root, "m3", {"up.sql": "CREATE TABLE later_table (id NUMBER(10))"})
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
            {
                "id": "violator",
                "path": "m2",
                "language": "sql",
                "mode": "atomic",
                "entry": "up.sql",
            },
            {
                "id": "later",
                "path": "m3",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
        ],
    )
    assert support.migrate(config, manifest) == Exit.CONTRACT_VIOLATION
    assert oracle_query(
        "SELECT COUNT(*) FROM all_objects WHERE owner = :owner AND object_name = 'LATER_TABLE'",
        owner=schema,
    ) == [(0,)]


def test_atomic_python_uses_native_binds(oracle_project, oracle_query):
    root, config, schema = oracle_project
    manifest = _orders_project(
        root,
        (
            "def migrate(ctx):\n"
            "    count = ctx.execute(\n"
            "        'INSERT INTO orders (id, region) VALUES (:id, :region)',\n"
            "        {'id': 7, 'region': 'EU'},\n"
            "    )\n"
            "    assert count == 1, count\n"
            "    rows = ctx.query('SELECT id, region FROM orders WHERE id = :id', {'id': 7})\n"
            "    assert rows == [(7, 'EU')], rows\n"
        ),
        mode="atomic",
        language="python",
        entry="migration.py",
    )
    assert support.migrate(config, manifest) == Exit.OK
    assert oracle_query("SELECT id, region FROM orders") == [(7, "EU")]


# --- restartable, DDL and required objects ----------------------------------------

PACKAGE_SPEC = """\
CREATE OR REPLACE PACKAGE pkg_orders AS
  FUNCTION default_region RETURN VARCHAR2;
END pkg_orders;
"""

PACKAGE_BODY = """\
CREATE OR REPLACE PACKAGE BODY pkg_orders AS
  FUNCTION default_region RETURN VARCHAR2 IS
  BEGIN
    RETURN 'EU';
  END default_region;
END pkg_orders;
"""


def test_restartable_package_with_required_objects(oracle_project, oracle_query):
    root, config, schema = oracle_project
    support.unit(root, "m1", {"spec.sql": PACKAGE_SPEC})
    support.unit(root, "m2", {"body.sql": PACKAGE_BODY})
    manifest = support.manifest(
        root,
        [
            {
                "id": "pkg-spec",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "spec.sql",
                "require_valid": [{"name": "PKG_ORDERS", "type": "PACKAGE"}],
            },
            {
                "id": "pkg-body",
                "path": "m2",
                "language": "sql",
                "mode": "restartable",
                "entry": "body.sql",
                "require_valid": [
                    {"name": "PKG_ORDERS", "type": "PACKAGE"},
                    {"name": "PKG_ORDERS", "type": "PACKAGE BODY"},
                ],
            },
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    assert oracle_query(
        "SELECT object_type, status FROM all_objects WHERE owner = :owner AND "
        "object_name = 'PKG_ORDERS' ORDER BY object_type",
        owner=schema,
    ) == [("PACKAGE", "VALID"), ("PACKAGE BODY", "VALID")]


def test_missing_required_object_fails_after_execution(oracle_project, oracle_query):
    root, config, schema = oracle_project
    support.unit(root, "m1", {"spec.sql": PACKAGE_SPEC})
    manifest = support.manifest(
        root,
        [
            {
                "id": "pkg-spec",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "spec.sql",
                "require_valid": [
                    {"name": "PKG_ORDERS", "type": "PACKAGE"},
                    {"name": "PKG_ORDERS", "type": "PACKAGE BODY"},
                ],
            }
        ],
    )
    report = support.migrate_report(config, manifest)
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert "PACKAGE BODY" in report.message
    assert "is missing" in report.message
    # The package specification was created; the migration stays ACTIVE.
    assert oracle_query("SELECT status FROM m8_history") == [("ACTIVE",)]


def test_wrong_type_declaration_reports_the_actual_type(oracle_project, oracle_query):
    root, config, schema = oracle_project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE orders (id NUMBER(10))"})
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
                "require_valid": [{"name": "ORDERS", "type": "VIEW"}],
            }
        ],
    )
    report = support.migrate_report(config, manifest)
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert "exists with type(s) TABLE" in report.message


def test_invalid_object_fails_and_keeps_failing_on_retry(oracle_project, oracle_query):
    """Completion performs no automatic compilation (spec Section 6)."""
    root, config, schema = oracle_project
    broken_body = (
        "CREATE OR REPLACE PACKAGE BODY pkg_orders AS\n"
        "  FUNCTION default_region RETURN VARCHAR2 IS\n"
        "  BEGIN\n"
        "    RETURN no_such_thing;\n"
        "  END default_region;\n"
        "END pkg_orders;\n"
    )
    support.unit(root, "m1", {"spec.sql": PACKAGE_SPEC})
    support.unit(root, "m2", {"body.sql": broken_body})
    manifest = support.manifest(
        root,
        [
            {
                "id": "pkg-spec",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "spec.sql",
            },
            {
                "id": "pkg-body",
                "path": "m2",
                "language": "sql",
                "mode": "restartable",
                "entry": "body.sql",
                "require_valid": [{"name": "PKG_ORDERS", "type": "PACKAGE BODY"}],
            },
        ],
    )
    first = support.migrate_report(config, manifest)
    assert first.exit_code == Exit.MIGRATION_FAILED
    assert "not 'VALID'" in first.message
    # An identical retry reaches the same verdict even though creation is skipped.
    second = support.migrate_report(config, manifest)
    assert second.exit_code == Exit.MIGRATION_FAILED
    assert "not 'VALID'" in second.message
    assert oracle_query(
        "SELECT status, attempt FROM m8_history WHERE migration_id = 'pkg-body'"
    ) == [("ACTIVE", 2)]


def test_warning_only_object_passes(oracle_project, oracle_query):
    root, config, schema = oracle_project
    # PLW-07203 style warning: an unreferenced parameter with warnings enabled.
    body = "CREATE OR REPLACE PROCEDURE p_warn (p_in IN VARCHAR2) AS\nBEGIN\n  NULL;\nEND p_warn;\n"
    support.unit(root, "m1", {"up.sql": body})
    manifest = support.manifest(
        root,
        [
            {
                "id": "warn-proc",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
                "require_valid": [{"name": "P_WARN", "type": "PROCEDURE"}],
            }
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    assert oracle_query(
        "SELECT status FROM all_objects WHERE owner = :owner AND object_name = 'P_WARN'",
        owner=schema,
    ) == [("VALID",)]


def test_unsupported_required_object_type_is_refused(oracle_project):
    root, config, schema = oracle_project
    support.unit(root, "m1", {"up.sql": "CREATE TABLE orders (id NUMBER(10))"})
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-orders",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
                "require_valid": [{"name": "ORDERS", "type": "TABLE"}],
            }
        ],
    )
    assert support.migrate(config, manifest) == Exit.USAGE


def test_required_set_change_changes_the_fingerprint(oracle_project, oracle_query):
    root, config, schema = oracle_project
    support.unit(root, "m1", {"up.sql": PACKAGE_SPEC})
    entry = {
        "id": "pkg-spec",
        "path": "m1",
        "language": "sql",
        "mode": "restartable",
        "entry": "up.sql",
    }
    manifest = support.manifest(root, [entry])
    assert support.migrate(config, manifest) == Exit.OK
    before = oracle_query("SELECT fingerprint FROM m8_history")[0][0]
    support.manifest(
        root, [{**entry, "require_valid": [{"name": "PKG_ORDERS", "type": "PACKAGE"}]}]
    )
    # The successful migration's fingerprint no longer matches its source.
    assert support.migrate(config, manifest) == Exit.VALIDATION
    report = support.report_for("validate", config, manifest)
    assert report.migrations[0].recorded_fingerprint == before
    assert report.migrations[0].recorded_matches_current is False
