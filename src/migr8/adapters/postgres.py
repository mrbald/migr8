"""The PostgreSQL adapter: the second adapter and an independent behaviour check
(spec Section 13.2).

PostgreSQL is not an Oracle emulator here. It uses its own transactional DDL,
its own session-level advisory lock and its own transaction identity. Its tests
validate the state machine and PostgreSQL behaviour; they do not substitute for
Oracle's DDL, PL/SQL, lock or commit-outcome evidence.

The driver connection runs in autocommit mode and every engine transaction is an
explicit ``BEGIN`` / ``COMMIT``. That is an adapter choice, not a relaxation of
the one-physical-session requirement: the same session holds the advisory lock
across every commit.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import psycopg

from ..config import Config
from ..errors import ConfigError, LockNotAcquiredError, UsageError
from ..manifest import Mode
from ..model import (
    ACTIVE_INDEX,
    HISTORY_TABLE,
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

LOGGER = logging.getLogger("migr8.adapters.postgres")

ADAPTER_NAME = "postgres"

IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_$]{0,62}$")

#: PostgreSQL has transactional DDL, so DDL is admitted in atomic migrations
#: rather than smuggled in. Statements that cannot run inside a transaction
#: block are refused there and belong to ctx.ddl() in a restartable migration.
_POLICY = StatementPolicy(
    atomic=frozenset(
        {
            "SELECT",
            "WITH",
            "VALUES",
            "INSERT",
            "UPDATE",
            "DELETE",
            "MERGE",
            "LOCK",
            "CREATE",
            "ALTER",
            "DROP",
            "TRUNCATE",
            "COMMENT",
            "DO",
        }
    ),
    query=frozenset({"SELECT", "WITH", "VALUES"}),
    procedural=frozenset({"DO", "CALL"}),
    ddl=frozenset({"CREATE", "ALTER", "DROP", "TRUNCATE", "COMMENT", "REINDEX"}),
    forbidden=frozenset(
        {
            "BEGIN",
            "COMMIT",
            "END",
            "ROLLBACK",
            "SAVEPOINT",
            "RELEASE",
            "START",
            "SET",
            "RESET",
            "DISCARD",
            "LISTEN",
            "NOTIFY",
            "GRANT",
            "REVOKE",
            "VACUUM",
            "PREPARE",
            "DEALLOCATE",
            "COPY",
        }
    ),
    allows_plsql=False,
)


def _require_identifier(value: str, what: str) -> str:
    lower = value.lower()
    if not IDENTIFIER_RE.match(lower) or lower != value:
        raise ConfigError(
            f"{what} must be an unquoted lowercase PostgreSQL identifier matching "
            f"[a-z_][a-z0-9_$]*; got {value!r}"
        )
    return lower


class PostgresAdapter(Adapter):
    name = ADAPTER_NAME
    driver_error = psycopg.Error

    # --- dialect ----------------------------------------------------------------------------------

    paramstyle = "pyformat"
    now_expression = "clock_timestamp()"
    supports_on_conflict = True
    identifier_case = "lower"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        if not config.dsn:
            raise ConfigError("the postgres adapter requires database.dsn")
        self._schema = _require_identifier(
            config.target_schema or "public", "database.target_schema"
        )
        self.metadata_schema = self._schema
        options = config.options.get("postgres", {})
        if not isinstance(options, dict):
            raise ConfigError("[postgres] must be a table")
        unknown = sorted(set(options) - {"lock_poll_seconds"})
        if unknown:
            raise ConfigError(f"[postgres] has unknown keys: {', '.join(unknown)}")
        self._poll = float(options.get("lock_poll_seconds", 0.1))

        lock = config.lock
        if lock.provider != "advisory":
            raise ConfigError(
                f'the postgres adapter requires lock.provider = "advisory", got '
                f"{lock.provider!r}; the engine will not fall back to an ineffective lock"
            )
        if lock.id is None:
            raise ConfigError("the postgres adapter requires an explicit lock.id")
        self._lock_id = lock.id
        self._lock_timeout = lock.timeout_seconds

        self._conn: psycopg.Connection | None = None
        self._lock_held = False
        self._banner = "not connected"
        self._session_identity: str | None = None
        self._synchronous_commit = "not established"

    # --- reporting --------------------------------------------------------------------------------

    def capabilities(self) -> Capabilities:
        return Capabilities(
            adapter=self.name,
            transactional_ddl_in_atomic=True,
            oracle_style_required_objects=False,
            database_backed_lock=True,
            transaction_identity_tripwire=True,
            notes=(
                "Session-level advisory lock, held across transaction boundaries, "
                "acquired with pg_try_advisory_lock polling so the timeout is the "
                "operator's policy.",
                f"synchronous_commit read back from the session: {self._synchronous_commit}",
                "Transactional DDL is admitted in atomic migrations. Oracle's implicit "
                "DDL commits are NOT emulated.",
                "Statements that cannot run inside a transaction block, such as CREATE "
                "INDEX CONCURRENTLY, are executed outside one by ctx.ddl().",
                "Transaction identity uses pg_current_xact_id / pg_current_xact_id_if_assigned.",
                "Nonempty Oracle-style require_valid lists are rejected.",
                f"psycopg {psycopg.__version__}; server {self._banner}.",
            ),
        )

    def server_description(self) -> str:
        return self._banner

    def normalized_namespace(self) -> str:
        return self._schema

    def lock_binding(self) -> str:
        return f"advisory:{self._lock_id}"

    def session_identity(self) -> str | None:
        return self._session_identity

    # --- lifecycle --------------------------------------------------------------------------------

    def connect(self) -> None:
        password = self.config.password()
        conninfo = self.config.dsn or ""
        if password:
            conninfo = f"{conninfo} password={password}"
        try:
            # Autocommit at the driver level; every engine transaction is an
            # explicit BEGIN/COMMIT issued by this adapter.
            self._conn = psycopg.connect(conninfo, autocommit=True)
        except psycopg.Error as exc:
            # The driver's connection message repeats the connection string, so it
            # is reported by code and the operator is pointed at the settings.
            raise UsageError(
                f"cannot connect to PostgreSQL: {self.describe_exception(exc)}. Check "
                "database.dsn, database.user and the MIGR8_PASSWORD environment variable."
            ) from exc
        self._configure_session()
        self._banner = self._read_banner()
        self._session_identity = self._read_session_identity()

    def _configure_session(self) -> None:
        conn = self._db
        try:
            # Durability first: no metadata write or lock request precedes it.
            conn.execute("SET synchronous_commit = on")
            row = conn.execute("SHOW synchronous_commit").fetchone()
        except psycopg.Error as exc:
            raise UsageError(
                f"cannot establish synchronous commit durability: {exc}. There is no "
                "setting to weaken required commit durability, so setup fails."
            ) from exc
        value = row[0] if row else None
        if value != "on":
            raise UsageError(
                f"synchronous_commit read back as {value!r}, not 'on'; required commit "
                "durability is not established"
            )
        self._synchronous_commit = "VERIFIED 'on' by session read-back"
        try:
            conn.execute(f"SET search_path = {self.quoted(self._schema)}, pg_catalog")
        except psycopg.Error as exc:
            raise UsageError(f"cannot set search_path to {self._schema}: {exc}") from exc
        row = conn.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname = %s", (self._schema,)
        ).fetchone()
        if row is None:
            raise UsageError(
                f"target schema {self._schema!r} does not exist; create it before running "
                "migrations. An inaccessible probe is an error, not evidence of emptiness."
            )

    def _read_banner(self) -> str:
        row = self._db.execute("SELECT version()").fetchone()
        return f"{row[0]} [psycopg {psycopg.__version__}]" if row else "unknown"

    def _read_session_identity(self) -> str:
        row = self._db.execute(
            "SELECT pg_backend_pid(), "
            "to_char(backend_start, 'YYYY-MM-DD\"T\"HH24:MI:SS.US'), "
            "current_setting('cluster_name', true) "
            "FROM pg_stat_activity WHERE pid = pg_backend_pid()"
        ).fetchone()
        if row is None:
            return f"pid={self._db.info.backend_pid},backend_start=unavailable"
        return f"pid={row[0]},backend_start={row[1]},cluster={row[2] or 'default'}"

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            # Teardown asks the driver directly: a failure while closing must not
            # be reclassified as an unknown migration outcome.
            if self._has_open_transaction():
                self._conn.execute("ROLLBACK")
            self.release_lock()
        finally:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def discard(self) -> None:
        """Drop the session without issuing any further SQL."""
        if self._conn is None:
            return
        self._lock_held = False
        try:
            self._conn.close()
        except psycopg.Error:
            pass
        finally:
            self._conn = None

    # --- namespace lock ---------------------------------------------------------------------------

    def acquire_lock(self) -> None:
        if self._lock_held:
            raise UsageError("the namespace lock is already held by this run")
        deadline = time.monotonic() + self._lock_timeout
        while True:
            row = self._db.execute("SELECT pg_try_advisory_lock(%s)", (self._lock_id,)).fetchone()
            if row and row[0]:
                self._lock_held = True
                return
            if time.monotonic() >= deadline:
                raise LockNotAcquiredError(
                    f"advisory lock {self._lock_id} not acquired after "
                    f"{self._lock_timeout}s. Another runner may hold it, or a previous "
                    "server session may not have ended. Contention is not a lock-test "
                    "failure."
                )
            time.sleep(self._poll)

    def release_lock(self) -> None:
        if not self._lock_held or self._conn is None:
            return
        try:
            self._conn.execute("SELECT pg_advisory_unlock(%s)", (self._lock_id,))
        except psycopg.Error as exc:
            LOGGER.warning("releasing advisory lock %s failed: %s", self._lock_id, exc)
        finally:
            self._lock_held = False

    # --- internals --------------------------------------------------------------------------------

    @property
    def _db(self) -> psycopg.Connection[tuple[Any, ...]]:
        if self._conn is None:
            raise UsageError("the PostgreSQL session is not connected")
        return self._conn

    # --- engine-owned SQL path, transactions and admission policy ---------------------------------

    def _metadata_execute(self, sql: str, params) -> int:
        cursor = self._db.execute(sql, dict(params))
        return max(cursor.rowcount, 0)

    def _metadata_query(self, sql: str, params) -> list[tuple]:
        return [tuple(row) for row in self._db.execute(sql, dict(params)).fetchall()]

    def _do_begin(self) -> None:
        self._db.execute("BEGIN")

    def _do_commit(self) -> None:
        self._db.execute("COMMIT")

    def _do_rollback(self) -> None:
        if self.has_open_transaction():
            self._db.execute("ROLLBACK")

    @property
    def statement_policy(self) -> StatementPolicy:
        return _POLICY

    def _extra_statement_checks(self, statement: Statement, *, mode: Mode, in_batch: bool) -> None:
        """CONCURRENTLY cannot run inside a transaction block."""
        if (mode is Mode.ATOMIC or in_batch) and _is_concurrent(statement):
            raise UsageError(
                f"{' '.join(statement.lead[:3])} cannot run inside a transaction block; "
                "use ctx.ddl() in a restartable migration"
            )

    # --- metadata ---------------------------------------------------------------------------------

    def _objects_present(self) -> set[str]:
        rows = self._db.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = ANY(%s) AND c.relkind IN ('r','i')",
            (self._schema, list(md.CREATION_ORDER)),
        ).fetchall()
        return {row[0] for row in rows}

    def _definition_problems(self, present: set[str]) -> list[str]:
        problems: list[str] = []
        for table, expected in _EXPECTED_COLUMNS.items():
            if table not in present:
                continue
            rows = self._db.execute(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull "
                "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 "
                "AND NOT a.attisdropped ORDER BY a.attnum",
                (self._schema, table),
            ).fetchall()
            actual = tuple((row[0], row[1], row[2]) for row in rows)
            if actual != expected:
                problems.append(
                    f"{table} column layout does not match the supported layout. Found: {actual}"
                )
        problems.extend(self._constraint_problems(present))
        problems.extend(self._index_problems(present))
        return problems

    def _constraint_problems(self, present: set[str]) -> list[str]:
        problems: list[str] = []
        for table, expected in _EXPECTED_CONSTRAINTS.items():
            if table not in present:
                continue
            rows = self._db.execute(
                # Key columns are read in key order, not table order: WITH
                # ORDINALITY over conkey/confkey keeps (b, a) distinct from
                # (a, b), which a plain ANY() lookup ordered by attnum loses.
                "SELECT con.contype, con.convalidated, "
                "  pg_get_constraintdef(con.oid), "
                "  (SELECT string_agg(att.attname, ',' ORDER BY k.ord) "
                "     FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) "
                "     JOIN pg_attribute att ON att.attrelid = con.conrelid "
                "      AND att.attnum = k.attnum), "
                "  (SELECT rn.nspname FROM pg_class rc "
                "     JOIN pg_namespace rn ON rn.oid = rc.relnamespace "
                "    WHERE rc.oid = con.confrelid), "
                "  (SELECT rc.relname FROM pg_class rc WHERE rc.oid = con.confrelid), "
                "  (SELECT string_agg(att.attname, ',' ORDER BY k.ord) "
                "     FROM unnest(con.confkey) WITH ORDINALITY AS k(attnum, ord) "
                "     JOIN pg_attribute att ON att.attrelid = con.confrelid "
                "      AND att.attnum = k.attnum) "
                "FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relname = %s",
                (self._schema, table),
            ).fetchall()
            keys: dict[tuple[str, str], list[tuple]] = {}
            for row in rows:
                if row[0] in ("p", "u", "f"):
                    keys.setdefault((row[0], row[3] or ""), []).append(row)
            for expected_key in expected["keys"]:
                kind, columns, references = expected_key
                matches = keys.get((kind, columns))
                if not matches:
                    problems.append(
                        f"{table} is missing a {_KIND_NAMES[kind]} on ({columns}); found "
                        f"{sorted(keys)}"
                    )
                    continue
                for (
                    _type,
                    validated,
                    _definition,
                    _columns,
                    target_schema,
                    target,
                    target_columns,
                ) in matches:
                    # A key the server is not enforcing does not hold the layout up.
                    if not validated:
                        problems.append(f"{table} {_KIND_NAMES[kind]} on ({columns}) is NOT VALID")
                    if references is None:
                        continue
                    wanted_table, wanted_columns = references
                    # The schema is part of the target identity: a same-named
                    # history table in another schema would otherwise satisfy
                    # this check while linking progress to that schema's history.
                    expected_target = (
                        f"{self.metadata_schema}."
                        f"{self._physical_object_name(wanted_table)}({wanted_columns})"
                    )
                    actual = (
                        f"{target_schema}.{target}({target_columns or ''})" if target else "nothing"
                    )
                    if actual != expected_target:
                        problems.append(
                            f"{table} {_KIND_NAMES[kind]} on ({columns}) references {actual}, "
                            f"not {expected_target}"
                        )
            problems.extend(
                md.check_problems(
                    table,
                    ((row[2], bool(row[1]), "NOT VALID") for row in rows if row[0] == "c"),
                    expected["checks"],
                    fold=self._fold,
                )
            )
        return problems

    def _index_problems(self, present: set[str]) -> list[str]:
        if ACTIVE_INDEX not in present:
            return []
        row = self._db.execute(
            "SELECT i.indisunique, i.indisvalid, i.indisready, "
            "  pg_get_indexdef(i.indexrelid), c.relname, i.indnatts, "
            "  pg_get_expr(i.indpred, i.indrelid), "
            "  (SELECT string_agg(a.attname, ',' ORDER BY k.ord) "
            "     FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) "
            "     JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum) "
            "FROM pg_index i JOIN pg_class x ON x.oid = i.indexrelid "
            "JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = x.relnamespace "
            "WHERE n.nspname = %s AND x.relname = %s",
            (self._schema, ACTIVE_INDEX),
        ).fetchone()
        if row is None:
            return [f"{ACTIVE_INDEX} is not visible in pg_index"]
        unique, valid, ready, definition, table_name, columns, predicate, indexed = row
        problems = []
        if table_name != HISTORY_TABLE:
            problems.append(f"{ACTIVE_INDEX} is on {table_name}, not {HISTORY_TABLE}")
        if not unique:
            problems.append(f"{ACTIVE_INDEX} is not UNIQUE")
        if not valid or not ready:
            problems.append(f"{ACTIVE_INDEX} is not valid and ready")
        # The indexed column and the predicate are checked separately: an index
        # whose predicate mentions ACTIVE but whose key is some other column is
        # unique per row and enforces nothing.
        if int(columns) != 1 or (indexed or "") != ACTIVE_INDEX_COLUMN:
            problems.append(
                f"{ACTIVE_INDEX} indexes ({indexed}), not exactly ({ACTIVE_INDEX_COLUMN}): "
                f"{definition!r}"
            )
        if (
            md.compact_definition(predicate or "", drop_parens=True)
            not in _SUPPORTED_ACTIVE_INDEX_PREDICATES
        ):
            problems.append(
                f"{ACTIVE_INDEX} is not restricted to {ACTIVE_INDEX_PREDICATE}: {definition!r}"
            )
        return problems

    def _create_metadata_object(self, name: str) -> None:
        """PostgreSQL DDL is transactional, so each object gets its own commit.

        Oracle's implicit DDL commits are not emulated.
        """
        self.begin()
        self._db.execute(
            _DDL[name].format(
                history=self.metadata_name(HISTORY_TABLE),
                progress=self.metadata_name(PROGRESS_TABLE),
                meta=self.metadata_name(META_TABLE),
                # CREATE INDEX takes a bare name; PostgreSQL puts the index in the
                # schema of the table it indexes.
                index=self.quoted(self._physical_object_name(ACTIVE_INDEX)),
            )
        )
        self.durable_commit(Boundary.METADATA_OBJECT_CREATED)

    def _read_snapshot(self, consistent: bool) -> Snapshot:
        conn = self._db
        opened = False
        if consistent and not self._has_open_transaction():
            # A read-only repeatable-read transaction gives one consistent view and
            # reads committed metadata without taking the migration lock.
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            opened = True
        try:
            history = tuple(
                md.history_row(row, md.parse_iso_timestamp)
                for row in self._fetch(
                    f"SELECT {self._columns(*md.HISTORY_COLUMNS)} "
                    f"FROM {self.metadata_name(HISTORY_TABLE)} "
                    f"ORDER BY {self.metadata_column('seq')}",
                    {},
                )
            )
            progress = tuple(
                md.progress_row(row, md.parse_iso_timestamp)
                for row in self._fetch(
                    f"SELECT {self._columns(*md.PROGRESS_COLUMNS)} "
                    f"FROM {self.metadata_name(PROGRESS_TABLE)} "
                    f"ORDER BY {self.metadata_column('migration_id')}, "
                    f"{self.metadata_column('prog_key')}",
                    {},
                )
            )
            meta, _rows = self._read_meta()
        finally:
            if opened:
                conn.execute("COMMIT")
        return Snapshot(history=history, progress=progress, meta=meta)

    # --- transaction control ----------------------------------------------------------------------

    def _has_open_transaction(self) -> bool:
        if self._conn is None:
            return False
        return self._conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE

    # --- transaction identity ---------------------------------------------------------------------

    def _establish_transaction_identity(self) -> str | None:
        row = self._db.execute("SELECT pg_current_xact_id()::text").fetchone()
        if row is None or row[0] is None:
            raise UsageError(
                "PostgreSQL did not assign a transaction id when establishing atomic "
                "transaction identity"
            )
        return str(row[0])

    def _read_transaction_identity(self) -> str | None:
        if not self._has_open_transaction():
            return None
        row = self._db.execute("SELECT pg_current_xact_id_if_assigned()::text").fetchone()
        return None if row is None or row[0] is None else str(row[0])

    # --- execution --------------------------------------------------------------------------------

    def _run(self, text: str, params: object | None):
        return self._db.execute(text, _bind(params))

    def executemany(self, statement: Statement, parameter_sets: list[object]) -> int:
        with self._db.cursor() as cursor:
            # Raise on the first error rather than collecting per-row failures.
            cursor.executemany(statement.text, [_bind(item) for item in parameter_sets])
            return max(cursor.rowcount, 0)

    def execute_ddl(self, statement: Statement) -> None:
        if _is_concurrent(statement):
            # CREATE/DROP INDEX CONCURRENTLY cannot run in a transaction block, so it
            # runs on the autocommit connection with no BEGIN around it.
            self._run(statement.text, None)
            return
        super().execute_ddl(statement)

    # --- diagnostics ------------------------------------------------------------------------------

    def _probe_session_liveness(self, db_session: str | None) -> tuple[str, str]:
        if not db_session:
            return ("unknown", "no session identity was recorded for the latest attempt")
        fields = dict(part.split("=", 1) for part in db_session.split(",") if "=" in part)
        if "pid" not in fields or "backend_start" not in fields:
            return ("unknown", "the recorded identity is incomplete, so no match is possible")
        try:
            row = self._db.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE pid = %s AND "
                "to_char(backend_start, 'YYYY-MM-DD\"T\"HH24:MI:SS.US') = %s",
                (int(fields["pid"]), fields["backend_start"]),
            ).fetchone()
        except psycopg.Error as exc:
            return ("unknown", f"pg_stat_activity is not readable: {self.describe_exception(exc)}")
        verdict = "present" if row and row[0] else "absent"
        return (
            verdict,
            "a backend pid alone is reusable, so the recorded backend start time is "
            "matched too; session existence is still not proof that the migration is "
            "executing or holding the lock",
        )

    # --- error classification ---------------------------------------------------------------------

    def classify_exception(self, exc: BaseException) -> OutcomeClass:
        """A SQLSTATE usually means the server answered, so the outcome is definite.

        Two families of code say otherwise and are excluded: class ``08``, the
        connection exceptions, and ``40003`` ``statement_completion_unknown``,
        which is the server stating in as many words that it does not know
        whether the statement completed.  Receiving a code is not by itself an
        outcome.

        Anything without a SQLSTATE -- a closed connection, a driver-side refusal
        this adapter has not justified, an unrecognised exception -- is a
        communication failure, so a commit-capable call that raised it has an
        unknown outcome.
        """
        if isinstance(exc, psycopg.Error):
            sqlstate = getattr(exc, "sqlstate", None)
            if sqlstate:
                code = str(sqlstate)
                if code.startswith(INDEFINITE_SQLSTATE_CLASSES) or code in INDEFINITE_SQLSTATES:
                    return OutcomeClass.COMMUNICATION_FAILURE
                return OutcomeClass.SERVER_REJECTION
            if isinstance(exc, (psycopg.ProgrammingError, psycopg.NotSupportedError)):
                # Raised client-side before anything is sent.
                return OutcomeClass.SERVER_REJECTION
        return OutcomeClass.COMMUNICATION_FAILURE

    def error_code(self, exc: BaseException) -> str | None:
        """The SQLSTATE alone; the diagnostic fields that follow it quote the row."""
        if isinstance(exc, psycopg.Error):
            sqlstate = getattr(exc, "sqlstate", None)
            if sqlstate:
                return f"SQLSTATE {sqlstate}"
        return None


# --- helpers --------------------------------------------------------------------------------------

_KIND_NAMES = {"p": "primary key", "u": "unique key", "f": "foreign key"}

#: SQLSTATE class 08 is the connection-exception class: the server did not answer.
INDEFINITE_SQLSTATE_CLASSES = ("08",)

#: Individual codes that answer without settling the outcome.  40003 is
#: ``statement_completion_unknown``: the server is reporting that it does not
#: know whether the statement completed, which is exactly an unknown outcome.
INDEFINITE_SQLSTATES = frozenset({"40003"})

#: The one-ACTIVE index is a unique partial index: the key is the status column
#: itself, so two ACTIVE rows collide, and the predicate keeps every other row
#: out of the index.  Both halves are required; either alone enforces nothing.
ACTIVE_INDEX_COLUMN = "status"
ACTIVE_INDEX_PREDICATE = "status = 'ACTIVE'"

#: How PostgreSQL renders that predicate back.  17.5 adds the varchar-to-text
#: casts; the uncast form is accepted too so a plainer rendering still passes.
_SUPPORTED_ACTIVE_INDEX_PREDICATES = frozenset(
    md.compact_definition(text, drop_parens=True)
    for text in (ACTIVE_INDEX_PREDICATE, "(status)::text = 'ACTIVE'::text")
)


def _is_concurrent(statement: Statement) -> bool:
    return "CONCURRENTLY" in statement.lead or "CONCURRENTLY" in {
        word.upper() for word in statement.text.split()
    }


def _bind(params: object | None):
    """psycopg reads ``None`` as "do not interpolate", so a literal ``%`` survives."""
    return bind_params(params, empty=None)


_DDL = {
    HISTORY_TABLE: """
        CREATE TABLE {history} (
            seq               integer      NOT NULL,
            migration_id      varchar(200) NOT NULL,
            fingerprint       varchar(80)  NOT NULL,
            first_fingerprint varchar(80)  NOT NULL,
            language          varchar(10)  NOT NULL,
            mode              varchar(12)  NOT NULL,
            status            varchar(8)   NOT NULL,
            attempt           integer,
            started_at        timestamptz  NOT NULL,
            last_attempt_at   timestamptz,
            finished_at       timestamptz,
            runner_host       varchar(255),
            runner_user       varchar(128),
            runner_pid        integer,
            db_session        varchar(200),
            tool_version      varchar(64)  NOT NULL,
            CONSTRAINT m8_history_pk     PRIMARY KEY (migration_id),
            CONSTRAINT m8_history_seq_uq UNIQUE (seq),
            CONSTRAINT m8_history_seq_ck CHECK (seq > 0),
            CONSTRAINT m8_history_lang_ck   CHECK (language IN ('sql','python')),
            CONSTRAINT m8_history_mode_ck   CHECK (mode IN ('atomic','restartable')),
            CONSTRAINT m8_history_status_ck CHECK (status IN ('ACTIVE','SUCCESS')),
            CONSTRAINT m8_history_attempt_ck CHECK (attempt IS NULL OR attempt > 0),
            CONSTRAINT m8_history_active_ck
                CHECK (status <> 'ACTIVE' OR (mode = 'restartable'
                       AND attempt IS NOT NULL AND finished_at IS NULL)),
            CONSTRAINT m8_history_success_ck
                CHECK (status <> 'SUCCESS' OR finished_at IS NOT NULL)
        )
    """,
    ACTIVE_INDEX: """
        CREATE UNIQUE INDEX {index} ON {history} (status) WHERE status = 'ACTIVE'
    """,
    PROGRESS_TABLE: """
        CREATE TABLE {progress} (
            migration_id varchar(200)  NOT NULL,
            prog_key     varchar(128)  NOT NULL,
            prog_value   varchar(4000) NOT NULL,
            updated_at   timestamptz   NOT NULL,
            CONSTRAINT m8_progress_pk PRIMARY KEY (migration_id, prog_key),
            CONSTRAINT m8_progress_fk FOREIGN KEY (migration_id)
                REFERENCES {history} (migration_id),
            CONSTRAINT m8_progress_key_ck   CHECK (char_length(prog_key) BETWEEN 1 AND 128),
            CONSTRAINT m8_progress_value_ck CHECK (char_length(prog_value) >= 1)
        )
    """,
    META_TABLE: """
        CREATE TABLE {meta} (
            meta_key         varchar(30)  NOT NULL,
            layout_version   integer      NOT NULL,
            adapter          varchar(30)  NOT NULL,
            lock_provider    varchar(30)  NOT NULL,
            lock_binding     varchar(200) NOT NULL,
            target_namespace varchar(128) NOT NULL,
            initialized_at   timestamptz  NOT NULL,
            CONSTRAINT m8_meta_pk PRIMARY KEY (meta_key),
            CONSTRAINT m8_meta_singleton_ck CHECK (meta_key = 'singleton')
        )
    """,
}

_EXPECTED_COLUMNS = {
    HISTORY_TABLE: (
        ("seq", "integer", True),
        ("migration_id", "character varying(200)", True),
        ("fingerprint", "character varying(80)", True),
        ("first_fingerprint", "character varying(80)", True),
        ("language", "character varying(10)", True),
        ("mode", "character varying(12)", True),
        ("status", "character varying(8)", True),
        ("attempt", "integer", False),
        ("started_at", "timestamp with time zone", True),
        ("last_attempt_at", "timestamp with time zone", False),
        ("finished_at", "timestamp with time zone", False),
        ("runner_host", "character varying(255)", False),
        ("runner_user", "character varying(128)", False),
        ("runner_pid", "integer", False),
        ("db_session", "character varying(200)", False),
        ("tool_version", "character varying(64)", True),
    ),
    PROGRESS_TABLE: (
        ("migration_id", "character varying(200)", True),
        ("prog_key", "character varying(128)", True),
        ("prog_value", "character varying(4000)", True),
        ("updated_at", "timestamp with time zone", True),
    ),
    META_TABLE: (
        ("meta_key", "character varying(30)", True),
        ("layout_version", "integer", True),
        ("adapter", "character varying(30)", True),
        ("lock_provider", "character varying(30)", True),
        ("lock_binding", "character varying(200)", True),
        ("target_namespace", "character varying(128)", True),
        ("initialized_at", "timestamp with time zone", True),
    ),
}

#: The complete supported constraint shape, one entry per metadata table.  Each
#: check is the canonical form of ``pg_get_constraintdef`` for the DDL above,
#: recorded from PostgreSQL 17.5 on 2026-09-13 rather than guessed.  Semantics
#: are compared, never the database-generated constraint name.
_EXPECTED_CONSTRAINTS: dict[str, md.ExpectedConstraints] = {
    HISTORY_TABLE: {
        "keys": (md.ExpectedKey("p", "migration_id"), md.ExpectedKey("u", "seq")),
        "checks": (
            "check ( ( seq > 0 ) )",
            "check ( ( ( language ) :: text = any ( ( array [ 'sql' :: character varying , "
            "'python' :: character varying ] ) :: text [ ] ) ) )",
            "check ( ( ( mode ) :: text = any ( ( array [ 'atomic' :: character varying , "
            "'restartable' :: character varying ] ) :: text [ ] ) ) )",
            "check ( ( ( status ) :: text = any ( ( array [ 'ACTIVE' :: character varying , "
            "'SUCCESS' :: character varying ] ) :: text [ ] ) ) )",
            "check ( ( ( attempt is null ) or ( attempt > 0 ) ) )",
            "check ( ( ( ( status ) :: text <> 'ACTIVE' :: text ) or ( ( ( mode ) :: text = "
            "'restartable' :: text ) and ( attempt is not null ) and ( finished_at is null ) "
            ") ) )",
            "check ( ( ( ( status ) :: text <> 'SUCCESS' :: text ) or ( finished_at is not "
            "null ) ) )",
        ),
    },
    PROGRESS_TABLE: {
        "keys": (
            md.ExpectedKey("p", "migration_id,prog_key"),
            md.ExpectedKey("f", "migration_id", (HISTORY_TABLE, "migration_id")),
        ),
        "checks": (
            "check ( ( ( char_length ( ( prog_key ) :: text ) >= 1 ) and ( char_length ( ( "
            "prog_key ) :: text ) <= 128 ) ) )",
            "check ( ( char_length ( ( prog_value ) :: text ) >= 1 ) )",
        ),
    },
    META_TABLE: {
        "keys": (md.ExpectedKey("p", "meta_key"),),
        "checks": ("check ( ( ( meta_key ) :: text = 'singleton' :: text ) )",),
    },
}
