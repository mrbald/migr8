"""The SQLite metadata layout, and the durability settings the adapter requires.

Every case here damages a namespace that migrated successfully, then asserts the
next command refuses it.  The engine never writes these shapes itself; what is
under test is whether the inspection accepts one it finds, because a layout that
enforces less than the supported one is accepted silently otherwise.  A unique
index restricted to ACTIVE rows but keyed on the migration id is the worked
example: it exists, it is unique, it is partial, and it permits as many ACTIVE
migrations as there are migrations.

The durability cases assert the settings are established and read back rather
than inherited from whatever the database or the build happened to default to.
"""

from __future__ import annotations

import pytest
import support

from migr8.adapters.sqlite import (
    _DDL,
    SUPPORTED_JOURNAL_MODES,
    SYNCHRONOUS_LEVELS,
)
from migr8.errors import Exit, UnsupportedCapabilityError
from migr8.model import ACTIVE_INDEX, HISTORY_TABLE, PROGRESS_TABLE

pytestmark = pytest.mark.sqlite


def _one_migration(root):
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    return support.manifest(
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


@pytest.fixture(params=SUPPORTED_JOURNAL_MODES)
def migrated(request, tmp_path):
    """A namespace with one successful migration, ready to be damaged."""
    db = tmp_path / "build" / "probe.db"
    config = support.sqlite_config(tmp_path, db_path=db, journal_mode=request.param)
    manifest = _one_migration(tmp_path)
    assert support.migrate(config, manifest) == Exit.OK
    return config, manifest, db


def stored(db, name: str) -> str:
    """One object's definition as SQLite keeps it, which is what damage starts from."""
    return support.db_query(db, "SELECT sql FROM sqlite_master WHERE name = ?", (name,))[0][0]


def cli(command: str, config, manifest) -> int:
    """Run one command the way an operator does, for the failures that have no report."""
    return support.run_cli(
        [command, "--config", str(config), "--manifest", str(manifest)],
    )


def refused(migrated) -> str:
    """The reported problem from a ``validate`` that has to refuse the namespace.

    ``migrate`` is asserted too: an inspection that only the read-only path
    applies would let the next run write to the damaged layout.
    """
    config, manifest, db = migrated
    report = support.report_for("validate", config, manifest)
    assert report.exit_code == int(Exit.METADATA_DAMAGED), report.problem
    assert support.migrate(config, manifest) == Exit.METADATA_DAMAGED
    return report.problem or ""


# --- the one-ACTIVE index ----------------------------------------------------------

INDEX_MUTATIONS = [
    pytest.param(
        f"CREATE UNIQUE INDEX {ACTIVE_INDEX} ON {HISTORY_TABLE} (migration_id) "
        "WHERE status = 'ACTIVE'",
        "keyed on",
        id="keyed-on-another-column",
    ),
    pytest.param(
        f"CREATE INDEX {ACTIVE_INDEX} ON {HISTORY_TABLE} (status) WHERE status = 'ACTIVE'",
        "not UNIQUE",
        id="not-unique",
    ),
    pytest.param(
        f"CREATE UNIQUE INDEX {ACTIVE_INDEX} ON {HISTORY_TABLE} (status)",
        "no WHERE clause",
        id="not-partial",
    ),
    pytest.param(
        f"CREATE UNIQUE INDEX {ACTIVE_INDEX} ON {HISTORY_TABLE} (status) WHERE status = 'active'",
        "restricted by",
        id="predicate-with-another-literal",
    ),
    pytest.param(
        f"CREATE UNIQUE INDEX {ACTIVE_INDEX} ON {HISTORY_TABLE} (status) "
        "WHERE status IN ('ACTIVE','SUCCESS')",
        "restricted by",
        id="widened-predicate",
    ),
    pytest.param(
        f"CREATE UNIQUE INDEX {ACTIVE_INDEX} ON {HISTORY_TABLE} (status) "
        "WHERE status = 'ACTIVE' OR 1 = 0",
        "restricted by",
        id="predicate-with-an-added-term",
    ),
]


@pytest.mark.parametrize("definition,expected", INDEX_MUTATIONS)
def test_a_one_active_index_that_holds_less_is_damage(migrated, definition, expected):
    _config, _manifest, db = migrated
    support.db_exec(db, f"DROP INDEX {ACTIVE_INDEX}")
    support.db_exec(db, definition)
    assert expected in refused(migrated)


def test_an_index_of_that_name_on_another_table_is_damage(migrated):
    _config, _manifest, db = migrated
    support.db_exec(db, f"DROP INDEX {ACTIVE_INDEX}")
    support.db_exec(db, "CREATE TABLE decoy (status TEXT)")
    support.db_exec(db, f"CREATE UNIQUE INDEX {ACTIVE_INDEX} ON decoy (status)")
    assert "not on m8_history" in refused(migrated)


def test_an_index_added_to_a_metadata_table_is_damage(migrated):
    """The layout is closed: an added unique index decides what may be written next."""
    _config, _manifest, db = migrated
    support.db_exec(db, f"CREATE UNIQUE INDEX extra_fp ON {HISTORY_TABLE} (fingerprint)")
    assert "indexes the supported layout does not define" in refused(migrated)


# --- check constraints -------------------------------------------------------------

CHECK_MUTATIONS = [
    pytest.param(
        "CHECK (status <> 'ACTIVE' OR mode = 'restartable'),\n",
        "",
        "missing the check constraint",
        id="removed",
    ),
    pytest.param(
        "CHECK (status IN ('ACTIVE','SUCCESS'))",
        "CHECK (status IN ('ACTIVE','SUCCESS','WHATEVER'))",
        "unsupported check constraint",
        id="widened",
    ),
    pytest.param(
        "CHECK (seq > 0)",
        "CHECK (seq > 0 OR 1 = 1)",
        "unsupported check constraint",
        id="satisfied-by-an-added-term",
    ),
    pytest.param(
        "CHECK (status <> 'SUCCESS' OR finished_at IS NOT NULL)",
        "CHECK (status <> 'SUCCESS' OR finished_at IS NOT NULL), CHECK (seq < 1000000)",
        "unsupported check constraint",
        id="added",
    ),
]


@pytest.mark.parametrize("before,after,expected", CHECK_MUTATIONS)
def test_an_altered_check_constraint_is_damage(migrated, before, after, expected):
    """The supported set is closed, so a missing, altered or added check is damage."""
    _config, _manifest, db = migrated
    definition = stored(db, HISTORY_TABLE)
    assert before in definition
    support.db_redefine(db, HISTORY_TABLE, definition.replace(before, after))
    assert expected in refused(migrated)


# --- keys --------------------------------------------------------------------------


def test_history_without_the_unique_key_on_seq_is_damage(migrated):
    _config, _manifest, db = migrated
    support.db_rebuild(
        db,
        HISTORY_TABLE,
        _DDL[HISTORY_TABLE].replace("NOT NULL UNIQUE CHECK (seq > 0)", "NOT NULL CHECK (seq > 0)"),
        after=(_DDL[ACTIVE_INDEX],),
    )
    assert "missing a unique key on (seq)" in refused(migrated)


def test_history_keyed_on_another_column_is_damage(migrated):
    _config, _manifest, db = migrated
    support.db_rebuild(
        db,
        HISTORY_TABLE,
        _DDL[HISTORY_TABLE]
        .replace(
            "migration_id      TEXT    NOT NULL PRIMARY KEY", "migration_id      TEXT    NOT NULL"
        )
        .replace(
            "seq               INTEGER NOT NULL UNIQUE",
            "seq               INTEGER NOT NULL PRIMARY KEY UNIQUE",
        ),
        after=(_DDL[ACTIVE_INDEX],),
    )
    assert "primary key is (seq), not (migration_id)" in refused(migrated)


def test_progress_referencing_another_table_is_damage(migrated):
    """A foreign key that exists but points elsewhere carries no relationship."""
    _config, _manifest, db = migrated
    support.db_exec(db, "CREATE TABLE decoy (migration_id TEXT PRIMARY KEY)")
    support.db_rebuild(
        db,
        PROGRESS_TABLE,
        _DDL[PROGRESS_TABLE].replace(
            f"REFERENCES {HISTORY_TABLE} (migration_id)", "REFERENCES decoy (migration_id)"
        ),
    )
    assert "missing a foreign key on (migration_id) referencing m8_history" in refused(migrated)


def test_progress_without_its_foreign_key_is_damage(migrated):
    _config, _manifest, db = migrated
    support.db_rebuild(
        db,
        PROGRESS_TABLE,
        _DDL[PROGRESS_TABLE].replace(f"REFERENCES {HISTORY_TABLE} (migration_id)", ""),
    )
    assert "missing a foreign key" in refused(migrated)


def test_progress_with_a_shorter_primary_key_is_damage(migrated):
    _config, _manifest, db = migrated
    support.db_rebuild(
        db,
        PROGRESS_TABLE,
        _DDL[PROGRESS_TABLE].replace(
            "PRIMARY KEY (migration_id, prog_key)", "PRIMARY KEY (migration_id)"
        ),
    )
    assert "primary key is (migration_id), not (migration_id,prog_key)" in refused(migrated)


# --- everything the structural checks do not name ----------------------------------


def test_a_table_stored_differently_is_damage(migrated):
    """The closing comparison: the same columns, keys and checks, another table.

    WITHOUT ROWID passes every structural check above and is still not the
    layout this engine writes to.
    """
    _config, _manifest, db = migrated
    support.db_rebuild(
        db,
        HISTORY_TABLE,
        _DDL[HISTORY_TABLE].rstrip().rstrip(")") + ") WITHOUT ROWID",
        after=(_DDL[ACTIVE_INDEX],),
    )
    assert "is not the supported layout" in refused(migrated)


def test_a_schema_sqlite_cannot_parse_is_damage(migrated):
    """A hand-edited schema fails every read of the file, and that is damage.

    Rewriting the definition without dropping the index it created leaves an
    orphan, which SQLite reports on the first access to the file rather than on
    the object at fault.  The namespace is refused with the metadata exit code,
    not with an unexpected-failure message.
    """
    config, manifest, db = migrated
    support.db_redefine(
        db,
        HISTORY_TABLE,
        stored(db, HISTORY_TABLE).replace(
            "NOT NULL UNIQUE CHECK (seq > 0)", "NOT NULL CHECK (seq > 0)"
        ),
    )
    assert cli("validate", config, manifest) == Exit.METADATA_DAMAGED
    assert cli("migrate", config, manifest) == Exit.METADATA_DAMAGED


# --- durability --------------------------------------------------------------------


def settings(config_path) -> dict:
    """What the adapter's own connection reports, not a second connection's defaults."""
    from migr8 import adapters
    from migr8.config import load as load_config

    adapter = adapters.create(load_config(config_path))
    adapter.connect()
    try:
        names = ("synchronous", "foreign_keys", "ignore_check_constraints", "busy_timeout")
        return {name: adapter._db.execute(f"PRAGMA {name}").fetchone()[0] for name in names}
    finally:
        adapter.close()


@pytest.mark.parametrize("level", sorted(SYNCHRONOUS_LEVELS))
def test_the_configured_durability_is_established_and_read_back(tmp_path, level):
    config = support.sqlite_config(tmp_path, journal_mode="delete")
    config.write_text(config.read_text() + f'synchronous = "{level}"\n')
    assert settings(config) == {
        "synchronous": SYNCHRONOUS_LEVELS[level],
        "foreign_keys": 1,
        "ignore_check_constraints": 0,
        "busy_timeout": 2000,
    }


def test_the_default_durability_is_full(tmp_path):
    """A database built with another default does not get to decide this."""
    config = support.sqlite_config(tmp_path)
    assert settings(config)["synchronous"] == SYNCHRONOUS_LEVELS["full"]


@pytest.mark.parametrize("level", ["off", "normal", "extra_safe"])
def test_a_durability_below_the_supported_set_is_refused(tmp_path, level):
    config = support.sqlite_config(tmp_path)
    config.write_text(config.read_text() + f'synchronous = "{level}"\n')
    assert cli("migrate", config, _one_migration(tmp_path)) == Exit.USAGE


def test_a_setting_that_does_not_read_back_fails_the_run(tmp_path, monkeypatch):
    """Injected: the pragma is made to disagree with what was requested."""
    from migr8 import adapters
    from migr8.adapters import sqlite as sqlite_adapter
    from migr8.config import load as load_config

    monkeypatch.setitem(sqlite_adapter.SYNCHRONOUS_LEVELS, "full", 99)
    config = support.sqlite_config(tmp_path)
    adapter = adapters.create(load_config(config))
    with pytest.raises(UnsupportedCapabilityError, match="reads back as 2"):
        adapter.connect()
    adapter.close()


@pytest.mark.parametrize("mode", SUPPORTED_JOURNAL_MODES)
def test_the_configured_journal_mode_is_the_one_in_force(tmp_path, mode):
    db = tmp_path / "build" / "probe.db"
    config = support.sqlite_config(tmp_path, db_path=db, journal_mode=mode)
    assert support.migrate(config, _one_migration(tmp_path)) == Exit.OK
    assert support.db_query(db, "PRAGMA journal_mode") == [(mode,)]


def test_a_journal_mode_that_cannot_be_established_fails_the_run(tmp_path):
    """Another open connection blocks the change, and the run says so rather than
    proceeding in the mode the file is already in."""
    import sqlite3

    db = tmp_path / "build" / "probe.db"
    config = support.sqlite_config(tmp_path, db_path=db, journal_mode="wal")
    manifest = _one_migration(tmp_path)
    assert support.migrate(config, manifest) == Exit.OK

    reader = sqlite3.connect(db, isolation_level=None, timeout=0.2)
    reader.execute("BEGIN")
    reader.execute(f"SELECT count(*) FROM {HISTORY_TABLE}").fetchone()
    try:
        delete_mode = support.sqlite_config(
            tmp_path, db_path=db, journal_mode="delete", name="delete.toml"
        )
        assert cli("migrate", delete_mode, manifest) == Exit.USAGE
        assert support.db_query(db, "PRAGMA journal_mode") == [("wal",)]
    finally:
        reader.close()


# --- the retired adapter name ------------------------------------------------------


def test_the_old_adapter_name_is_refused_by_name(tmp_path):
    """`sqlite-probe` is gone, and the message says what to change."""
    config = support.sqlite_config(tmp_path)
    config.write_text(config.read_text().replace('"sqlite"', '"sqlite-probe"'), encoding="utf-8")
    manifest = _one_migration(tmp_path)
    assert cli("migrate", config, manifest) == Exit.USAGE


def test_a_namespace_recorded_under_the_old_name_is_not_adopted(migrated):
    """The recorded adapter is part of the binding, and there is no repair for it."""
    config, manifest, db = migrated
    support.db_exec(db, "UPDATE m8_meta SET adapter = 'sqlite-probe'")
    report = support.report_for("validate", config, manifest)
    assert report.exit_code == Exit.USAGE
    assert "sqlite-probe" in (report.problem or "")
    assert support.migrate(config, manifest) == Exit.USAGE
