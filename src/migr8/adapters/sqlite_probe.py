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

import contextlib
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
    UnsupportedCapabilityError,
    UsageError,
)
from ..model import (
    ACTIVE_INDEX,
    HISTORY_TABLE,
    META_SINGLETON_KEY,
    META_TABLE,
    PROGRESS_TABLE,
    Snapshot,
)
from ..sqltext import Statement
from . import metadata as md
from .base import (
    Adapter,
    Boundary,
    Capabilities,
    OutcomeClass,
    StatementPolicy,
    bind_params,
)

ADAPTER_NAME = "sqlite-probe"
LOCK_SUFFIX = ".m8lock"

#: Database time, produced by SQLite rather than by the Python process.
_NOW = "strftime('%Y-%m-%dT%H:%M:%f','now')"

#: SQLite has transactional DDL, so DDL is explicitly admitted in atomic work.
#: PRAGMA, VACUUM, ATTACH and DETACH are not: they can commit or change
#: connection-wide state.
_POLICY = StatementPolicy(
    atomic=frozenset(
        {
            "SELECT",
            "WITH",
            "VALUES",
            "INSERT",
            "REPLACE",
            "UPDATE",
            "DELETE",
            "CREATE",
            "ALTER",
            "DROP",
        }
    ),
    query=frozenset({"SELECT", "WITH", "VALUES"}),
    procedural=frozenset(),
    ddl=frozenset({"CREATE", "ALTER", "DROP"}),
    forbidden=frozenset(
        {
            "BEGIN",
            "COMMIT",
            "END",
            "ROLLBACK",
            "SAVEPOINT",
            "RELEASE",
            "PRAGMA",
            "VACUUM",
            "ATTACH",
            "DETACH",
            "ANALYZE",
            "REINDEX",
        }
    ),
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
        ("seq", "INTEGER", 1),
        ("migration_id", "TEXT", 1),
        ("fingerprint", "TEXT", 1),
        ("first_fingerprint", "TEXT", 1),
        ("language", "TEXT", 1),
        ("mode", "TEXT", 1),
        ("status", "TEXT", 1),
        ("attempt", "INTEGER", 0),
        ("started_at", "TEXT", 1),
        ("last_attempt_at", "TEXT", 0),
        ("finished_at", "TEXT", 0),
        ("runner_host", "TEXT", 0),
        ("runner_user", "TEXT", 0),
        ("runner_pid", "INTEGER", 0),
        ("db_session", "TEXT", 0),
        ("tool_version", "TEXT", 1),
    ),
    PROGRESS_TABLE: (
        ("migration_id", "TEXT", 1),
        ("prog_key", "TEXT", 1),
        ("prog_value", "TEXT", 1),
        ("updated_at", "TEXT", 1),
    ),
    META_TABLE: (
        ("meta_key", "TEXT", 1),
        ("layout_version", "INTEGER", 1),
        ("adapter", "TEXT", 1),
        ("lock_provider", "TEXT", 1),
        ("lock_binding", "TEXT", 1),
        ("target_namespace", "TEXT", 1),
        ("initialized_at", "TEXT", 1),
    ),
}


class SqliteProbeAdapter(Adapter):
    name = ADAPTER_NAME
    driver_error = sqlite3.Error

    # --- dialect ----------------------------------------------------------------------------------

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

    # --- reporting --------------------------------------------------------------------------------

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

    # --- lifecycle --------------------------------------------------------------------------------

    def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Legacy transaction control with isolation_level=None means the driver
        # never issues an implicit BEGIN: every transaction here is explicit.
        # typeshed types `autocommit` as bool; sqlite3.LEGACY_TRANSACTION_CONTROL
        # is the documented int sentinel for it.
        self._conn = sqlite3.connect(  # type: ignore[call-overload]
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
            with contextlib.suppress(sqlite3.Error):
                self._conn.close()
            self._conn = None
        self.release_lock()

    # --- namespace lock ---------------------------------------------------------------------------

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
                    raise UsageError(f"cannot lock {self._lock_path}: {exc}") from exc
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

    # --- internals --------------------------------------------------------------------------------

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise UsageError("the sqlite-probe session is not connected")
        return self._conn

    def _objects_present(self) -> set[str]:
        rows = self._db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index') AND name IN (?,?,?,?)",
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
                    f"{table} column layout does not match the supported layout: found {actual}"
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

    # --- engine-owned SQL path, transactions and admission policy ---------------------------------

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

    # --- metadata ---------------------------------------------------------------------------------

    def _create_metadata_object(self, name: str) -> None:
        """SQLite DDL is transactional, so each CREATE is its own durable step."""
        self.begin()
        self._db.execute(_DDL[name])
        self.durable_commit(Boundary.METADATA_OBJECT_CREATED)

    def _read_snapshot(self, consistent: bool) -> Snapshot:
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
                    f"SELECT {progress_cols} FROM {PROGRESS_TABLE} ORDER BY migration_id, prog_key"
                ).fetchall()
            )
            meta, _rows = self._read_meta()
        finally:
            if opened:
                db.execute("COMMIT")
                self._txn_epoch += 1
        return Snapshot(history=history, progress=progress, meta=meta)

    # --- transaction control ----------------------------------------------------------------------

    def _has_open_transaction(self) -> bool:
        return bool(self._db.in_transaction)

    # --- transaction identity ---------------------------------------------------------------------

    def _establish_transaction_identity(self) -> str | None:
        """SQLite has no server transaction id; the epoch plus native state is the guard."""
        if not self._db.in_transaction:
            # BEGIN IMMEDIATE already acquired a write lock, so the transaction
            # is live; this path means the caller did not begin one.
            raise UsageError("no transaction is open when establishing atomic identity")
        return f"sqlite-txn:{self._txn_epoch}"

    def _read_transaction_identity(self) -> str | None:
        if not self._db.in_transaction:
            return None
        return f"sqlite-txn:{self._txn_epoch}"

    # --- execution --------------------------------------------------------------------------------

    def _run(self, text: str, params: object | None):
        return self._db.execute(text, _bind(params))

    def executemany(self, statement: Statement, parameter_sets: list[object]) -> int:
        cursor = self._db.executemany(statement.text, [_bind(item) for item in parameter_sets])
        return max(cursor.rowcount, 0)

    # --- error classification ---------------------------------------------------------------------

    def classify_exception(self, exc: BaseException) -> OutcomeClass:
        """SQLite runs in-process, so a library error is a definite rejection.

        There is no transport to lose, so this adapter produces no unknown
        outcomes.  Unknown-outcome behaviour is evidenced against the real
        Oracle and PostgreSQL transports.
        """
        if isinstance(exc, sqlite3.Error):
            return OutcomeClass.SERVER_REJECTION
        return OutcomeClass.COMMUNICATION_FAILURE

    def error_code(self, exc: BaseException) -> str | None:
        """SQLite's symbolic result code, which names the fault but not the row."""
        if isinstance(exc, sqlite3.Error):
            return getattr(exc, "sqlite_errorname", None)
        return None


def _bind(params: object | None):
    """sqlite3 refuses ``None`` where it expects a parameter sequence."""
    return bind_params(params, empty=())
