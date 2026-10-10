"""Preflight: what is checked before a connection or any migration code.

These run against the real :func:`migr8.checks.preflight`, with the smallest
adapter that can answer its questions, so a refusal an adapter declares is shown
to reach the caller rather than being assumed to.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import support

from migr8 import adapters
from migr8.adapters.base import StatementPolicy
from migr8.checks import SqlCategory, admit_sql_entry, preflight
from migr8.config import load as load_config
from migr8.engine import Engine
from migr8.errors import Exit, SqlSyntaxError, UnsupportedCapabilityError, UsageError
from migr8.manifest import Language, Mode
from migr8.manifest import load as load_manifest
from migr8.staging import capture_in_place


class _StubAdapter:
    """Answers only what preflight asks, and records what it was asked."""

    name = "stub"
    statement_policy = StatementPolicy(
        atomic=frozenset(),
        query=frozenset(),
        procedural=frozenset(),
        ddl=frozenset(),
        forbidden=frozenset(),
    )

    def __init__(self, *, refuse: tuple[Language, Mode] | None = None) -> None:
        self.refuse = refuse
        self.seen: list[tuple[Language, Mode]] = []

    def admit_combination(self, language: Language, mode: Mode) -> None:
        self.seen.append((language, mode))
        if self.refuse == (language, mode):
            raise UnsupportedCapabilityError(
                f"the stub adapter does not support {language.value} in {mode.value} mode"
            )

    def admit_required_objects(self, required) -> None:
        return

    def admit_statement(self, statement, *, mode, in_batch) -> None:
        return

    def admit_ddl(self, statement) -> None:
        return


def _capture(root):
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER);\n"})
    support.unit(root, "m2", {"up.sql": "INSERT INTO t (id) VALUES (1);\n"})
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
                "id": "insert-t",
                "path": "m2",
                "language": "sql",
                "mode": "atomic",
                "entry": "up.sql",
            },
        ],
    )
    return capture_in_place(load_manifest(manifest))


def test_every_declared_combination_is_offered_to_the_adapter(tmp_path):
    adapter = _StubAdapter()
    preflight(_capture(tmp_path), adapter)
    assert adapter.seen == [
        (Language.SQL, Mode.RESTARTABLE),
        (Language.SQL, Mode.ATOMIC),
    ]


def test_a_combination_the_adapter_refuses_fails_preflight(tmp_path):
    """The refusal is the adapter's to make; preflight must not swallow it."""
    adapter = _StubAdapter(refuse=(Language.SQL, Mode.ATOMIC))
    with pytest.raises(UnsupportedCapabilityError, match="does not support sql in atomic"):
        preflight(_capture(tmp_path), adapter)


# --- SQL entry admission: one decision for preflight and execution -----------------------------

_CONFIGS = {
    "oracle": """
        [database]
        adapter = "oracle"
        dsn = "localhost:1/X"
        user = "U"
        target_schema = "U"

        [lock]
        provider = "dbms_lock"
        package = "SYS.DBMS_LOCK"
        id = 4711
        timeout_seconds = 1
    """,
    "postgres": """
        [database]
        adapter = "postgres"
        dsn = "host=127.0.0.1 port=1 dbname=x user=x"
        user = "x"
        target_schema = "s"

        [lock]
        provider = "advisory"
        id = 4711
        timeout_seconds = 1
    """,
    "sqlite": """
        [database]
        adapter = "sqlite"
        path = "build/never.db"

        [lock]
        provider = "file"
        timeout_seconds = 1
    """,
}


def _adapter(name: str, root: Path):
    """An adapter that has not connected; admission needs no session."""
    if name == "oracle":
        pytest.importorskip("oracledb")
    if name == "postgres":
        pytest.importorskip("psycopg")
    config = support.write(root / f"{name}.toml", _CONFIGS[name])
    return adapters.create(load_config(config))


def _sql_capture(root: Path, mode: str, sql: str | bytes):
    support.unit(root, "unit", {"up.sql": sql})
    manifest = support.manifest(
        root,
        [{"id": "entry", "path": "unit", "language": "sql", "mode": mode, "entry": "up.sql"}],
    )
    return capture_in_place(load_manifest(manifest))


def _preflight_sql(name: str, root: Path, mode: str, sql: str | bytes):
    adapter = _adapter(name, root)
    capture = _sql_capture(root, mode, sql)
    preflight(capture, adapter)
    return adapter, capture


RESERVED = r"reserved metadata object"
NO_PLSQL = r"no PL/SQL support"

