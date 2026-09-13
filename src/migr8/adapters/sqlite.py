"""The ``sqlite`` adapter (spec Section 13.3).

Supported profile: a POSIX host, one database file on a local filesystem, and
cooperating runners that open the same canonical path.  Two differences from the
Oracle and PostgreSQL adapters are part of the contract, not defects:

* the namespace lock is a process-held POSIX advisory file lock associated with
  the canonical database path, not a database-backed lock;
* the transaction-contract guard is SQLite's native transaction state plus
  facade-level statement admission, not a server transaction identity.

Outside the profile: network filesystems, hard-link or symlink aliases opened
under a different canonical path, shared in-memory databases, and Windows
locking.  ``docs/MANUAL.md`` states the operational profile, including backup
and restore, and ``docs/ACCEPTANCE.md`` records the versions it was tested on.

Durability is set by this adapter and read back, because a database built with
different defaults would otherwise decide it: the journal mode is whichever of
``wal`` or ``delete`` the configuration selects, ``synchronous`` is ``full`` or
``extra``, and foreign keys are enforced.  A setting that does not read back as
requested fails the run rather than proceeding at an unknown durability.
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
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

ADAPTER_NAME = "sqlite"
LOCK_SUFFIX = ".m8lock"

#: Journal modes this adapter supports.  Both are exercised by the suite; the
#: other SQLite modes either drop the rollback journal or keep it in memory, and
#: neither survives the process death the recovery protocol is defined against.
SUPPORTED_JOURNAL_MODES = ("wal", "delete")

#: ``PRAGMA synchronous`` levels this adapter accepts, with the integer the
#: pragma reads back.  FULL is what SQLite documents for a durable WAL commit;
#: EXTRA additionally syncs the directory when a DELETE-mode journal is removed,
#: which is the difference that shows up as a lost last transaction after power
#: loss.  https://www.sqlite.org/pragma.html#pragma_synchronous
SYNCHRONOUS_LEVELS = {"full": 2, "extra": 3}

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

_KIND_NAMES = {"pk": "primary key", "u": "unique key", "fk": "foreign key"}

#: The keys each metadata table must have, in SQLite's own vocabulary and in key
#: order.  A foreign key names the table and columns it must point at: SQLite
#: resolves a reference within the one database file, and the facade refuses
#: ATTACH, so the table name is the whole target identity here.
_EXPECTED_KEYS: dict[str, tuple[md.ExpectedKey, ...]] = {
    HISTORY_TABLE: (md.ExpectedKey("pk", "migration_id"), md.ExpectedKey("u", "seq")),
    PROGRESS_TABLE: (
        md.ExpectedKey("pk", "migration_id,prog_key"),
        md.ExpectedKey("fk", "migration_id", (HISTORY_TABLE, "migration_id")),
    ),
    META_TABLE: (md.ExpectedKey("pk", "meta_key"),),
}

#: The single column the one-ACTIVE index must be keyed on, and the predicate
#: that must restrict it.  Both are compared: an index whose predicate names
#: ACTIVE but whose key is some other column is unique per row and holds nothing.
ACTIVE_INDEX_COLUMN = "status"
ACTIVE_INDEX_PREDICATE = "status = 'ACTIVE'"

#: One token of a stored definition.  String literals and quoted identifiers are
#: matched first, so a ``CHECK`` written inside either is never read as a clause.
_DEFINITION_TOKEN = re.compile(
    r"'(?:[^']|'')*'"
    r'|"(?:[^"]|"")*"'
    r"|\[[^\]]*\]"
    r"|`(?:[^`]|``)*`"
    r"|--[^\n]*"
    r"|/\*.*?\*/"
    r"|[A-Za-z_][A-Za-z0-9_$]*"
    r"|\d+(?:\.\d+)*"
    r"|<>|!=|<=|>=|\|\|"
    r"|[(),.;+\-*/%<>=~&|]"
    r"|\s+",
    re.DOTALL,
)


def _tokens(sql: str) -> list[tuple[str, int, int]] | None:
    """Significant tokens of a stored definition as ``(text, start, end)``.

    Returns ``None`` when the text holds something this scanner does not
    recognise.  The caller reports that as damage: deciding by inspection that
    an unreadable definition is equivalent is how one that enforces nothing gets
    accepted.
    """
    found: list[tuple[str, int, int]] = []
    position = 0
    while position < len(sql):
        match = _DEFINITION_TOKEN.match(sql, position)
        if match is None:
            return None
        position = match.end()
        text = match.group()
        if text.isspace() or text.startswith(("--", "/*")):
            continue
        found.append((text, match.start(), match.end()))
    return found


def _check_clauses(sql: str, tokens: list[tuple[str, int, int]]) -> list[str]:
    """The inner text of every ``CHECK ( ... )`` clause, in declaration order.

    Column-level and table-level checks are both returned: SQLite enforces them
    identically and stores them in the one statement.
    """
    clauses = []
    for index, (text, _start, _end) in enumerate(tokens):
        if text.lower() != "check" or index + 1 >= len(tokens) or tokens[index + 1][0] != "(":
            continue
        close = _closing_paren(tokens, index + 1)
        if close is not None:
            clauses.append(sql[tokens[index + 1][2] : tokens[close][1]])
    return clauses


def _closing_paren(tokens: list[tuple[str, int, int]], opening: int) -> int | None:
    depth = 0
    for index in range(opening, len(tokens)):
        if tokens[index][0] == "(":
            depth += 1
        elif tokens[index][0] == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _where_predicate(sql: str, tokens: list[tuple[str, int, int]]) -> str | None:
    """The text after a partial index's top-level ``WHERE``, or ``None``."""
    depth = 0
    for text, _start, end in tokens:
        if text == "(":
            depth += 1
        elif text == ")":
            depth -= 1
        elif depth == 0 and text.lower() == "where":
            return sql[end:]
    return None


