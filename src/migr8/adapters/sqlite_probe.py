"""The ``sqlite-probe`` adapter (spec Section 13.3).

This is test and development support.  It is not an Oracle emulator and it
certifies nothing about production behaviour.  Two deliberate differences from
the production adapters are documented here and in :meth:`capabilities`:

* the namespace lock is a process-held POSIX advisory file lock associated with
  the canonical database path, not a database-backed lock;
* the transaction-contract guard is SQLite's native transaction state plus
  facade-level statement admission, not a server transaction identity.

Only cooperating local processes using the same canonical database path are
supported.  Network filesystems, hard-link aliases, shared in-memory databases
and Windows locking are outside this probe profile.
"""

from __future__ import annotations

import errno
import os
import sqlite3
import sys
import time
from pathlib import Path

from ..config import Config
from ..errors import (
    ConfigError,
    LockNotAcquiredError,
    MetadataDamagedError,
    UnsupportedCapabilityError,
    UsageError,
)
from ..manifest import Language, Mode, RequiredObject
from ..model import (
    ACTIVE_INDEX,
    HISTORY_TABLE,
    META_SINGLETON_KEY,
    META_TABLE,
    PROGRESS_TABLE,
    LAYOUT_VERSION,
    MetadataReport,
    Snapshot,
)
from ..sqltext import Statement, StatementKind
from ..version import TOOL_VERSION
from . import metadata as md
from .base import Adapter, Boundary, Capabilities, OutcomeClass, StatementPolicy

ADAPTER_NAME = "sqlite-probe"
LOCK_SUFFIX = ".fwlock"

#: Database time, produced by SQLite rather than by the Python process.
_NOW = "strftime('%Y-%m-%dT%H:%M:%f','now')"

#: SQLite has transactional DDL, so DDL is explicitly admitted in atomic work.
#: PRAGMA, VACUUM, ATTACH and DETACH are not: they can commit or change
#: connection-wide state.
_POLICY = StatementPolicy(
    atomic=frozenset({
        "SELECT", "WITH", "VALUES", "INSERT", "REPLACE", "UPDATE", "DELETE",
        "CREATE", "ALTER", "DROP",
    }),
    query=frozenset({"SELECT", "WITH", "VALUES"}),
    procedural=frozenset(),
    ddl=frozenset({"CREATE", "ALTER", "DROP"}),
    forbidden=frozenset({
        "BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE",
        "PRAGMA", "VACUUM", "ATTACH", "DETACH", "ANALYZE", "REINDEX",
    }),
    allows_plsql=False,
)

_DDL = {
    HISTORY_TABLE: f"""
        CREATE TABLE {HISTORY_TABLE} (
            seq               INTEGER NOT NULL UNIQUE CHECK (seq > 0),
            migration_id      TEXT    NOT NULL PRIMARY KEY,
            fingerprint       TEXT    NOT NULL,
            first_fingerprint TEXT    NOT NULL,
            language          TEXT    NOT NULL CHECK (language IN ('sql','python')),
            mode              TEXT    NOT NULL CHECK (mode IN ('atomic','restartable')),
            status            TEXT    NOT NULL CHECK (status IN ('ACTIVE','SUCCESS')),
            attempt           INTEGER          CHECK (attempt IS NULL OR attempt > 0),
            started_at        TEXT    NOT NULL,
            last_attempt_at   TEXT,
            finished_at       TEXT,
            runner_host       TEXT,
            runner_user       TEXT,
            runner_pid        INTEGER,
            db_session        TEXT,
            tool_version      TEXT    NOT NULL,
            CHECK (status <> 'ACTIVE' OR mode = 'restartable'),
            CHECK (status <> 'ACTIVE' OR (attempt IS NOT NULL AND finished_at IS NULL)),
            CHECK (status <> 'SUCCESS' OR finished_at IS NOT NULL)
        )
    """,
    ACTIVE_INDEX: f"""
        CREATE UNIQUE INDEX {ACTIVE_INDEX}
            ON {HISTORY_TABLE} (status) WHERE status = 'ACTIVE'
    """,
    PROGRESS_TABLE: f"""
        CREATE TABLE {PROGRESS_TABLE} (
            migration_id TEXT NOT NULL REFERENCES {HISTORY_TABLE} (migration_id),
            prog_key     TEXT NOT NULL CHECK (length(prog_key) BETWEEN 1 AND 128),
            prog_value   TEXT NOT NULL CHECK (length(prog_value) >= 1),
            updated_at   TEXT NOT NULL,
            PRIMARY KEY (migration_id, prog_key)
        )
    """,
    META_TABLE: f"""
        CREATE TABLE {META_TABLE} (
            meta_key         TEXT    NOT NULL PRIMARY KEY
                             CHECK (meta_key = '{META_SINGLETON_KEY}'),
            layout_version   INTEGER NOT NULL,
            adapter          TEXT    NOT NULL,
            lock_provider    TEXT    NOT NULL,
            lock_binding     TEXT    NOT NULL,
            target_namespace TEXT    NOT NULL,
            initialized_at   TEXT    NOT NULL
        )
    """,
}

