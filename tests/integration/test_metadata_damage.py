"""Live damage detection on the real metadata layout (spec Section 8.3).

Each test initializes a real namespace, then mutates one object the layout's
guarantees rest on, then asks the runner what it sees. Inspection must report
the damage and must not repair it: the specification is explicit that nothing is
recreated or altered to make validation pass.

These are the live counterparts to the dictionary-row cases in
``tests/test_metadata_damage.py``.
"""

from __future__ import annotations

import pytest
import support

from migr8.errors import Exit
from migr8.model import ACTIVE_INDEX, HISTORY_TABLE, PROGRESS_TABLE


def _one_migration(root):
    support.unit(root, "m1", {"up.sql": "CREATE TABLE orders (id NUMBER(10) PRIMARY KEY);\n"})
    return support.manifest(
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


def _user_exists(oracle_sys, name: str) -> bool:
    return bool(oracle_sys("SELECT 1 FROM all_users WHERE username = :n", n=name))


def _one_pg_migration(root):
    support.unit(root, "m1", {"up.sql": "CREATE TABLE orders (id integer PRIMARY KEY);\n"})
    return support.manifest(
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


# --- Oracle ---------------------------------------------------------------------------


@pytest.mark.oracle
class TestOracle:
    @pytest.fixture
    def initialized(self, oracle_project):
        root, config, schema = oracle_project
        manifest = _one_migration(root)
        assert support.migrate(config, manifest) == Exit.OK
        return root, config, schema, manifest

    def test_a_replaced_index_expression_is_reported_as_damage(self, initialized, oracle_query):
        """A per-row key admits two ACTIVE rows, so it is not the supported index."""
        root, config, schema, manifest = initialized
        oracle_query(f'DROP INDEX "{schema}"."{ACTIVE_INDEX.upper()}"')
        oracle_query(
            f'CREATE UNIQUE INDEX "{schema}"."{ACTIVE_INDEX.upper()}" '
            f'ON "{schema}"."{HISTORY_TABLE.upper()}" '
            "(CASE WHEN status = 'ACTIVE' THEN seq END)"
        )
        report = support.migrate_report(config, manifest)
        assert report.exit_code == Exit.METADATA_DAMAGED
        assert ACTIVE_INDEX in (report.message or "")
        # Damage is reported, not repaired.
        rows = oracle_query(
            "SELECT column_expression FROM all_ind_expressions "
            "WHERE index_owner = :o AND index_name = :n",
            o=schema,
            n=ACTIVE_INDEX.upper(),
        )
        rendered = rows[0][0]
        assert "SEQ" in (rendered.read() if hasattr(rendered, "read") else str(rendered)).upper()

    def test_a_disabled_unique_key_is_reported_as_damage(self, initialized, oracle_query):
        root, config, schema, manifest = initialized
        name = oracle_query(
            "SELECT constraint_name FROM all_constraints WHERE owner = :o "
            "AND table_name = :t AND constraint_type = 'U'",
            o=schema,
            t=HISTORY_TABLE.upper(),
        )[0][0]
        history = f'"{schema}"."{HISTORY_TABLE.upper()}"'
        oracle_query(f'ALTER TABLE {history} DISABLE CONSTRAINT "{name}"')
        report = support.migrate_report(config, manifest)
        assert report.exit_code == Exit.METADATA_DAMAGED
        assert "DISABLED" in (report.message or "")
        # Still disabled: inspection does not re-enable it.
        status = oracle_query(
            "SELECT status FROM all_constraints WHERE owner = :o AND constraint_name = :n",
            o=schema,
            n=name,
        )[0][0]
        assert status == "DISABLED"

    def test_a_dropped_foreign_key_is_reported_as_damage(self, initialized, oracle_query):
        root, config, schema, manifest = initialized
        name = oracle_query(
            "SELECT constraint_name FROM all_constraints WHERE owner = :o "
            "AND table_name = :t AND constraint_type = 'R'",
            o=schema,
            t=PROGRESS_TABLE.upper(),
        )[0][0]
        oracle_query(f'ALTER TABLE "{schema}"."{PROGRESS_TABLE.upper()}" DROP CONSTRAINT "{name}"')
        report = support.migrate_report(config, manifest)
        assert report.exit_code == Exit.METADATA_DAMAGED
        assert "foreign key" in (report.message or "")

    def test_a_tautological_check_constraint_is_reported_as_damage(self, initialized, oracle_query):
        """A check that admits everything enforces nothing, however it is worded.

        The existing rows stay valid, so only the definition says what the next
        write will be held to.
        """
        root, config, schema, manifest = initialized
        history = f'"{schema}"."{HISTORY_TABLE.upper()}"'
        name = oracle_query(
            "SELECT constraint_name FROM all_constraints WHERE owner = :o "
            "AND table_name = :t AND constraint_type = 'C' "
            "AND constraint_name = 'M8_HISTORY_SEQ_CK'",
            o=schema,
            t=HISTORY_TABLE.upper(),
        )[0][0]
        oracle_query(f'ALTER TABLE {history} DROP CONSTRAINT "{name}"')
        oracle_query(f'ALTER TABLE {history} ADD CONSTRAINT "{name}" CHECK (seq > 0 OR 1=1)')

        report = support.migrate_report(config, manifest)
        assert report.exit_code == Exit.METADATA_DAMAGED
        assert "unsupported check constraint" in (report.message or "")
        # Damage is reported, not repaired.
        condition = oracle_query(
            "SELECT search_condition_vc FROM all_constraints "
            "WHERE owner = :o AND constraint_name = :n",
            o=schema,
            n=name,
        )[0][0]
        assert "1=1" in str(condition)

    def test_a_foreign_key_into_another_schema_is_reported_as_damage(
        self, initialized, oracle_query, oracle_sys
    ):
        """A same-named history table in another schema is not this namespace's."""
        root, config, schema, manifest = initialized
        other = f"{schema}_OTHER"
        if _user_exists(oracle_sys, other):
            oracle_sys(f"DROP USER {other} CASCADE")
        oracle_sys(f'CREATE USER {other} IDENTIFIED BY "migr8_other_test"')
        try:
            oracle_sys(f"ALTER USER {other} QUOTA UNLIMITED ON USERS")
            oracle_sys(
                f'CREATE TABLE {other}."{HISTORY_TABLE.upper()}" '
                "(migration_id VARCHAR2(200 CHAR) NOT NULL PRIMARY KEY)"
            )
            oracle_sys(f'GRANT REFERENCES ON {other}."{HISTORY_TABLE.upper()}" TO {schema}')
            progress = f'"{schema}"."{PROGRESS_TABLE.upper()}"'
            name = oracle_query(
                "SELECT constraint_name FROM all_constraints WHERE owner = :o "
                "AND table_name = :t AND constraint_type = 'R'",
                o=schema,
                t=PROGRESS_TABLE.upper(),
            )[0][0]
            oracle_query(f'ALTER TABLE {progress} DROP CONSTRAINT "{name}"')
            oracle_query(
                f'ALTER TABLE {progress} ADD CONSTRAINT "{name}" FOREIGN KEY (migration_id) '
                f'REFERENCES {other}."{HISTORY_TABLE.upper()}" (migration_id)'
            )

            report = support.migrate_report(config, manifest)
            assert report.exit_code == Exit.METADATA_DAMAGED
            assert f"references {other}." in (report.message or "")
            # Damage is reported, not repaired.
            owner = oracle_query(
                "SELECT r_owner FROM all_constraints WHERE owner = :o AND constraint_name = :n",
                o=schema,
                n=name,
            )[0][0]
            assert owner == other
        finally:
            oracle_sys(f"DROP USER {other} CASCADE")

    def test_the_layout_the_runner_creates_passes_its_own_checks(self, initialized):
        """The checks above must not reject a freshly initialized namespace."""
        root, config, schema, manifest = initialized
        report = support.migrate_report(config, manifest)
        assert report.exit_code == Exit.OK
        assert report.executed == []


# --- PostgreSQL -----------------------------------------------------------------------


@pytest.mark.postgres
class TestPostgres:
    @pytest.fixture
    def initialized(self, pg_project):
        root, config, schema = pg_project
        manifest = _one_pg_migration(root)
        assert support.migrate(config, manifest) == Exit.OK
        return root, config, schema, manifest

    def test_an_index_keyed_on_another_column_is_reported_as_damage(self, initialized, pg_query):
        """The predicate alone is not the guarantee; the key has to be status."""
        root, config, schema, manifest = initialized
        pg_query(f'DROP INDEX "{schema}"."{ACTIVE_INDEX}"')
        pg_query(
            f'CREATE UNIQUE INDEX "{ACTIVE_INDEX}" ON "{schema}"."{HISTORY_TABLE}" (seq) '
            "WHERE status = 'ACTIVE'"
        )
        report = support.migrate_report(config, manifest)
        assert report.exit_code == Exit.METADATA_DAMAGED
        assert ACTIVE_INDEX in (report.message or "")

    def test_an_index_without_its_predicate_is_reported_as_damage(self, initialized, pg_query):
        root, config, schema, manifest = initialized
        pg_query(f'DROP INDEX "{schema}"."{ACTIVE_INDEX}"')
        pg_query(f'CREATE UNIQUE INDEX "{ACTIVE_INDEX}" ON "{schema}"."{HISTORY_TABLE}" (status)')
        report = support.migrate_report(config, manifest)
        assert report.exit_code == Exit.METADATA_DAMAGED
        assert "not restricted" in (report.message or "")

    def test_a_not_valid_foreign_key_is_reported_as_damage(self, initialized, pg_query):
        root, config, schema, manifest = initialized
        name = pg_query(
            "SELECT con.conname FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s AND con.contype = 'f'",
            (schema, PROGRESS_TABLE),
        )[0][0]
        pg_query(f'ALTER TABLE "{schema}"."{PROGRESS_TABLE}" DROP CONSTRAINT "{name}"')
        pg_query(
            f'ALTER TABLE "{schema}"."{PROGRESS_TABLE}" ADD CONSTRAINT "{name}" '
            f'FOREIGN KEY (migration_id) REFERENCES "{schema}"."{HISTORY_TABLE}" (migration_id) '
            "NOT VALID"
        )
        report = support.migrate_report(config, manifest)
        assert report.exit_code == Exit.METADATA_DAMAGED
        assert "NOT VALID" in (report.message or "")

    def test_a_tautological_check_constraint_is_reported_as_damage(self, initialized, pg_query):
        """A check that admits everything enforces nothing, however it is worded."""
        root, config, schema, manifest = initialized
        history = f'"{schema}"."{HISTORY_TABLE}"'
        pg_query(f"ALTER TABLE {history} DROP CONSTRAINT m8_history_seq_ck")
        pg_query(f"ALTER TABLE {history} ADD CONSTRAINT m8_history_seq_ck CHECK (seq > 0 OR 1 = 1)")

        report = support.migrate_report(config, manifest)
        assert report.exit_code == Exit.METADATA_DAMAGED
        assert "unsupported check constraint" in (report.message or "")
        # Damage is reported, not repaired.
        definition = pg_query(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s",
            ("m8_history_seq_ck",),
        )[0][0]
        assert "1 = 1" in definition

    def test_a_foreign_key_into_another_schema_is_reported_as_damage(self, initialized, pg_query):
        """A same-named history table in another schema is not this namespace's."""
        root, config, schema, manifest = initialized
        other = f"{schema}_other"
        progress = f'"{schema}"."{PROGRESS_TABLE}"'
        name = pg_query(
            "SELECT con.conname FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s AND con.contype = 'f'",
            (schema, PROGRESS_TABLE),
        )[0][0]
        pg_query(f'DROP SCHEMA IF EXISTS "{other}" CASCADE')
        pg_query(f'CREATE SCHEMA "{other}"')
        try:
            pg_query(
                f'CREATE TABLE "{other}"."{HISTORY_TABLE}" '
                "(migration_id character varying(200) PRIMARY KEY)"
            )
            pg_query(f'ALTER TABLE {progress} DROP CONSTRAINT "{name}"')
            pg_query(
                f'ALTER TABLE {progress} ADD CONSTRAINT "{name}" FOREIGN KEY (migration_id) '
                f'REFERENCES "{other}"."{HISTORY_TABLE}" (migration_id)'
            )

            report = support.migrate_report(config, manifest)
            assert report.exit_code == Exit.METADATA_DAMAGED
            assert f"references {other}." in (report.message or "")
        finally:
            pg_query(f'ALTER TABLE {progress} DROP CONSTRAINT IF EXISTS "{name}"')
            pg_query(f'DROP SCHEMA IF EXISTS "{other}" CASCADE')

    def test_the_layout_the_runner_creates_passes_its_own_checks(self, initialized):
        root, config, schema, manifest = initialized
        report = support.migrate_report(config, manifest)
        assert report.exit_code == Exit.OK
        assert report.executed == []