def _canonical(sql: str) -> str | None:
    """Fold identifiers and whitespace while keeping literals exactly as written."""
    return md.canonical_condition(sql, fold=str.lower)


def _declared(sql: str) -> tuple[str, tuple[str, ...]]:
    """This module's own DDL, canonicalised the way a stored definition will be."""
    tokens = _tokens(sql)
    canonical = _canonical(sql)
    if tokens is None or canonical is None:  # pragma: no cover - this module's own text
        raise AssertionError("the adapter's own metadata DDL does not scan")
    checks = []
    for clause in _check_clauses(sql, tokens):
        condition = _canonical(clause)
        if condition is None:  # pragma: no cover - this module's own text
            raise AssertionError("the adapter's own check conditions do not scan")
        checks.append(condition)
    return canonical, tuple(checks)


#: What each object's stored definition must canonicalise to, and the complete
#: set of check conditions each table may carry.  Both are derived from ``_DDL``
#: rather than restated: SQLite keeps the statement this module issued, so a
#: second copy of the same text here could only drift from it.
_EXPECTED_DEFINITION = {name: _declared(ddl)[0] for name, ddl in _DDL.items()}
_EXPECTED_CHECKS = {name: _declared(_DDL[name])[1] for name in _EXPECTED_COLUMNS}