#: Expected ``(name, declared type, notnull)`` per logical table, checked on every run.
_EXPECTED_COLUMNS = {
    HISTORY_TABLE: (
        ("seq", "INTEGER", 1), ("migration_id", "TEXT", 1), ("fingerprint", "TEXT", 1),
        ("first_fingerprint", "TEXT", 1), ("language", "TEXT", 1), ("mode", "TEXT", 1),
        ("status", "TEXT", 1), ("attempt", "INTEGER", 0), ("started_at", "TEXT", 1),
        ("last_attempt_at", "TEXT", 0), ("finished_at", "TEXT", 0),
        ("runner_host", "TEXT", 0), ("runner_user", "TEXT", 0), ("runner_pid", "INTEGER", 0),
        ("db_session", "TEXT", 0), ("tool_version", "TEXT", 1),
    ),
    PROGRESS_TABLE: (
        ("migration_id", "TEXT", 1), ("prog_key", "TEXT", 1),
        ("prog_value", "TEXT", 1), ("updated_at", "TEXT", 1),
    ),
    META_TABLE: (
        ("meta_key", "TEXT", 1), ("layout_version", "INTEGER", 1), ("adapter", "TEXT", 1),
        ("lock_provider", "TEXT", 1), ("lock_binding", "TEXT", 1),
        ("target_namespace", "TEXT", 1), ("initialized_at", "TEXT", 1),
    ),
}