#: (adapter, mode, SQL, refusal) for each file that must not reach the database.
RED_CASES = [
    ("oracle", "restartable", "BEGIN DELETE FROM m8_history; COMMIT; END;", RESERVED),
    (
        "oracle",
        "restartable",
        "DECLARE n NUMBER; BEGIN UPDATE m8_meta SET layout_version = 2; COMMIT; END;",
        RESERVED,
    ),
    ("oracle", "atomic", "BEGIN DELETE FROM m8_history; END;", RESERVED),
    ("postgres", "restartable", "BEGIN DELETE FROM m8_history; COMMIT; END;", NO_PLSQL),
    ("postgres", "restartable", "BEGIN; DELETE FROM m8_history; COMMIT;", NO_PLSQL),
    ("postgres", "restartable", "BEGIN;", NO_PLSQL),
    ("postgres", "restartable", "DECLARE n INT; BEGIN NULL; END;", NO_PLSQL),
    ("postgres", "atomic", "BEGIN DELETE FROM m8_history; END;", NO_PLSQL),
    ("postgres", "restartable", "DO $$ BEGIN DELETE FROM m8_history; END $$;", RESERVED),
    ("postgres", "restartable", 'DO $x$ BEGIN DELETE FROM "m8_progress"; END $x$;', RESERVED),
    ("postgres", "restartable", "DO 'BEGIN UPDATE m8_meta SET layout_version = 2; END';", RESERVED),
    ("postgres", "atomic", "DO $$ BEGIN DELETE FROM m8_history; END $$;", RESERVED),
    ("sqlite", "restartable", "BEGIN DELETE FROM m8_history; COMMIT; END;", NO_PLSQL),
    ("sqlite", "restartable", "BEGIN; DELETE FROM m8_history; COMMIT;", NO_PLSQL),
    ("sqlite", "restartable", "BEGIN;", NO_PLSQL),
    ("sqlite", "atomic", "BEGIN DELETE FROM m8_history; END;", NO_PLSQL),
    ("sqlite", "atomic", "DELETE FROM 'm8_history' WHERE seq = 2;", RESERVED),
    ("sqlite", "atomic", "UPDATE 'm8_meta' SET layout_version = 2", RESERVED),
    ("sqlite", "atomic", "INSERT INTO main.'m8_progress' VALUES (1)", RESERVED),
    ("sqlite", "atomic", "UPDATE OR ROLLBACK 'm8_history' SET seq = 9", RESERVED),
]


@pytest.mark.parametrize(("name", "mode", "sql", "refusal"), RED_CASES)
def test_preflight_refuses_a_file_the_facade_would_refuse(tmp_path, name, mode, sql, refusal):
    with pytest.raises((UsageError, UnsupportedCapabilityError), match=refusal):
        _preflight_sql(name, tmp_path, mode, sql)


@pytest.mark.parametrize(("name", "mode", "sql", "refusal"), RED_CASES)
def test_the_engine_refuses_the_same_files_without_executing(
    tmp_path, monkeypatch, name, mode, sql, refusal
):
    adapter = _adapter(name, tmp_path)
    capture = _sql_capture(tmp_path, mode, sql)
    for method in ("execute", "execute_ddl", "has_open_transaction", "guarded"):
        monkeypatch.setattr(
            adapter, method, lambda *a, **k: pytest.fail("the engine reached the database")
        )
    engine = Engine(config=load_config(tmp_path / f"{name}.toml"), adapter=adapter, capture=capture)
    with pytest.raises((UsageError, UnsupportedCapabilityError), match=refusal):
        engine._invoke_sql(capture.units[0])


def test_the_oracle_manual_example_block_passes_preflight(tmp_path):
    manual = (Path(__file__).parents[1] / "docs" / "MANUAL.md").read_text(encoding="utf-8")
    blocks = [b for b in re.findall(r"```sql\n(.*?)```", manual, re.S) if "DECLARE" in b]
    assert blocks, "the MANUAL no longer carries the DECLARE example this test reads"
    _preflight_sql("oracle", tmp_path, "restartable", blocks[0])


@pytest.mark.parametrize(
    "sql",
    [
        "BEGIN INSERT INTO orders (id) VALUES (1); COMMIT; END;",
        "DECLARE n NUMBER; BEGIN NULL; END;",
    ],
)
def test_an_oracle_block_without_a_reserved_name_is_a_procedural_entry(tmp_path, sql):
    adapter, capture = _preflight_sql("oracle", tmp_path, "restartable", sql)
    admitted = admit_sql_entry(capture.units[0], adapter)
    assert admitted.category is SqlCategory.RESTARTABLE_PROCEDURAL