class SqliteAdapter(Adapter):
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
            raise ConfigError("the sqlite adapter requires database.path")
        if sys.platform.startswith("win"):
            raise UnsupportedCapabilityError(
                "the sqlite adapter's file lock profile does not support Windows"
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
        unknown = sorted(set(options) - {"busy_timeout_ms", "journal_mode", "synchronous"})
        if unknown:
            raise ConfigError(f"[sqlite] has unknown keys: {', '.join(unknown)}")
        self._busy_timeout_ms = int(options.get("busy_timeout_ms", 5000))
        self._journal_mode = str(options.get("journal_mode", "wal")).lower()
        if self._journal_mode not in SUPPORTED_JOURNAL_MODES:
            raise ConfigError(
                "sqlite.journal_mode must be "
                f"{' or '.join(repr(mode) for mode in SUPPORTED_JOURNAL_MODES)}"
            )
        self._synchronous = str(options.get("synchronous", "full")).lower()
        if self._synchronous not in SYNCHRONOUS_LEVELS:
            raise ConfigError(
                "sqlite.synchronous must be "
                f"{' or '.join(repr(level) for level in SYNCHRONOUS_LEVELS)}. "
                "'off' and 'normal' do not survive the power loss this engine's "
                "durable commits are defined against"
            )

    # --- reporting --------------------------------------------------------------------------------

    def capabilities(self) -> Capabilities:
        return Capabilities(
            adapter=self.name,
            transactional_ddl_in_atomic=True,
            oracle_style_required_objects=False,
            database_backed_lock=False,
            transaction_identity_tripwire=False,
            notes=(
                "One local database file on a local filesystem, opened by cooperating "
                "processes under the same canonical path. Network filesystems, path "
                "aliases and shared in-memory databases are outside the profile.",
                "Namespace lock is a POSIX advisory lock on "
                f"<database>{LOCK_SUFFIX}, held by the running process for the whole run.",
                "Transaction-contract enforcement boundary: facade statement admission "
                "plus SQLite's native in_transaction state and an adapter-owned "
                "transaction epoch. There is no server transaction identity.",
                "Nonempty Oracle-style require_valid lists are rejected.",
                f"Durability: journal_mode={self._journal_mode}, "
                f"synchronous={self._synchronous}, foreign_keys=on, "
                f"busy_timeout={self._busy_timeout_ms}ms, each read back on connect.",
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
        # A path the runner cannot use is a configuration answer, not an
        # internal failure: the operator needs the path and the reason, and the
        # fallback handler's "the database may have completed this" is exactly
        # what did not happen here.
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise UsageError(
                f"the directory {self._path.parent} for database.path cannot be used: "
                f"{exc.strerror or type(exc).__name__}"
            ) from exc
        # Legacy transaction control with isolation_level=None means the driver
        # never issues an implicit BEGIN: every transaction here is explicit.
        # typeshed types `autocommit` as bool; sqlite3.LEGACY_TRANSACTION_CONTROL
        # is the documented int sentinel for it.
        try:
            self._conn = sqlite3.connect(  # type: ignore[call-overload]
                self._path,
                isolation_level=None,
                autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL,
                timeout=self._busy_timeout_ms / 1000.0,
            )
        except sqlite3.Error as exc:
            raise UsageError(
                f"the database {self._path} cannot be opened: {self.describe_exception(exc)}"
            ) from exc
        self._conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        self._establish_settings()

    def _establish_settings(self) -> None:
        """Set every connection-level setting the adapter requires, and read it back.

        These are connection settings, not file settings: they are established on
        every connection, including the read-only commands', and each one is read
        back.  A build or a future driver default that silently refused one of
        them would otherwise leave the run enforcing less than it reports.

        SQLite parses the whole stored schema on the first statement of a
        connection, so a hand-edited or corrupt schema surfaces here rather than
        at the inspection that would have named the object at fault.  That is a
        damaged namespace, and it gets the exit code that says so.  Contention is
        not: a database another connection holds is unavailable now, and saying
        "damaged" about it would send an operator looking for a repair.
        """
        try:
            self._set_and_verify("foreign_keys", "ON", 1)
            # A connection that ignores check constraints accepts rows the layout
            # forbids while the inspection still reports the constraints as
            # present: the enforcement is connection state, so it is set here.
            self._set_and_verify("ignore_check_constraints", "OFF", 0)
            self._set_and_verify(
                "synchronous", self._synchronous, SYNCHRONOUS_LEVELS[self._synchronous]
            )
        except sqlite3.DatabaseError as exc:
            if _is_contention(exc):
                raise
            raise MetadataDamagedError(
                f"the schema of {self._path} cannot be read: {self.describe_exception(exc)}"
            ) from exc

    def _set_and_verify(self, pragma: str, value: str, expected: object) -> None:
        db = self._db
        db.execute(f"PRAGMA {pragma} = {value}")
        row = db.execute(f"PRAGMA {pragma}").fetchone()
        actual = None if row is None else row[0]
        if actual != expected:
            raise UnsupportedCapabilityError(
                f"PRAGMA {pragma} was set to {value} but reads back as {actual!r}; "
                f"this database cannot provide the durability settings the {ADAPTER_NAME} "
                "adapter requires"
            )

    def prepare_storage(self) -> None:
        """Set the journal mode, the one storage setting held in the file itself.

        Only ``migrate`` calls this, so ``status`` and ``validate`` cannot change
        a WAL setting as a side effect (spec Section 13.3).  The pragma returns
        the mode now in force: SQLite reports the *old* mode when it cannot make
        the change, for instance while another connection holds the database, so
        the answer is checked rather than assumed.
        """
        db = self._db
        try:
            row = db.execute(f"PRAGMA journal_mode = {self._journal_mode}").fetchone()
        except sqlite3.Error as exc:
            raise UnsupportedCapabilityError(
                f"journal mode {self._journal_mode!r} could not be established: "
                f"{self.describe_exception(exc)}. A mode change needs exclusive access to the "
                "database file, so another connection may hold it"
            ) from exc
        effective = None if row is None else str(row[0]).lower()
        if effective != self._journal_mode:
            raise UnsupportedCapabilityError(
                f"journal mode {self._journal_mode!r} was requested but the database is in "
                f"{effective!r} mode; a mode change needs exclusive access to the database file"
            )

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
                        f"another runner holds the namespace lock {self._lock_path}; waited "
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
            raise UsageError("the sqlite session is not connected")
        return self._conn

    def _objects_present(self) -> set[str]:
        rows = self._db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index') AND name IN (?,?,?,?)",
            (HISTORY_TABLE, PROGRESS_TABLE, META_TABLE, ACTIVE_INDEX),
        ).fetchall()
        return {row[0] for row in rows}

    def _stored_definition(self, name: str) -> tuple[str, str, str]:
        """``(object type, the table it belongs to, the statement SQLite kept)``."""
        row = self._db.execute(
            "SELECT type, tbl_name, sql FROM sqlite_master WHERE name = ?", (name,)
        ).fetchone()
        if row is None:  # pragma: no cover - the caller only asks about present objects
            return ("", "", "")
        return (row[0], row[1], row[2] or "")

    def _definition_problems(self, present: set[str]) -> list[str]:
        """Every way an existing metadata object differs from the supported layout.

        SQLite keeps the text of the statement that created an object and
        rewrites it when the object is altered, so the closing comparison is
        against that text: anything this module did not write is refused rather
        than assumed equivalent.  The structural checks run first because "the
        definition differs" is not an answer an operator can act on.
        """
        problems: list[str] = []
        for name in md.CREATION_ORDER:
            if name not in present:
                continue
            kind, owner, sql = self._stored_definition(name)
            if name in _EXPECTED_COLUMNS:
                found = self._table_problems(name, kind, sql)
            else:
                found = self._index_problems(name, kind, owner, sql)
            problems.extend(found or _definition_differences(name, sql))
        return problems

    def _table_problems(self, table: str, kind: str, sql: str) -> list[str]:
        if kind != "table":
            return [f"{table} is not a table: sqlite_master records it as {kind or 'absent'}"]
        problems = self._column_problems(table)
        problems.extend(self._key_problems(table))
        problems.extend(self._check_constraint_problems(table, sql))
        problems.extend(self._added_index_problems(table))
        return problems

    def _column_problems(self, table: str) -> list[str]:
        info = self._db.execute(f"PRAGMA table_info({table})").fetchall()
        actual = tuple((row[1], row[2].upper(), row[3]) for row in info)
        if actual == _EXPECTED_COLUMNS[table]:
            return []
        return [f"{table} column layout does not match the supported layout: found {actual}"]

    def _key_problems(self, table: str) -> list[str]:
        """Primary, unique and foreign keys, compared by columns and target.

        Foreign-key *enforcement* is a connection setting rather than part of
        the declaration, so it is established and read back in :meth:`connect`;
        a declared reference on a connection that ignores it enforces nothing.
        """
        info = self._db.execute(f"PRAGMA table_info({table})").fetchall()
        # table_info's last column is the column's 1-based position in the
        # primary key, so the key is read in key order rather than table order.
        primary = ",".join(row[1] for row in sorted(info, key=lambda row: row[5]) if row[5])
        unique = set()
        for _seq, index, is_unique, origin, _partial in self._db.execute(
            f"PRAGMA index_list({table})"
        ).fetchall():
            if origin == "u" and is_unique:
                unique.add(",".join(self._index_columns(index)))
        references: dict[int, tuple[str, list[str], list[str]]] = {}
        # Sorted by key and then by column position: a foreign key's columns are
        # ordered, and (b, a) is not the key (a, b).
        for row in sorted(
            self._db.execute(f"PRAGMA foreign_key_list({table})").fetchall(),
            key=lambda row: (row[0], row[1]),
        ):
            target, source_column, target_column = row[2], row[3], row[4]
            entry = references.setdefault(row[0], (target, [], []))
            entry[1].append(source_column)
            entry[2].append(target_column)
        foreign = {
            (",".join(columns), (target, ",".join(targets)))
            for target, columns, targets in references.values()
        }

        problems = []
        for kind, columns, target in _EXPECTED_KEYS[table]:
            if kind == "pk" and primary != columns:
                problems.append(
                    f"{table} primary key is ({primary}), not ({columns})"
                    if primary
                    else f"{table} has no primary key; ({columns}) is required"
                )
            elif kind == "u" and columns not in unique:
                problems.append(
                    f"{table} is missing a {_KIND_NAMES[kind]} on ({columns}); found "
                    f"{sorted(unique)}"
                )
            elif kind == "fk" and (columns, target) not in foreign:
                wanted = f"{target[0]}({target[1]})" if target else "nothing"
                problems.append(
                    f"{table} is missing a {_KIND_NAMES[kind]} on ({columns}) referencing "
                    f"{wanted}; found {sorted(foreign)}"
                )
        return problems

    def _index_columns(self, index: str) -> list[str]:
        return [row[2] for row in self._db.execute(f"PRAGMA index_info({index})").fetchall()]

    def _check_constraint_problems(self, table: str, sql: str) -> list[str]:
        tokens = _tokens(sql)
        if tokens is None:
            return [_unreadable(table)]
        # SQLite has no disabled or unvalidated constraint state: a CHECK that is
        # in the stored definition is enforced on the next write.
        return md.check_problems(
            table,
            ((clause, True, "enforced") for clause in _check_clauses(sql, tokens)),
            _EXPECTED_CHECKS[table],
            fold=self._fold,
        )

    def _added_index_problems(self, table: str) -> list[str]:
        """Indexes created on a metadata table beyond the supported layout.

        An added unique index constrains what the engine may write next, so the
        set is closed here in the same way the check conditions are.
        """
        allowed = {ACTIVE_INDEX} if table == HISTORY_TABLE else set()
        added = sorted(
            row[1]
            for row in self._db.execute(f"PRAGMA index_list({table})").fetchall()
            if row[3] == "c" and row[1] not in allowed
        )
        return (
            [f"{table} carries indexes the supported layout does not define: {added}"]
            if added
            else []
        )

    def _index_problems(self, name: str, kind: str, owner: str, sql: str) -> list[str]:
        if kind != "index":
            return [f"{name} is not an index: sqlite_master records it as {kind or 'absent'}"]
        if owner != HISTORY_TABLE:
            return [f"{name} is an index on {owner!r}, not on {HISTORY_TABLE}"]
        entry = next(
            (
                row
                for row in self._db.execute(f"PRAGMA index_list({HISTORY_TABLE})").fetchall()
                if row[1] == name
            ),
            None,
        )
        if entry is None:  # pragma: no cover - sqlite_master and index_list disagree
            return [f"{name} is not listed among the indexes of {HISTORY_TABLE}"]
        problems = []
        if not entry[2]:
            problems.append(f"{name} is not UNIQUE, so it holds nothing to a single row")
        columns = tuple(self._index_columns(name))
        if columns != (ACTIVE_INDEX_COLUMN,):
            problems.append(
                f"{name} is keyed on {columns}, not exactly ('{ACTIVE_INDEX_COLUMN}',): a unique "
                "index on any other key permits more than one ACTIVE migration"
            )
        tokens = _tokens(sql)
        if tokens is None:
            return [*problems, _unreadable(name)]
        predicate = _where_predicate(sql, tokens)
        if predicate is None:
            problems.append(
                f"{name} has no WHERE clause, so it is not the partial index on "
                f"{ACTIVE_INDEX_PREDICATE!r} the layout requires"
            )
        elif _canonical(predicate) != _canonical(ACTIVE_INDEX_PREDICATE):
            problems.append(
                f"{name} is restricted by {predicate!r}, not by {ACTIVE_INDEX_PREDICATE!r}"
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
        """Commit, and say so honestly when storage failed at the commit point.

        SQLite's commit point is the removal of the rollback journal, or the
        sync of the WAL commit record.  A write, sync or unlink error can be
        reported either side of it: the transaction may already be durable, and
        the next open of the file decides by replaying or rolling back the
        journal.  The codes that do settle it -- a busy or locked commit, a
        deferred constraint, a refusal made before anything was written -- keep
        the ordinary definite path.

        Sources: https://www.sqlite.org/lang_transaction.html,
        https://www.sqlite.org/atomiccommit.html.
        """
        try:
            self._db.execute("COMMIT")
        except sqlite3.Error as exc:
            if _settles_the_transaction(exc):
                raise
            raise CommitOutcomeUnknown(getattr(exc, "sqlite_errorname", None)) from exc
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
        """SQLite runs in-process, so an error it raises is a definite rejection.

        There is no transport to lose.  A failed statement leaves the
        transaction open with nothing durable, so the engine may roll it back
        and report an ordinary failure.

        One case is not definite, and :meth:`_do_commit` raises
        :class:`CommitOutcomeUnknown` for it rather than the library error, so
        it reaches the clause below: a COMMIT that fails on storage.
        """
        if isinstance(exc, sqlite3.Error):
            return OutcomeClass.SERVER_REJECTION
        return OutcomeClass.COMMUNICATION_FAILURE

    def error_code(self, exc: BaseException) -> str | None:
        """SQLite's symbolic result code, which names the fault but not the row."""
        if isinstance(exc, CommitOutcomeUnknown):
            return exc.code
        if isinstance(exc, sqlite3.Error):
            return getattr(exc, "sqlite_errorname", None)
        return None


class CommitOutcomeUnknown(Exception):
    """A COMMIT that failed on storage, where the next open of the file decides.

    Deliberately not a :class:`sqlite3.Error`: the operation guard classifies
    anything else as a communication failure, which is what this is -- the
    library reported a failure whose durable effect it does not settle.
    """

    def __init__(self, code: str | None) -> None:
        super().__init__(code or "unknown result code")
        self.code = code


#: Result codes whose meaning at a commit is settled: the transaction is still
#: open and nothing was made durable.  SQLITE_BUSY and SQLITE_LOCKED are the
#: documented busy-commit cases, a deferred foreign key surfaces as
#: SQLITE_CONSTRAINT at COMMIT, and the rest are refusals the library makes
#: before it writes anything.  Anything outside this set on a commit is read as
#: an unknown outcome rather than assumed harmless.
_SETTLED_COMMIT_CODES = (
    "SQLITE_BUSY",
    "SQLITE_LOCKED",
    "SQLITE_CONSTRAINT",
    "SQLITE_READONLY",
    "SQLITE_MISUSE",
    "SQLITE_AUTH",
    "SQLITE_PERM",
)


def _is_contention(exc: sqlite3.Error) -> bool:
    """True when another connection holds what this one asked for."""
    code = getattr(exc, "sqlite_errorname", None)
    return bool(code) and str(code).startswith(("SQLITE_BUSY", "SQLITE_LOCKED"))


def _settles_the_transaction(exc: sqlite3.Error) -> bool:
    """True when this error leaves the transaction open with nothing committed."""
    code = getattr(exc, "sqlite_errorname", None)
    return bool(code) and str(code).startswith(_SETTLED_COMMIT_CODES)


def _unreadable(name: str) -> str:
    return f"{name} has a definition this tool cannot read; no equivalence is assumed"


def _definition_differences(name: str, sql: str) -> list[str]:
    """The closing comparison: the stored statement against the one this module issues.

    It runs only where the structural checks found nothing, and it is what
    refuses everything they do not name -- ``WITHOUT ROWID``, ``STRICT``, a
    default, a collation, a generated column -- instead of accepting a layout
    this engine has never written to.
    """
    stored = _canonical(sql)
    if stored == _EXPECTED_DEFINITION[name]:
        return []
    if stored is None:
        return [_unreadable(name)]
    return [f"{name} is defined as {sql!r}, which is not the supported layout"]


def _bind(params: object | None):
    """sqlite3 refuses ``None`` where it expects a parameter sequence."""
    return bind_params(params, empty=())
