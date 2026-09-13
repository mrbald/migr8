"""Helpers for building migration trees and configurations in tests."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

MANIFEST_HEADER = "manifest_version = 1\n"


def write(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def unit(root: Path, name: str, files: dict[str, str | bytes]) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    for relpath, content in files.items():
        write(directory / relpath, content)
    return directory


def manifest(
    root: Path,
    entries: list[dict[str, object]],
    *,
    name: str = "manifest.toml",
    version: int | None = 1,
    extra: str = "",
) -> Path:
    lines = []
    if version is not None:
        lines.append(f"manifest_version = {version}")
    lines.append(extra)
    for entry in entries:
        lines.append("[[migration]]")
        for key, value in entry.items():
            if key == "require_valid":
                rendered = ", ".join(
                    '{{ name = "{}", type = "{}" }}'.format(item["name"], item["type"])
                    for item in value  # type: ignore[union-attr]
                )
                lines.append(f"require_valid = [{rendered}]")
            elif isinstance(value, str):
                lines.append(f'{key} = "{value}"')
            else:
                lines.append(f"{key} = {value}")
        lines.append("")
    return write(root / name, "\n".join(lines) + "\n")


def sqlite_config(
    root: Path,
    *,
    db_path: Path | None = None,
    timeout: int = 5,
    journal_mode: str = "delete",
    name: str = "migr8.toml",
) -> Path:
    target = db_path if db_path is not None else root / "build" / "probe.db"
    return write(
        root / name,
        f"""
        [database]
        adapter = "sqlite"
        path = "{target}"

        [lock]
        provider = "file"
        timeout_seconds = {timeout}

        [sqlite]
        journal_mode = "{journal_mode}"
        busy_timeout_ms = 2000
        """,
    )


def simple_sql_project(
    root: Path, *, mode: str = "atomic", sql: str = "INSERT INTO t (id) VALUES (1);"
) -> tuple[Path, Path]:
    """A one-migration project whose table is created by a preceding migration."""
    unit(root, "m1", {"up.sql": "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY);\n"})
    unit(root, "m2", {"up.sql": sql})
    manifest_path = manifest(
        root,
        [
            {
                "id": "create-t",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
            {"id": "insert-t", "path": "m2", "language": "sql", "mode": mode, "entry": "up.sql"},
        ],
    )
    config_path = sqlite_config(root)
    return config_path, manifest_path


#: Set to an Oracle Client library directory to run the Oracle suites in Thick
#: mode.  The switch is the adapter's own: the generated configs carry
#: `allow_thick_mode` and `client_lib_dir`, so every runner the suite starts, in
#: this process and in a subprocess, is in the mode named here.
ORACLE_CLIENT_LIB = os.environ.get("MIGR8_ORACLE_CLIENT_LIB")


def oracle_mode_options() -> str:
    """The `[oracle]` lines that select the driver mode, for a generated config."""
    if not ORACLE_CLIENT_LIB:
        return ""
    return f'        allow_thick_mode = true\n        client_lib_dir = "{ORACLE_CLIENT_LIB}"\n'


def enable_oracle_thick_mode(oracledb) -> None:
    """Load the Oracle Client libraries for this process, if the suite asks for it."""
    if ORACLE_CLIENT_LIB and oracledb.is_thin_mode():
        oracledb.init_oracle_client(lib_dir=ORACLE_CLIENT_LIB)


def run_cli(argv: list[str], *, cwd: Path | None = None) -> int:
    """Invoke the CLI in-process, returning its exit code."""
    from migr8.cli import main

    previous = Path.cwd()
    if cwd is not None:
        os.chdir(cwd)
    try:
        return main(argv)
    finally:
        os.chdir(previous)


# --- driving the engine from tests -------------------------------------------------


def migrate(
    config_path: Path, manifest_path: Path, *, recover: str | None = None, adapter_hook=None
) -> int:
    """Run one ``migrate`` through the real engine, returning the exit code."""
    from migr8 import adapters
    from migr8.config import load as load_config
    from migr8.engine import Engine
    from migr8.manifest import load as load_manifest
    from migr8.staging import cleanup, stage

    config = load_config(config_path)
    manifest = load_manifest(manifest_path)
    adapter = adapters.create(config)
    if adapter_hook is not None:
        adapter_hook(adapter)
    capture = stage(manifest)
    try:
        engine = Engine(config=config, adapter=adapter, capture=capture, recover_id=recover)
        report = engine.run()
    finally:
        cleanup(capture.staging_root)
    return int(report.exit_code)


def migrate_report(
    config_path: Path, manifest_path: Path, *, recover: str | None = None, adapter_hook=None
):
    """Like :func:`migrate` but returns the whole run report."""
    from migr8 import adapters
    from migr8.config import load as load_config
    from migr8.engine import Engine
    from migr8.manifest import load as load_manifest
    from migr8.staging import cleanup, stage

    config = load_config(config_path)
    manifest = load_manifest(manifest_path)
    adapter = adapters.create(config)
    if adapter_hook is not None:
        adapter_hook(adapter)
    capture = stage(manifest)
    try:
        engine = Engine(config=config, adapter=adapter, capture=capture, recover_id=recover)
        return engine.run()
    finally:
        cleanup(capture.staging_root)


def report_for(command: str, config_path: Path, manifest_path: Path):
    """Run ``validate`` or ``status`` and return its report object."""
    from migr8 import adapters
    from migr8.config import load as load_config
    from migr8.manifest import load as load_manifest
    from migr8.readonly import run_status, run_validate
    from migr8.staging import capture_in_place

    config = load_config(config_path)
    capture = capture_in_place(load_manifest(manifest_path))
    adapter = adapters.create(config)
    runner = run_status if command == "status" else run_validate
    return runner(adapter, capture)


def report_for_offline(config_path: Path, manifest_path: Path, *, baseline: Path | None = None):
    """Run ``validate --offline`` and return its report object."""
    from migr8 import adapters
    from migr8.config import load as load_config
    from migr8.manifest import load as load_manifest
    from migr8.readonly import run_offline
    from migr8.staging import capture_in_place

    config = load_config(config_path)
    capture = capture_in_place(load_manifest(manifest_path))
    return run_offline(adapters.create(config), capture, baseline=baseline)


def db_query(db_path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def db_exec(db_path: Path, sql: str, params: tuple = ()) -> None:
    import sqlite3

    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(sql, params)
    finally:
        conn.close()


def db_redefine(db_path: Path, name: str, sql: str) -> None:
    """Replace one object's stored definition, as a hand-edited schema would.

    `PRAGMA writable_schema` is SQLite's own way to rewrite a stored definition
    without recreating the object, and it is what expresses "the layout was
    changed underneath a namespace that is already initialized".  The schema
    version is bumped so the next connection reloads rather than serving a
    cached schema.
    """
    import sqlite3

    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        version = conn.execute("PRAGMA schema_version").fetchone()[0]
        conn.execute("PRAGMA writable_schema = ON")
        conn.execute("UPDATE sqlite_master SET sql = ? WHERE name = ?", (sql, name))
        conn.execute(f"PRAGMA schema_version = {version + 1}")
        conn.execute("PRAGMA writable_schema = OFF")
    finally:
        conn.close()


def db_rebuild(db_path: Path, name: str, sql: str, after: tuple[str, ...] = ()) -> None:
    """Recreate one table from a different definition, keeping its rows.

    Dropping a table drops the indexes on it, so *after* carries whatever has to
    be recreated alongside it.
    """
    import sqlite3

    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        rows = conn.execute(f"SELECT * FROM {name}").fetchall()
        width = len(conn.execute(f"PRAGMA table_info({name})").fetchall())
        conn.execute("BEGIN")
        conn.execute(f"DROP TABLE {name}")
        conn.execute(sql)
        for statement in after:
            conn.execute(statement)
        if rows:
            conn.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' * width)})", rows)
        conn.execute("COMMIT")
    finally:
        conn.close()


def history(db_path: Path) -> list[tuple]:
    return db_query(
        db_path,
        "SELECT seq, migration_id, status, mode, language, attempt, fingerprint, "
        "first_fingerprint FROM m8_history ORDER BY seq",
    )


def progress(db_path: Path) -> list[tuple]:
    return db_query(
        db_path,
        "SELECT migration_id, prog_key, prog_value FROM m8_progress "
        "ORDER BY migration_id, prog_key",
    )