@pytest.mark.parametrize(
    "sql",
    [
        "DO $$ BEGIN PERFORM 1; END $$;",
        "DO LANGUAGE plpgsql $$ BEGIN PERFORM 1; END $$;",
        "CALL refresh_totals();",
        # A comment in the body is prose, as it is outside a DO.
        "DO $$ BEGIN -- never touches m8_history\n PERFORM 1; END $$;",
        # A backslash-escaped quote defeats the tokenizer; the word scan still passes.
        "DO $$ BEGIN RAISE NOTICE E'it\\'s fine'; END $$;",
    ],
)
def test_a_restartable_postgres_procedural_entry_passes_preflight(tmp_path, sql):
    adapter, capture = _preflight_sql("postgres", tmp_path, "restartable", sql)
    admitted = admit_sql_entry(capture.units[0], adapter)
    assert admitted.category is SqlCategory.RESTARTABLE_PROCEDURAL


def test_a_restartable_postgres_ddl_entry_is_still_ddl(tmp_path):
    adapter, capture = _preflight_sql("postgres", tmp_path, "restartable", "CREATE TABLE t (a int)")
    assert admit_sql_entry(capture.units[0], adapter).category is SqlCategory.RESTARTABLE_DDL


def test_an_atomic_entry_is_an_atomic_statement(tmp_path):
    adapter, capture = _preflight_sql("sqlite", tmp_path, "atomic", "INSERT INTO t VALUES (1)")
    assert admit_sql_entry(capture.units[0], adapter).category is SqlCategory.ATOMIC_STATEMENT


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO audit (note) VALUES ('m8_history cleanup')",
        "INSERT INTO audit (note) VALUES ('m8_history')",
        "INSERT INTO audit (a, b) VALUES (1, 'm8_meta')",
        "SELECT replace('m8_history', 'm8', 'x')",
    ],
)
def test_sqlite_admits_a_reserved_name_used_as_a_string_value(tmp_path, sql):
    _preflight_sql("sqlite", tmp_path, "atomic", sql)


def test_postgres_admits_a_reserved_name_used_as_a_string_value(tmp_path):
    sql = "SELECT trim(both ' ' from 'm8_history')"
    _preflight_sql("postgres", tmp_path, "atomic", sql)


# --- a SQL file that is not UTF-8 --------------------------------------------------------------

BAD_BYTES = b"INSERT INTO t (id) VALUES (1) -- caf\xe9\n"


@pytest.mark.parametrize("name", ["oracle", "postgres", "sqlite"])
def test_preflight_names_the_migration_and_file_of_an_undecodable_sql_file(tmp_path, name):
    with pytest.raises(SqlSyntaxError, match=r"'entry'.*up\.sql.*not valid UTF-8") as caught:
        _preflight_sql(name, tmp_path, "atomic", BAD_BYTES)
    assert caught.value.exit_code is Exit.VALIDATION


def test_the_engine_reports_an_undecodable_file_as_a_source_error(tmp_path):
    adapter = _adapter("sqlite", tmp_path)
    capture = _sql_capture(tmp_path, "atomic", BAD_BYTES)
    engine = Engine(config=load_config(tmp_path / "sqlite.toml"), adapter=adapter, capture=capture)
    with pytest.raises(SqlSyntaxError, match="not valid UTF-8"):
        engine._invoke_sql(capture.units[0])


@pytest.mark.sqlite
def test_a_unit_with_a_non_utf8_sql_file_exits_2_from_migrate_and_validate_offline(tmp_path):
    db = tmp_path / "build" / "probe.db"
    config = support.sqlite_config(tmp_path, db_path=db)
    support.unit(tmp_path, "m1", {"up.sql": BAD_BYTES})
    manifest = support.manifest(
        tmp_path,
        [{"id": "bad", "path": "m1", "language": "sql", "mode": "atomic", "entry": "up.sql"}],
    )
    for command in (["migrate"], ["validate", "--offline"]):
        argv = [*command, "--config", str(config), "--manifest", str(manifest)]
        assert support.run_cli(argv) == Exit.VALIDATION
    assert not db.exists(), "preflight failed, so nothing connected and nothing was written"


@pytest.mark.sqlite
def test_migrate_refuses_a_quoted_reserved_name_before_connecting(tmp_path):
    db = tmp_path / "build" / "probe.db"
    config = support.sqlite_config(tmp_path, db_path=db)
    support.unit(tmp_path, "m1", {"up.sql": "DELETE FROM 'm8_history' WHERE seq = 2;\n"})
    manifest = support.manifest(
        tmp_path,
        [{"id": "wipe", "path": "m1", "language": "sql", "mode": "atomic", "entry": "up.sql"}],
    )
    argv = ["migrate", "--config", str(config), "--manifest", str(manifest)]
    assert support.run_cli(argv) == Exit.USAGE
    assert not db.exists()