class SqliteProbeAdapter(Adapter):
    name = ADAPTER_NAME

    # --- dialect ---------------------------------------------------------------

    paramstyle = "named"
    now_expression = _NOW
    supports_on_conflict = True
    identifier_case = "lower"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        if config.database_path is None:
            raise ConfigError("the sqlite-probe adapter requires database.path")
        if sys.platform.startswith("win"):
            raise UnsupportedCapabilityError(
                "the sqlite-probe adapter's file lock profile does not support Windows"
            )
        self._path = Path(config.database_path).resolve(strict=False)
        self._lock_path = self._path.with_name(self._path.name + LOCK_SUFFIX)
        self.metadata_schema = None
        self._conn: sqlite3.Connection | None = None
        self._lock_fd: int | None = None
        self._txn_epoch = 0
        options = config.options.get("sqlite", {})
        if not isinstance(options, dict):
            raise ConfigError("[sqlite] must be a table")
        unknown = sorted(set(options) - {"busy_timeout_ms", "journal_mode"})
        if unknown:
            raise ConfigError(f"[sqlite] has unknown keys: {', '.join(unknown)}")
        self._busy_timeout_ms = int(options.get("busy_timeout_ms", 5000))
        self._journal_mode = str(options.get("journal_mode", "wal")).lower()
        if self._journal_mode not in ("wal", "delete"):
            raise ConfigError("sqlite.journal_mode must be 'wal' or 'delete'")

    # --- reporting --------------------------------------------------------------

    def capabilities(self) -> Capabilities:
        return Capabilities(
            adapter=self.name,
            transactional_ddl_in_atomic=True,
            oracle_style_required_objects=False,
            database_backed_lock=False,
            transaction_identity_tripwire=False,
            notes=(
                "Local probe only; not an Oracle emulator and not a production claim.",
                "Namespace lock is a POSIX advisory lock on "
                f"<database>{LOCK_SUFFIX}, held by the running process for the whole run.",
                "Transaction-contract enforcement boundary: facade statement admission "
                "plus SQLite's native in_transaction state and an adapter-owned "
                "transaction epoch. There is no server transaction identity.",
                "Nonempty Oracle-style require_valid lists are rejected.",
                f"Python {sys.version.split()[0]}, sqlite3 DB-API {sqlite3.apilevel}, "
                f"SQLite library {sqlite3.sqlite_version}. Driver transaction defaults "
                "vary by Python version, so this adapter sets them explicitly.",
            ),
        )

    def server_description(self) -> str:
        return (
            f"SQLite {sqlite3.sqlite_version} via Python {sys.version.split()[0]} "
            f"(file {self._path})"
        )

    def normalized_namespace(self) -> str:
        return str(self._path)

    def lock_binding(self) -> str:
        return f"file:{self._lock_path}"

    def session_identity(self) -> str | None:
        return f"pid={os.getpid()}"

    # --- lifecycle ---------------------------------------------------------------

    def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Legacy transaction control with isolation_level=None means the driver
        # never issues an implicit BEGIN: every transaction here is explicit.
        self._conn = sqlite3.connect(
            self._path,
            isolation_level=None,
            autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL,
            timeout=self._busy_timeout_ms / 1000.0,
        )
        self._conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        self._conn.execute("PRAGMA foreign_keys = ON")

    def prepare_storage(self) -> None:
        """Controlled probe initialization of the journal mode.

        Only ``migrate`` calls this, so ``status`` and ``validate`` cannot change
        a WAL setting as a side effect (spec Section 13.3).
        """
        assert self._conn is not None
        self._conn.execute(f"PRAGMA journal_mode = {self._journal_mode}")

    def close(self) -> None:
        if self._conn is not None:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            self._conn.close()
            self._conn = None
        self.release_lock()

    def discard(self) -> None:
        """Drop the connection without issuing any SQL."""
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None
        self.release_lock()

    # --- namespace lock ----------------------------------------------------------

    def acquire_lock(self) -> None:
        import fcntl

        # The lock file is created once and never unlinked: unlinking a live
        # lock file would let a second runner lock a different inode.
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + self.config.lock.timeout_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._lock_fd = fd
                return
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    os.close(fd)
                    raise UsageError(
                        f"cannot lock {self._lock_path}: {exc}"
                    ) from exc
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise LockNotAcquiredError(
                        f"another runner holds the probe lock {self._lock_path}; waited "
                        f"{self.config.lock.timeout_seconds}s. Contention is not evidence that "
                        "the other runner has stopped."
                    ) from exc
                time.sleep(0.05)

    def release_lock(self) -> None:
        if self._lock_fd is None:
            return
        import fcntl

        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(self._lock_fd)
            self._lock_fd = None

    # --- internals ---------------------------------------------------------------

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise UsageError("the sqlite-probe session is not connected")
        return self._conn

    def _objects_present(self) -> set[str]:
        rows = self._db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index') AND name IN "
            "(?,?,?,?)",
            (HISTORY_TABLE, PROGRESS_TABLE, META_TABLE, ACTIVE_INDEX),
        ).fetchall()
        return {row[0] for row in rows}

    def _definition_problems(self, present: set[str]) -> list[str]:
        problems: list[str] = []
        for table, expected in _EXPECTED_COLUMNS.items():
            if table not in present:
                continue
            info = self._db.execute(f"PRAGMA table_info({table})").fetchall()
            actual = tuple((row[1], row[2].upper(), row[3]) for row in info)
            if actual != expected:
                problems.append(
                    f"{table} column layout does not match the supported layout: "
                    f"found {actual}"
                )
        if ACTIVE_INDEX in present:
            row = self._db.execute(
                "SELECT sql, tbl_name FROM sqlite_master WHERE type='index' AND name=?",
                (ACTIVE_INDEX,),
            ).fetchone()
            sql = (row[0] or "") if row else ""
            if row is None or row[1] != HISTORY_TABLE:
                problems.append(f"{ACTIVE_INDEX} is not an index on {HISTORY_TABLE}")
            elif "UNIQUE" not in sql.upper() or "WHERE STATUS = 'ACTIVE'" not in sql.upper():
                problems.append(
                    f"{ACTIVE_INDEX} is not a unique partial index on status='ACTIVE': {sql!r}"
                )
        return problems

    def _read_meta(self, present: set[str]):
        if META_TABLE not in present:
            return None
        columns = ", ".join(md.META_COLUMNS)
        row = self._db.execute(
            f"SELECT {columns} FROM {META_TABLE} WHERE meta_key = ?", (META_SINGLETON_KEY,)
        ).fetchone()
        if row is None:
            return None
        return md.meta_row(tuple(row), md.parse_iso_timestamp)

    def _count(self, table: str, present: set[str]) -> int | None:
        if table not in present:
            return None
        return int(self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    # --- engine-owned SQL path, transactions and admission policy -----------------

    def _metadata_execute(self, sql: str, params) -> int:
        cursor = self._db.execute(sql, dict(params))
        return max(cursor.rowcount, 0)

    def _metadata_query(self, sql: str, params) -> list[tuple]:
        return [tuple(row) for row in self._db.execute(sql, dict(params)).fetchall()]

    def _do_begin(self) -> None:
        self._db.execute("BEGIN IMMEDIATE")
        self._txn_epoch += 1

    def _do_commit(self) -> None:
        self._db.execute("COMMIT")
        self._txn_epoch += 1

    def _do_rollback(self) -> None:
        if self._db.in_transaction:
            self._db.execute("ROLLBACK")
            self._txn_epoch += 1

    @property
    def statement_policy(self) -> StatementPolicy:
        return _POLICY

    # --- metadata -----------------------------------------------------------------

    def inspect_metadata(self) -> MetadataReport:
        present = self._objects_present()
        problems = self._definition_problems(present)
        meta = None
        if not problems or META_TABLE in present:
            try:
                meta = self._read_meta(present)
            except sqlite3.Error as exc:
                problems.append(f"{META_TABLE} cannot be read: {exc}")
        if meta is not None:
            extra_rows = int(
                self._db.execute(f"SELECT COUNT(*) FROM {META_TABLE}").fetchone()[0]
            )
            if extra_rows != 1:
                problems.append(f"{META_TABLE} holds {extra_rows} rows; exactly one is expected")
        return md.classify(
            present=present,
            problems=problems,
            meta=meta,
            history_count=self._count(HISTORY_TABLE, present),
            progress_count=self._count(PROGRESS_TABLE, present),
        )

    def initialize(self) -> None:
        """Create missing objects in fixed order, verify, then commit the marker last."""
        present = self._objects_present()
        for name in md.CREATION_ORDER:
            if name in present:
                continue
            # Each CREATE is its own durable step, so an interruption leaves a
            # recognisable prefix rather than a half-built object.
            self.begin()
            self._db.execute(_DDL[name])
            self.durable_commit(Boundary.METADATA_OBJECT_CREATED)

        present = self._objects_present()
        missing = sorted(set(md.CREATION_ORDER) - present)
        problems = self._definition_problems(present)
        if missing or problems:
            raise MetadataDamagedError(
                "metadata layout is not complete after creating objects: "
                + "; ".join([*(f"missing {name}" for name in missing), *problems])
            )

        self.begin()
        self._exec(
            f"INSERT INTO {META_TABLE} (meta_key, layout_version, adapter, lock_provider, "
            f"lock_binding, target_namespace, initialized_at) "
            f"VALUES (:key, :layout, :adapter, :provider, :binding, :namespace, {{now}})",
            {
                "key": META_SINGLETON_KEY,
                "layout": LAYOUT_VERSION,
                "adapter": self.name,
                "provider": self.config.lock.provider,
                "binding": self.lock_binding(),
                "namespace": self.normalized_namespace(),
            },
        )
        self.durable_commit(Boundary.INITIALIZATION_COMPLETE)

    def read_snapshot(self, *, consistent: bool) -> Snapshot:
        db = self._db
        opened = False
        if consistent and not db.in_transaction:
            db.execute("BEGIN DEFERRED")
            self._txn_epoch += 1
            opened = True
        try:
            history_cols = ", ".join(md.HISTORY_COLUMNS)
            history = tuple(
                md.history_row(tuple(row), md.parse_iso_timestamp)
                for row in db.execute(
                    f"SELECT {history_cols} FROM {HISTORY_TABLE} ORDER BY seq"
                ).fetchall()
            )
            progress_cols = ", ".join(md.PROGRESS_COLUMNS)
            progress = tuple(
                md.progress_row(tuple(row), md.parse_iso_timestamp)
                for row in db.execute(
                    f"SELECT {progress_cols} FROM {PROGRESS_TABLE} "
                    "ORDER BY migration_id, prog_key"
                ).fetchall()
            )
            meta = self._read_meta({META_TABLE})
        finally:
            if opened:
                db.execute("COMMIT")
                self._txn_epoch += 1
        return Snapshot(history=history, progress=progress, meta=meta)

    # --- history transitions -------------------------------------------------------

    # --- transaction control --------------------------------------------------------

    def has_open_transaction(self) -> bool:
        return bool(self._db.in_transaction)

    # --- transaction identity -------------------------------------------------------

    def establish_transaction_identity(self) -> str | None:
        """SQLite has no server transaction id; the epoch plus native state is the guard."""
        if not self._db.in_transaction:
            # BEGIN IMMEDIATE already acquired a write lock, so the transaction
            # is live; this path means the caller did not begin one.
            raise UsageError("no transaction is open when establishing atomic identity")
        return f"sqlite-txn:{self._txn_epoch}"

    def read_transaction_identity(self) -> str | None:
        if not self._db.in_transaction:
            return None
        return f"sqlite-txn:{self._txn_epoch}"

    # --- admission -------------------------------------------------------------------

    # --- execution --------------------------------------------------------------------

    def execute(self, statement: Statement, params: object | None) -> int:
        cursor = self._db.execute(statement.text, _bind(params))
        return max(cursor.rowcount, 0)

    def executemany(self, statement: Statement, parameter_sets: list[object]) -> int:
        cursor = self._db.executemany(statement.text, [_bind(item) for item in parameter_sets])
        return max(cursor.rowcount, 0)

    def query(self, statement: Statement, params: object | None) -> list[tuple]:
        cursor = self._db.execute(statement.text, _bind(params))
        return [tuple(row) for row in cursor.fetchall()]

    def execute_ddl(self, statement: Statement) -> None:
        # SQLite DDL is transactional; the probe runs it in its own transaction
        # rather than pretending Oracle's implicit commits exist.
        self.begin()
        try:
            self._db.execute(statement.text)
        except sqlite3.Error:
            self.rollback()
            raise
        self.durable_commit(Boundary.RESTARTABLE_DDL)

    # --- final validity ----------------------------------------------------------------

    # --- progress ----------------------------------------------------------------------

    # --- error classification -----------------------------------------------------------

    def classify_exception(self, exc: BaseException) -> OutcomeClass:
        """SQLite runs in-process, so a library error is a definite rejection.

        There is no transport to lose, so this adapter produces no unknown
        outcomes.  Unknown-outcome behaviour is evidenced against the real
        Oracle and PostgreSQL transports.
        """
        if isinstance(exc, sqlite3.Error):
            return OutcomeClass.SERVER_REJECTION
        return OutcomeClass.COMMUNICATION_FAILURE


def _bind(params: object | None):
    if params is None:
        return ()
    if isinstance(params, (list, tuple)):
        return tuple(params)
    if isinstance(params, dict):
        return params
    return (params,)
