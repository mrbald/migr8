"""The Oracle adapter: the specification's primary target (spec Section 12).

One physical ``python-oracledb`` Thin-mode session serves the whole run. It
holds the ``DBMS_LOCK`` namespace lock across every commit, establishes
synchronous commit durability before writing any metadata, and guards atomic
migrations with Oracle's own transaction identity.

Identifiers are restricted to unquoted uppercase Oracle names so namespace
resolution stays precise, and every metadata object is schema-qualified
internally so migration code cannot redirect engine writes by changing name
resolution.
"""

from __future__ import annotations

import logging
import re

import oracledb

from ..config import Config
from ..errors import (
    ConfigError,
    LockNotAcquiredError,
    MetadataDamagedError,
    UnsupportedCapabilityError,
    UsageError,
)
from ..manifest import Mode, RequiredObject
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
    ValidityResult,
)

LOGGER = logging.getLogger("migr8.adapters.oracle")

ADAPTER_NAME = "oracle"

#: Unquoted uppercase Oracle identifier.  The initial adapter deliberately
#: refuses quoted mixed-case names so owner resolution is unambiguous.
IDENTIFIER_RE = re.compile(r"^[A-Z][A-Z0-9_$#]{0,127}$")
QUALIFIED_RE = re.compile(r"^[A-Z][A-Z0-9_$#]{0,127}(\.[A-Z][A-Z0-9_$#]{0,127})?$")

#: Object types whose final validity this adapter can check (spec Section 3.2).
SUPPORTED_REQUIRED_TYPES = frozenset(
    {
        "PROCEDURE",
        "FUNCTION",
        "PACKAGE",
        "PACKAGE BODY",
        "TYPE",
        "TYPE BODY",
        "TRIGGER",
        "VIEW",
    }
)

#: Oracle DDL commits independently, so it is never part of atomic work. The
#: atomic set is the specification's list in Section 5.3.
_POLICY = StatementPolicy(
    atomic=frozenset(
        {
            "SELECT",
            "WITH",
            "INSERT",
            "UPDATE",
            "DELETE",
            "MERGE",
            "LOCK",
            "DECLARE",
            "BEGIN",
        }
    ),
    query=frozenset({"SELECT", "WITH"}),
    procedural=frozenset({"DECLARE", "BEGIN"}),
    ddl=frozenset(
        {
            "CREATE",
            "ALTER",
            "DROP",
            "TRUNCATE",
            "RENAME",
            "COMMENT",
            "ANALYZE",
        }
    ),
    forbidden=frozenset(
        {
            "COMMIT",
            "ROLLBACK",
            "SAVEPOINT",
            "SET",
            "GRANT",
            "REVOKE",
            "AUDIT",
            "NOAUDIT",
            "PURGE",
            "FLASHBACK",
        }
    ),
    allows_plsql=True,
)

#: DBMS_LOCK exclusive mode.
_X_MODE = 6

#: ORA codes that mean the transport or session is gone, so an in-flight
#: commit-capable call has an unknown outcome.  Kept narrow and justified.
TRANSPORT_ORA_CODES = frozenset(
    {
        28,  # your session has been killed
        1012,  # not logged on
        1089,  # immediate shutdown in progress
        1092,  # instance terminated
        3106,  # fatal two-task communication protocol error
        3113,  # end-of-file on communication channel
        3114,  # not connected to ORACLE
        3135,  # connection lost contact
        12152,  # TNS: unable to send break message
        12537,  # TNS: connection closed
        12571,  # TNS: packet writer failure
    }
)


#: python-oracledb errors raised before anything is sent to the server.  An
#: unlisted DPY code stays conservative (unknown outcome) on purpose.
CLIENT_SIDE_DPY_CODES = frozenset(
    {
        "DPY-2002",  # cursor is not open
        "DPY-2005",  # invalid dictionary value
        "DPY-2006",  # invalid number of array elements
        "DPY-2008",  # invalid parameter type / value
        "DPY-2009",  # invalid number of positional parameters
        "DPY-2010",  # invalid keyword parameter
        "DPY-3002",  # percent-style placeholders are not supported
        "DPY-3003",  # named bind placeholders are required
        "DPY-3004",  # data type is not supported
        "DPY-3005",  # operation is not supported in this mode
        "DPY-4008",  # no bind placeholder with the given name was found
        "DPY-4009",  # missing positional bind variable value
        "DPY-4010",  # a bind variable replacement value was not provided
    }
)


def _require_identifier(value: str, what: str) -> str:
    upper = value.upper()
    if not IDENTIFIER_RE.match(upper) or upper != value:
        raise ConfigError(
            f"{what} must be an unquoted uppercase Oracle identifier matching "
            f"[A-Z][A-Z0-9_$#]*; got {value!r}. Quoted mixed-case identifiers require "
            "explicit adapter support."
        )
    return upper


class OracleAdapter(Adapter):
    name = ADAPTER_NAME
    driver_error = oracledb.Error

    # --- dialect ----------------------------------------------------------------------------------

    paramstyle = "named"
    now_expression = "SYSTIMESTAMP"
    #: Oracle has no ON CONFLICT; the shared progress upsert uses MERGE.
    supports_on_conflict = False
    one_row_table = "dual"
    #: Oracle folds unquoted identifiers to upper case, so quoted metadata names
    #: must be upper case too. That is also why the reserved word MODE needs no
    #: special handling: the folded, quoted form is exactly "MODE".
    identifier_case = "upper"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        if not config.dsn:
            raise ConfigError("the oracle adapter requires database.dsn")
        if not config.user:
            raise ConfigError("the oracle adapter requires database.user")
        self._user = _require_identifier(config.user, "database.user")
        schema = config.target_schema or config.user
        self._schema = _require_identifier(schema, "database.target_schema")

        options = config.options.get("oracle", {})
        if not isinstance(options, dict):
            raise ConfigError("[oracle] must be a table")
        unknown = sorted(set(options) - {"ddl_lock_timeout_seconds", "allow_thick_mode"})
        if unknown:
            raise ConfigError(f"[oracle] has unknown keys: {', '.join(unknown)}")
        self._ddl_lock_timeout = int(options.get("ddl_lock_timeout_seconds", 30))
        if self._ddl_lock_timeout < 0:
            raise ConfigError("oracle.ddl_lock_timeout_seconds must not be negative")
        self._allow_thick = bool(options.get("allow_thick_mode", False))

        lock = config.lock
        if lock.provider != "dbms_lock":
            raise ConfigError(
                f'the oracle adapter requires lock.provider = "dbms_lock", got '
                f"{lock.provider!r}; the engine will not fall back to an ineffective lock"
            )
        if lock.id is None:
            raise ConfigError("the oracle adapter requires an explicit lock.id")
        self._lock_id = lock.id
        package = lock.package or "SYS.DBMS_LOCK"
        if not QUALIFIED_RE.match(package.upper()) or package.upper() != package:
            raise ConfigError(
                f"lock.package must be an unquoted uppercase [schema.]package name, got {package!r}"
            )
        self._lock_package = package
        self._lock_timeout = min(lock.timeout_seconds, 32767)

        self.metadata_schema = self._schema
        self._conn: oracledb.Connection | None = None
        self._lock_held = False
        self._banner = "not connected"
        self._session_identity: str | None = None
        self._commit_wait_note = "not established"

    # --- reporting --------------------------------------------------------------------------------

    def capabilities(self) -> Capabilities:
        return Capabilities(
            adapter=self.name,
            transactional_ddl_in_atomic=False,
            oracle_style_required_objects=True,
            database_backed_lock=True,
            transaction_identity_tripwire=True,
            notes=(
                "python-oracledb Thin mode; thick mode refused unless explicitly enabled.",
                "COMMIT_WAIT = FORCE_WAIT is set before the lock and any metadata write. "
                f"Read-back: {self._commit_wait_note}",
                "DDL is restartable only. A plain CREATE that fails on repetition is not "
                "made convergent by its mode label.",
                "Atomic migrations may not run DDL; Oracle DDL commits independently.",
                f"python-oracledb {oracledb.__version__}; server {self._banner}.",
            ),
        )

    def server_description(self) -> str:
        return self._banner

    def normalized_namespace(self) -> str:
        return self._schema

    def lock_binding(self) -> str:
        # The binding is the user-lock id: DBMS_LOCK user locks are global by id,
        # so a documented wrapper package is an access path, not a new namespace.
        return f"dbms_lock:{self._lock_id}"

    def session_identity(self) -> str | None:
        return self._session_identity

    # --- lifecycle --------------------------------------------------------------------------------

    def connect(self) -> None:
        password = self.config.password()
        try:
            self._conn = oracledb.connect(
                user=self._user,
                password=password,
                dsn=self.config.dsn,
                # Transparent reconnect and replay are not used by this runner.
                # Thin mode implements neither; see the thick-mode check below.
            )
        except oracledb.Error as exc:
            # The driver's connection message repeats the DSN, so it is reported
            # by code and the operator is pointed at the settings instead.
            raise UsageError(
                f"cannot connect to Oracle: {self.describe_exception(exc)}. Check "
                "database.dsn, database.user and the MIGR8_PASSWORD environment variable."
            ) from exc
        if not self._conn.thin and not self._allow_thick:
            self._conn.close()
            self._conn = None
            raise UnsupportedCapabilityError(
                "this adapter is tested in python-oracledb Thin mode only; set "
                "oracle.allow_thick_mode = true only after running the suite in thick mode"
            )
        self._conn.autocommit = False
        self._configure_session()
        self._banner = self._read_banner()
        self._session_identity = self._read_session_identity()

    def _configure_session(self) -> None:
        cursor = self._cursor()
        # Durability first: no metadata write or lock request precedes it.
        try:
            cursor.execute("ALTER SESSION SET COMMIT_WAIT = FORCE_WAIT")
            cursor.execute("ALTER SESSION SET COMMIT_LOGGING = IMMEDIATE")
        except oracledb.Error as exc:
            raise UsageError(
                f"cannot establish synchronous commit durability: {exc}. There is no setting "
                "to weaken required commit durability, so setup fails."
            ) from exc
        self._commit_wait_note = self._probe_commit_wait()
        try:
            cursor.execute(f"ALTER SESSION SET DDL_LOCK_TIMEOUT = {self._ddl_lock_timeout}")
        except oracledb.Error as exc:
            raise UsageError(f"cannot set DDL_LOCK_TIMEOUT: {exc}") from exc
        if self._schema != self._user:
            # CURRENT_SCHEMA changes name resolution only; it grants nothing, and
            # engine writes stay schema-qualified regardless.
            try:
                cursor.execute(f'ALTER SESSION SET CURRENT_SCHEMA = "{self._schema}"')
            except oracledb.Error as exc:
                raise UsageError(f"cannot set CURRENT_SCHEMA to {self._schema}: {exc}") from exc
        self._verify_namespace_access()

    def _probe_commit_wait(self) -> str:
        """Attempt a read-back and report honestly when it is unavailable."""
        try:
            row = (
                self._cursor()
                .execute("SELECT value FROM v$parameter WHERE name = 'commit_wait'")
                .fetchone()
            )
        except oracledb.Error as exc:
            code = getattr(exc.args[0], "code", 0) if exc.args else 0
            return (
                "NOT VERIFIED: v$parameter is not readable with the current privileges "
                f"(ORA-{code:05d}); ALTER SESSION succeeded but no read-back was possible"
            )
        instance_value = row[0] if row else None
        return (
            "NOT VERIFIED at session level: Oracle exposes no session-level read-back for "
            f"COMMIT_WAIT. Instance value is {instance_value!r}; the session setting "
            "overrides it and ALTER SESSION succeeded"
        )

    def _verify_namespace_access(self) -> None:
        """Confirm exact-owner dictionary probes work before doing anything else."""
        try:
            self._cursor().execute(
                "SELECT COUNT(*) FROM all_objects WHERE owner = :owner", owner=self._schema
            ).fetchone()
        except oracledb.Error as exc:
            raise UsageError(
                f"cannot inspect objects owned by {self._schema} through ALL_OBJECTS: {exc}. "
                "An inaccessible probe is an error, not evidence that the namespace is empty."
            ) from exc

    def _read_banner(self) -> str:
        assert self._conn is not None
        cursor = self._cursor()
        try:
            row = cursor.execute("SELECT banner_full FROM v$version").fetchone()
            if row and row[0]:
                return f"{row[0]} [thin={self._conn.thin}]"
        except oracledb.Error:
            pass
        return (
            f"Oracle Database {self._conn.version} "
            f"[thin={self._conn.thin}, driver python-oracledb {oracledb.__version__}]"
        )

    def _read_session_identity(self) -> str:
        cursor = self._cursor()
        base = cursor.execute(
            "SELECT sys_context('USERENV','SID'), sys_context('USERENV','SESSIONID'), "
            "sys_context('USERENV','INSTANCE_NAME'), sys_context('USERENV','SERVER_HOST') "
            "FROM dual"
        ).fetchone()
        sid, audsid, instance, host = base
        serial = None
        try:
            row = cursor.execute(
                "SELECT serial# FROM v$session WHERE sid = sys_context('USERENV','SID')"
            ).fetchone()
            serial = row[0] if row else None
        except oracledb.Error:
            serial = None
        suffix = f",serial={serial}" if serial is not None else ",serial=unavailable"
        return f"instance={instance}@{host},sid={sid},audsid={audsid}{suffix}"

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            # Teardown asks the driver directly: a failure while closing must not
            # be reclassified as an unknown migration outcome.
            if self._has_open_transaction():
                self._conn.rollback()
            self.release_lock()
        finally:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def discard(self) -> None:
        """Drop the session without issuing any further SQL.

        No rollback, no lock release: releasing a lock through another connection
        after losing the owning session is never correct, and the server session
        may still be executing the submitted call.
        """
        if self._conn is None:
            return
        self._lock_held = False
        try:
            self._conn.close()
        except oracledb.Error:
            pass
        finally:
            self._conn = None

    # --- namespace lock ---------------------------------------------------------------------------

    def acquire_lock(self) -> None:
        if self._lock_held:
            raise UsageError("the namespace lock is already held by this run")
        cursor = self._cursor()
        result = cursor.var(int)
        try:
            cursor.execute(
                f"BEGIN :result := {self._lock_package}.REQUEST("
                "id => :lock_id, lockmode => :mode, timeout => :timeout, "
                "release_on_commit => FALSE); END;",
                result=result,
                lock_id=self._lock_id,
                mode=_X_MODE,
                timeout=self._lock_timeout,
            )
        except oracledb.Error as exc:
            raise UsageError(
                f"cannot call {self._lock_package}.REQUEST: {exc}. Required grants must be "
                "established before running migrations; the engine does not fall back to an "
                "ineffective lock."
            ) from exc
        code = int(result.getvalue())
        if code == 0:
            self._lock_held = True
            return
        if code in (1, 2):
            reason = "timeout" if code == 1 else "deadlock"
            raise LockNotAcquiredError(
                f"migration lock {self._lock_id} not acquired ({reason}) after "
                f"{self._lock_timeout}s. Another runner may hold it, or a previous server "
                "session may not have ended. Contention is not a lock-test failure, and no "
                "upper bound follows from dead-connection detection settings."
            )
        if code == 3:
            raise ConfigError(
                f"{self._lock_package}.REQUEST rejected the parameters for lock "
                f"{self._lock_id} (return code 3)"
            )
        if code == 5:
            raise ConfigError(
                f"{self._lock_package}.REQUEST reported an illegal lock handle for "
                f"{self._lock_id} (return code 5)"
            )
        if code == 4:
            raise UsageError(
                f"lock {self._lock_id} is already owned by this session (return code 4); "
                "the lock is acquired exactly once per run"
            )
        raise UsageError(f"unexpected DBMS_LOCK.REQUEST return code {code}")

    def release_lock(self) -> None:
        if not self._lock_held or self._conn is None:
            return
        cursor = self._cursor()
        result = cursor.var(int)
        try:
            cursor.execute(
                f"BEGIN :result := {self._lock_package}.RELEASE(id => :lock_id); END;",
                result=result,
                lock_id=self._lock_id,
            )
        except oracledb.Error as exc:
            LOGGER.warning("releasing migration lock %s failed: %s", self._lock_id, exc)
        finally:
            self._lock_held = False

    # --- internals --------------------------------------------------------------------------------

    def _cursor(self) -> oracledb.Cursor:
        if self._conn is None:
            raise UsageError("the Oracle session is not connected")
        return self._conn.cursor()

    def _q(self, name: str) -> str:
        """Schema-qualified metadata object name.

        Delegates to the shared renderer so the string-built and the
        dictionary-probe paths cannot disagree about which object is meant.
        """
        return self.metadata_name(name)

    # --- engine-owned SQL path, transactions and admission policy ---------------------------------

    def _metadata_execute(self, sql: str, params) -> int:
        cursor = self._cursor()
        cursor.execute(sql, dict(params))
        return max(cursor.rowcount, 0)

    def _metadata_query(self, sql: str, params) -> list[tuple]:
        cursor = self._cursor()
        cursor.execute(sql, dict(params))
        return [tuple(row) for row in cursor.fetchall()]

    def _do_begin(self) -> None:
        """Oracle starts a transaction implicitly on the first write."""
        return

    def _do_commit(self) -> None:
        assert self._conn is not None
        self._conn.commit()

    def _do_rollback(self) -> None:
        assert self._conn is not None
        self._conn.rollback()

    @property
    def statement_policy(self) -> StatementPolicy:
        return _POLICY

    def _extra_statement_checks(self, statement: Statement, *, mode: Mode, in_batch: bool) -> None:
        """ALTER SESSION and ALTER SYSTEM change state the engine owns."""
        lead = statement.lead
        if len(lead) >= 2 and lead[0] == "ALTER" and lead[1] in ("SESSION", "SYSTEM"):
            raise UsageError(
                f"{' '.join(lead[:2])} changes session or system state and is not "
                "admitted through the facade"
            )

    # --- metadata ---------------------------------------------------------------------------------

    def _objects_present(self) -> set[str]:
        """Oracle stores these folded to upper case; fold back to logical names."""
        names = [n.upper() for n in md.CREATION_ORDER]
        placeholders = ", ".join(f":n{i}" for i in range(len(names)))
        binds = {f"n{i}": name for i, name in enumerate(names)}
        rows = (
            self._cursor()
            .execute(
                "SELECT object_name FROM all_objects WHERE owner = :owner AND object_type IN "
                f"('TABLE','INDEX') AND object_name IN ({placeholders})",
                owner=self._schema,
                **binds,
            )
            .fetchall()
        )
        return {row[0].lower() for row in rows}

    def _definition_problems(self, present: set[str]) -> list[str]:
        problems: list[str] = []
        for table, expected in _EXPECTED_COLUMNS.items():
            if table not in present:
                continue
            rows = (
                self._cursor()
                .execute(
                    "SELECT column_name, data_type, char_length, char_used, nullable, "
                    "data_precision, data_scale FROM all_tab_columns "
                    "WHERE owner = :owner AND table_name = :name ORDER BY column_id",
                    owner=self._schema,
                    name=table.upper(),
                )
                .fetchall()
            )
            actual = tuple(
                (
                    row[0],
                    row[1],
                    int(row[2] or 0),
                    row[3],
                    row[4],
                    int(row[5]) if row[5] is not None else None,
                    int(row[6]) if row[6] is not None else None,
                )
                for row in rows
            )
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
            rows = (
                self._cursor()
                .execute(
                    "SELECT c.constraint_type, c.status, c.validated, c.search_condition, "
                    "  (SELECT LISTAGG(cc.column_name, ',') WITHIN GROUP (ORDER BY cc.position) "
                    "     FROM all_cons_columns cc "
                    "    WHERE cc.owner = c.owner AND cc.constraint_name = c.constraint_name), "
                    "  c.r_owner, "
                    "  (SELECT r.table_name FROM all_constraints r "
                    "    WHERE r.owner = c.r_owner AND r.constraint_name = c.r_constraint_name), "
                    "  (SELECT LISTAGG(rc.column_name, ',') WITHIN GROUP (ORDER BY rc.position) "
                    "     FROM all_cons_columns rc "
                    "    WHERE rc.owner = c.r_owner AND rc.constraint_name = c.r_constraint_name) "
                    "FROM all_constraints c "
                    "WHERE c.owner = :owner AND c.table_name = :name",
                    owner=self._schema,
                    name=table.upper(),
                )
                .fetchall()
            )
            # Compare normalised semantics, not database-generated names.
            found_keys: dict[tuple[str, str], list[tuple]] = {}
            for row in rows:
                if row[0] in ("P", "U", "R"):
                    found_keys.setdefault((row[0], (row[4] or "").upper()), []).append(row)
            for expected_key in expected["keys"]:
                kind, columns, references = expected_key
                matches = found_keys.get((kind, columns))
                if not matches:
                    problems.append(
                        f"{table} is missing a {_KIND_NAMES[kind]} on ({columns}); found "
                        f"{sorted(found_keys)}"
                    )
                    continue
                for (
                    _type,
                    status,
                    validated,
                    _condition,
                    _columns,
                    target_owner,
                    target,
                    target_columns,
                ) in matches:
                    # A key the server is not enforcing does not hold the layout up.
                    if status != "ENABLED" or validated != "VALIDATED":
                        problems.append(
                            f"{table} {_KIND_NAMES[kind]} on ({columns}) is "
                            f"{status}/{validated}, not ENABLED/VALIDATED"
                        )
                    if references is None:
                        continue
                    wanted_table, wanted_columns = references
                    # The owner is part of the target identity.  Oracle resolves
                    # the referenced constraint through R_OWNER, so a same-named
                    # history table in another schema would otherwise satisfy
                    # this check while linking progress to that schema's history.
                    expected_target = (
                        f"{self.metadata_schema}."
                        f"{self._physical_object_name(wanted_table).upper()}({wanted_columns})"
                    )
                    actual = (
                        f"{target_owner}.{target}({target_columns or ''})" if target else "nothing"
                    )
                    if actual != expected_target:
                        problems.append(
                            f"{table} {_KIND_NAMES[kind]} on ({columns}) references {actual}, "
                            f"not {expected_target}"
                        )
            problems.extend(
                md.check_problems(
                    table,
                    (
                        (
                            row[3],
                            row[1] == "ENABLED" and row[2] == "VALIDATED",
                            f"{row[1]}/{row[2]}, not ENABLED/VALIDATED",
                        )
                        for row in rows
                        if row[0] == "C"
                    ),
                    expected["checks"],
                    fold=self._fold,
                )
            )
        return problems

    def _index_problems(self, present: set[str]) -> list[str]:
        if ACTIVE_INDEX not in present:
            return []
        problems: list[str] = []
        row = (
            self._cursor()
            .execute(
                "SELECT uniqueness, status, funcidx_status, table_name FROM all_indexes "
                "WHERE owner = :owner AND index_name = :name",
                owner=self._schema,
                name=ACTIVE_INDEX.upper(),
            )
            .fetchone()
        )
        if row is None:
            return [f"{ACTIVE_INDEX} is not visible in ALL_INDEXES"]
        uniqueness, status, funcidx, table_name = row
        if table_name != HISTORY_TABLE.upper():
            problems.append(f"{ACTIVE_INDEX} is on {table_name}, not {HISTORY_TABLE}")
        if uniqueness != "UNIQUE":
            problems.append(f"{ACTIVE_INDEX} is {uniqueness}, not UNIQUE")
        if status != "VALID":
            problems.append(f"{ACTIVE_INDEX} status is {status}, not VALID")
        if funcidx not in (None, "ENABLED"):
            problems.append(f"{ACTIVE_INDEX} function-based status is {funcidx}, not ENABLED")
        expression = (
            self._cursor()
            .execute(
                "SELECT column_expression FROM all_ind_expressions "
                "WHERE index_owner = :owner AND index_name = :name ORDER BY column_position",
                owner=self._schema,
                name=ACTIVE_INDEX.upper(),
            )
            .fetchall()
        )
        rendered = " ".join(
            (value[0].read() if hasattr(value[0], "read") else str(value[0]))
            for value in expression
        )
        column_count = (
            self._cursor()
            .execute(
                "SELECT COUNT(*) FROM all_ind_columns "
                "WHERE index_owner = :owner AND index_name = :name",
                owner=self._schema,
                name=ACTIVE_INDEX.upper(),
            )
            .fetchone()[0]
        )
        if int(column_count) != 1 or len(expression) != 1:
            problems.append(
                f"{ACTIVE_INDEX} indexes {column_count} column(s) over {len(expression)} "
                "expression(s); the supported layout indexes exactly one expression"
            )
        if md.compact_definition(rendered) not in _SUPPORTED_ACTIVE_INDEX_EXPRESSIONS:
            problems.append(
                f"{ACTIVE_INDEX} does not carry the supported one-ACTIVE expression "
                f"({ACTIVE_INDEX_EXPRESSION}): {rendered!r}. Only a constant key makes two "
                "ACTIVE rows collide; an expression that merely mentions the column and the "
                "value can yield a distinct key per row and enforce nothing."
            )
        return problems

    def _create_metadata_object(self, name: str) -> None:
        """Oracle DDL commits independently, so the CREATE is its own durable step.

        Being durable on its own makes it commit-capable, so it goes through the
        operation guard: a lost reply here leaves the object's existence unknown
        and must stop the run rather than fall through to the next object.
        """
        sql = _DDL[name].format(
            history=self._q(HISTORY_TABLE),
            progress=self._q(PROGRESS_TABLE),
            meta=self._q(META_TABLE),
            index=self._q(ACTIVE_INDEX),
        )
        self.guarded(
            lambda: self._cursor().execute(sql),
            operation=f"create metadata object {name}",
            phase=Boundary.METADATA_OBJECT_CREATED.value,
            commit_capable=True,
        )

    def _read_snapshot(self, consistent: bool) -> Snapshot:
        """Read history, progress and the marker in one statement.

        A single SQL statement is read-consistent in Oracle, so no transaction is
        needed.  A ``SET TRANSACTION READ ONLY`` block would be the alternative,
        but Oracle raises ORA-01466 when such a snapshot reads a table whose
        definition changed in the same second -- which happens on every run that
        has just created metadata or executed migration DDL, and would make
        ``status`` fail during exactly the long migration it is meant to observe.
        """
        rows = (
            self._cursor()
            .execute(
                _SNAPSHOT_SQL.format(
                    history=self._q(HISTORY_TABLE),
                    progress=self._q(PROGRESS_TABLE),
                    meta=self._q(META_TABLE),
                )
            )
            .fetchall()
        )

        history: list = []
        progress: list = []
        meta = None
        meta_rows = 0
        for row in rows:
            kind = row[0]
            payload = [_lob(value) for value in row[2:]]
            if kind == "H":
                history.append(
                    md.history_row(
                        (
                            int(row[1]),
                            payload[0],
                            payload[1],
                            payload[2],
                            payload[3],
                            payload[4],
                            payload[5],
                            None if payload[6] is None else int(payload[6]),
                            payload[7],
                            payload[8],
                            payload[9],
                            payload[10],
                            payload[11],
                            None if payload[12] is None else int(payload[12]),
                            payload[13],
                            payload[14],
                        ),
                        md.parse_iso_timestamp,
                    )
                )
            elif kind == "P":
                progress.append(
                    md.progress_row(
                        (payload[0], payload[1], payload[2], payload[3]),
                        md.parse_iso_timestamp,
                    )
                )
            else:
                meta_rows += 1
                if payload[0] == META_SINGLETON_KEY:
                    meta = md.meta_row(
                        (
                            payload[0],
                            int(payload[1]),
                            payload[2],
                            payload[3],
                            payload[4],
                            payload[5],
                            payload[6],
                        ),
                        md.parse_iso_timestamp,
                    )
        if meta_rows > 1:
            raise MetadataDamagedError(
                f"{META_TABLE} holds {meta_rows} rows; exactly one is expected"
            )
        return Snapshot(history=tuple(history), progress=tuple(progress), meta=meta)

    # --- transaction control ----------------------------------------------------------------------

    def _has_open_transaction(self) -> bool:
        """Oracle assigns a local transaction id only once a write happens.

        That makes this an accurate answer to "is there uncommitted work", which
        is what ``ctx.ddl()``'s precondition and the post-return check need. It is
        *not* an answer to "am I inside an engine-opened batch"; the base tracks
        that separately, because a batch's first write is often the checkpoint.
        """
        return self._read_transaction_identity() is not None

    # --- transaction identity ---------------------------------------------------------------------

    def _establish_transaction_identity(self) -> str | None:
        cursor = self._cursor()
        holder = cursor.var(str)
        cursor.execute(
            "BEGIN :xid := SYS.DBMS_TRANSACTION.LOCAL_TRANSACTION_ID(TRUE); END;", xid=holder
        )
        value = holder.getvalue()
        if not value:
            raise UsageError(
                "Oracle did not return a local transaction id when establishing atomic "
                "transaction identity"
            )
        return value

    def _read_transaction_identity(self) -> str | None:
        cursor = self._cursor()
        holder = cursor.var(str)
        cursor.execute(
            "BEGIN :xid := SYS.DBMS_TRANSACTION.LOCAL_TRANSACTION_ID(FALSE); END;", xid=holder
        )
        return holder.getvalue() or None

    # --- admission --------------------------------------------------------------------------------

    def admit_required_objects(self, required: tuple[RequiredObject, ...]) -> None:
        """Accept only declaration types this adapter can actually resolve.

        Oracle is the one adapter that implements final-validity checking, so it
        overrides the base's blanket refusal. The name is validated as an
        identifier here rather than at use, so a bad declaration fails in
        preflight instead of mid-run.
        """
        for obj in required:
            if obj.type not in SUPPORTED_REQUIRED_TYPES:
                raise UnsupportedCapabilityError(
                    f"require_valid type {obj.type!r} is not supported by the oracle "
                    f"adapter; supported types are "
                    f"{', '.join(sorted(SUPPORTED_REQUIRED_TYPES))}"
                )
            _require_identifier(obj.name, f"require_valid name {obj.name!r}")

    # --- execution --------------------------------------------------------------------------------

    def _run(self, text: str, params: object | None):
        cursor = self._cursor()
        cursor.execute(text, params or {})
        return cursor

    def executemany(self, statement: Statement, parameter_sets: list[object]) -> int:
        cursor = self._cursor()
        # Raise on the first error rather than silently collecting row errors.
        cursor.executemany(
            statement.text, parameter_sets, batcherrors=False, arraydmlrowcounts=False
        )
        return max(cursor.rowcount, 0)

    def execute_ddl(self, statement: Statement) -> None:
        """Oracle DDL commits independently, so it owns no engine transaction."""
        self._run(statement.text, None)

    # --- final validity ---------------------------------------------------------------------------

    def _check_required_objects(self, required: tuple[RequiredObject, ...]) -> ValidityResult:
        """Read-only check of every declaration (spec Section 6).

        The verdict for the whole declared set comes from one statement, so it is
        a consistent read.  No compilation is performed, no object is touched,
        and compiler warnings are reported without failing.  The set is never
        empty here: the base returns before making a database call for that.
        """
        failures: list[str] = []
        warnings: list[str] = []
        branches = " UNION ALL ".join(
            f"SELECT :t{i} AS otype, :n{i} AS oname FROM dual" for i in range(len(required))
        )
        binds: dict[str, object] = {"owner": self._schema}
        for index, obj in enumerate(required):
            binds[f"t{index}"] = obj.type
            binds[f"n{index}"] = obj.name
        try:
            rows = (
                self._cursor().execute(_REQUIRED_SQL.format(branches=branches), **binds).fetchall()
            )
        except oracledb.Error as exc:
            return ValidityResult(
                failures=(
                    "required-object inspection failed: "
                    f"{self.describe_exception(exc)}. An inaccessible probe is an "
                    "error, not evidence that the object is absent.",
                )
            )
        for otype, oname, status, exact_count, other_types, hard, soft in rows:
            qualified = f"{otype} {self._schema}.{oname}"
            if exact_count == 0:
                others = _lob(other_types)
                if others:
                    failures.append(
                        f"{qualified} is missing; an object of that name exists with "
                        f"type(s) {others}"
                    )
                else:
                    failures.append(
                        f"{qualified} does not exist or is not visible with the current privileges"
                    )
                continue
            if exact_count > 1:
                failures.append(
                    f"{qualified} is ambiguous: {exact_count} matching dictionary rows in "
                    "the supported namespace"
                )
                continue
            if soft:
                warnings.append(
                    f"{qualified} has {soft} compiler warning(s): "
                    + self._error_text(oname, otype, attribute="WARNING")
                )
            if status != "VALID":
                failures.append(
                    f"{qualified} has STATUS = {status!r}, not 'VALID'. "
                    + self._error_text(oname, otype, attribute="ERROR")
                    + " Completion performs no automatic compilation."
                )
                continue
            if hard:
                failures.append(
                    f"{qualified} has compiler errors despite STATUS = 'VALID': "
                    + self._error_text(oname, otype, attribute="ERROR")
                )
        return ValidityResult(failures=tuple(failures), warnings=tuple(warnings))

    def _error_text(self, name: str, obj_type: str, *, attribute: str) -> str:
        """Fetch diagnostic text for an already-decided verdict."""
        try:
            rows = (
                self._cursor()
                .execute(
                    "SELECT line, position, text FROM all_errors WHERE owner = :owner AND "
                    "name = :name AND type = :type AND attribute = :attribute "
                    "ORDER BY sequence FETCH FIRST 3 ROWS ONLY",
                    owner=self._schema,
                    name=name,
                    type=obj_type,
                    attribute=attribute,
                )
                .fetchall()
            )
        except oracledb.Error:
            return "(compiler messages are not readable with the current privileges)"
        if not rows:
            return "(no compiler message recorded)"
        return "; ".join(f"line {row[0]}:{row[1]} {str(_lob(row[2])).strip()}" for row in rows)

    # --- diagnostics ------------------------------------------------------------------------------

    def _probe_session_liveness(self, db_session: str | None) -> tuple[str, str]:
        if not db_session:
            return ("unknown", "no session identity was recorded for the latest attempt")
        fields = dict(part.split("=", 1) for part in db_session.split(",") if "=" in part)
        if fields.get("serial") in (None, "unavailable"):
            return (
                "unknown",
                "the recorded identity has no serial number, so a session match cannot be "
                "established; a reusable SID alone is not an identity",
            )
        try:
            row = (
                self._cursor()
                .execute(
                    "SELECT COUNT(*) FROM v$session WHERE sid = :sid AND serial# = :serial",
                    sid=int(fields["sid"]),
                    serial=int(fields["serial"]),
                )
                .fetchone()
            )
        except oracledb.Error as exc:
            return (
                "unknown",
                "v$session is not readable with the current privileges: "
                f"{self.describe_exception(exc)}",
            )
        verdict = "present" if row and row[0] else "absent"
        return (
            verdict,
            "session existence is not proof that the migration is executing or holding the "
            "lock; the instance is not cross-checked by this probe",
        )

    # --- error classification ---------------------------------------------------------------------

    def classify_exception(self, exc: BaseException) -> OutcomeClass:
        """Classify a driver exception conservatively.

        ``SERVER_REJECTION`` means the outcome is *definite* and the operation
        had no durable effect.  Two narrow cases qualify:

        * an ORA error number returned by the server that is not in the small
          transport-failure list, and
        * a driver error from :data:`CLIENT_SIDE_DPY_CODES`, which the driver
          raises before submitting anything.

        Everything else -- a driver error this adapter has not justified, an
        unrecognised exception, or a transport ORA code -- is a communication
        failure, so a commit-capable call that raised it has an unknown outcome.
        """
        if isinstance(exc, oracledb.Error) and exc.args:
            error = exc.args[0]
            code = getattr(error, "code", 0) or 0
            full_code = getattr(error, "full_code", "") or ""
            if code:
                return (
                    OutcomeClass.COMMUNICATION_FAILURE
                    if code in TRANSPORT_ORA_CODES
                    else OutcomeClass.SERVER_REJECTION
                )
            if full_code in CLIENT_SIDE_DPY_CODES:
                return OutcomeClass.SERVER_REJECTION
        return OutcomeClass.COMMUNICATION_FAILURE

    def error_code(self, exc: BaseException) -> str | None:
        """The ``ORA-`` or ``DPY-`` code alone, without the message that follows it."""
        if isinstance(exc, oracledb.Error) and exc.args:
            return getattr(exc.args[0], "full_code", None) or None
        return None


# --- helpers --------------------------------------------------------------------------------------

_KIND_NAMES = {"P": "primary key", "U": "unique key", "R": "foreign key"}

#: The expression the one-ACTIVE index is created with.  A constant key is the
#: whole point: two ACTIVE rows must collide on it.
ACTIVE_INDEX_EXPRESSION = "CASE WHEN status = 'ACTIVE' THEN 1 END"

#: How the supported expression comes back from ALL_IND_EXPRESSIONS.  Oracle 23ai
#: rewrites the searched CASE above into the simple form, so both are accepted:
#: they are the same expression, and a future release need not keep rewriting.
_SUPPORTED_ACTIVE_INDEX_EXPRESSIONS = frozenset(
    md.compact_definition(text)
    for text in (
        ACTIVE_INDEX_EXPRESSION,
        "CASE \"STATUS\" WHEN 'ACTIVE' THEN 1 END",
    )
)


def _lob(value):
    """Materialise a CLOB column value; other values pass through."""
    return value.read() if hasattr(value, "read") else value


_TS = "TO_CHAR({column}, 'YYYY-MM-DD\"T\"HH24:MI:SS.FF6TZH:TZM')"

#: One statement, so the whole metadata read is consistent without a transaction.
_SNAPSHOT_SQL = (
    """
SELECT 'H' AS kind, seq AS ord,
       migration_id AS c1, fingerprint AS c2, first_fingerprint AS c3,
       language AS c4, "MODE" AS c5, status AS c6, TO_CHAR(attempt) AS c7,
       {ts_started} AS c8, {ts_last} AS c9, {ts_finished} AS c10,
       runner_host AS c11, runner_user AS c12, TO_CHAR(runner_pid) AS c13,
       db_session AS c14, tool_version AS c15
  FROM {history}
UNION ALL
SELECT 'P', 0,
       migration_id, prog_key, prog_value, {ts_updated}, NULL, NULL, NULL,
       NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
  FROM {progress}
UNION ALL
SELECT 'M', 0,
       meta_key, TO_CHAR(layout_version), adapter, lock_provider, lock_binding,
       target_namespace, {ts_initialized}, NULL, NULL, NULL, NULL, NULL, NULL,
       NULL, NULL
  FROM {meta}
ORDER BY 1, 2
""".replace("{ts_started}", _TS.format(column="started_at"))
    .replace("{ts_last}", _TS.format(column="last_attempt_at"))
    .replace("{ts_finished}", _TS.format(column="finished_at"))
    .replace("{ts_updated}", _TS.format(column="updated_at"))
    .replace("{ts_initialized}", _TS.format(column="initialized_at"))
)

#: One statement for the whole declared set, so the verdict is a consistent read.
_REQUIRED_SQL = """
WITH req AS ({branches})
SELECT r.otype,
       r.oname,
       (SELECT MAX(a.status) FROM all_objects a
         WHERE a.owner = :owner AND a.object_name = r.oname
           AND a.object_type = r.otype) AS object_status,
       (SELECT COUNT(*) FROM all_objects a
         WHERE a.owner = :owner AND a.object_name = r.oname
           AND a.object_type = r.otype) AS exact_count,
       (SELECT LISTAGG(t, ',') FROM
          (SELECT DISTINCT a2.object_type AS t FROM all_objects a2
            WHERE a2.owner = :owner AND a2.object_name = r.oname)) AS other_types,
       (SELECT COUNT(*) FROM all_errors e
         WHERE e.owner = :owner AND e.name = r.oname AND e.type = r.otype
           AND e.attribute = 'ERROR') AS hard_errors,
       (SELECT COUNT(*) FROM all_errors e
         WHERE e.owner = :owner AND e.name = r.oname AND e.type = r.otype
           AND e.attribute <> 'ERROR') AS soft_errors
  FROM req r
"""


_DDL = {
    HISTORY_TABLE: """
        CREATE TABLE {history} (
            seq               NUMBER(10)                  NOT NULL,
            migration_id      VARCHAR2(200 CHAR)          NOT NULL,
            fingerprint       VARCHAR2(80 CHAR)           NOT NULL,
            first_fingerprint VARCHAR2(80 CHAR)           NOT NULL,
            language          VARCHAR2(10 CHAR)           NOT NULL,
            "MODE"            VARCHAR2(12 CHAR)           NOT NULL,
            status            VARCHAR2(8 CHAR)            NOT NULL,
            attempt           NUMBER(10),
            started_at        TIMESTAMP(6) WITH TIME ZONE NOT NULL,
            last_attempt_at   TIMESTAMP(6) WITH TIME ZONE,
            finished_at       TIMESTAMP(6) WITH TIME ZONE,
            runner_host       VARCHAR2(255 CHAR),
            runner_user       VARCHAR2(128 CHAR),
            runner_pid        NUMBER(10),
            db_session        VARCHAR2(200 CHAR),
            tool_version      VARCHAR2(64 CHAR)           NOT NULL,
            CONSTRAINT m8_history_pk     PRIMARY KEY (migration_id),
            CONSTRAINT m8_history_seq_uq UNIQUE (seq),
            CONSTRAINT m8_history_seq_ck CHECK (seq > 0),
            CONSTRAINT m8_history_lang_ck
                CHECK (language IN ('sql','python')),
            CONSTRAINT m8_history_mode_ck
                CHECK ("MODE" IN ('atomic','restartable')),
            CONSTRAINT m8_history_status_ck
                CHECK (status IN ('ACTIVE','SUCCESS')),
            CONSTRAINT m8_history_attempt_ck
                CHECK (attempt IS NULL OR attempt > 0),
            CONSTRAINT m8_history_active_ck
                CHECK (status <> 'ACTIVE' OR ("MODE" = 'restartable'
                       AND attempt IS NOT NULL AND finished_at IS NULL)),
            CONSTRAINT m8_history_success_ck
                CHECK (status <> 'SUCCESS' OR finished_at IS NOT NULL)
        )
    """,
    ACTIVE_INDEX: f"""
        CREATE UNIQUE INDEX {{index}} ON {{history}} ({ACTIVE_INDEX_EXPRESSION})
    """,
    PROGRESS_TABLE: """
        CREATE TABLE {progress} (
            migration_id VARCHAR2(200 CHAR)          NOT NULL,
            prog_key     VARCHAR2(128 CHAR)          NOT NULL,
            prog_value   VARCHAR2(4000 BYTE)         NOT NULL,
            updated_at   TIMESTAMP(6) WITH TIME ZONE NOT NULL,
            CONSTRAINT m8_progress_pk PRIMARY KEY (migration_id, prog_key),
            CONSTRAINT m8_progress_fk FOREIGN KEY (migration_id)
                REFERENCES {history} (migration_id),
            CONSTRAINT m8_progress_key_ck   CHECK (LENGTH(prog_key) BETWEEN 1 AND 128),
            CONSTRAINT m8_progress_value_ck CHECK (LENGTH(prog_value) >= 1)
        )
    """,
    META_TABLE: """
        CREATE TABLE {meta} (
            meta_key         VARCHAR2(30 CHAR)           NOT NULL,
            layout_version   NUMBER(10)                  NOT NULL,
            adapter          VARCHAR2(30 CHAR)           NOT NULL,
            lock_provider    VARCHAR2(30 CHAR)           NOT NULL,
            lock_binding     VARCHAR2(200 CHAR)          NOT NULL,
            target_namespace VARCHAR2(128 CHAR)          NOT NULL,
            initialized_at   TIMESTAMP(6) WITH TIME ZONE NOT NULL,
            CONSTRAINT m8_meta_pk PRIMARY KEY (meta_key),
            CONSTRAINT m8_meta_singleton_ck CHECK (meta_key = 'singleton')
        )
    """,
}

#: ``(name, data_type, char_length, char_used, nullable, precision, scale)``.
_EXPECTED_COLUMNS = {
    HISTORY_TABLE: (
        ("SEQ", "NUMBER", 0, None, "N", 10, 0),
        ("MIGRATION_ID", "VARCHAR2", 200, "C", "N", None, None),
        ("FINGERPRINT", "VARCHAR2", 80, "C", "N", None, None),
        ("FIRST_FINGERPRINT", "VARCHAR2", 80, "C", "N", None, None),
        ("LANGUAGE", "VARCHAR2", 10, "C", "N", None, None),
        ("MODE", "VARCHAR2", 12, "C", "N", None, None),
        ("STATUS", "VARCHAR2", 8, "C", "N", None, None),
        ("ATTEMPT", "NUMBER", 0, None, "Y", 10, 0),
        ("STARTED_AT", "TIMESTAMP(6) WITH TIME ZONE", 0, None, "N", None, 6),
        ("LAST_ATTEMPT_AT", "TIMESTAMP(6) WITH TIME ZONE", 0, None, "Y", None, 6),
        ("FINISHED_AT", "TIMESTAMP(6) WITH TIME ZONE", 0, None, "Y", None, 6),
        ("RUNNER_HOST", "VARCHAR2", 255, "C", "Y", None, None),
        ("RUNNER_USER", "VARCHAR2", 128, "C", "Y", None, None),
        ("RUNNER_PID", "NUMBER", 0, None, "Y", 10, 0),
        ("DB_SESSION", "VARCHAR2", 200, "C", "Y", None, None),
        ("TOOL_VERSION", "VARCHAR2", 64, "C", "N", None, None),
    ),
    PROGRESS_TABLE: (
        ("MIGRATION_ID", "VARCHAR2", 200, "C", "N", None, None),
        ("PROG_KEY", "VARCHAR2", 128, "C", "N", None, None),
        ("PROG_VALUE", "VARCHAR2", 4000, "B", "N", None, None),
        ("UPDATED_AT", "TIMESTAMP(6) WITH TIME ZONE", 0, None, "N", None, 6),
    ),
    META_TABLE: (
        ("META_KEY", "VARCHAR2", 30, "C", "N", None, None),
        ("LAYOUT_VERSION", "NUMBER", 0, None, "N", 10, 0),
        ("ADAPTER", "VARCHAR2", 30, "C", "N", None, None),
        ("LOCK_PROVIDER", "VARCHAR2", 30, "C", "N", None, None),
        ("LOCK_BINDING", "VARCHAR2", 200, "C", "N", None, None),
        ("TARGET_NAMESPACE", "VARCHAR2", 128, "C", "N", None, None),
        ("INITIALIZED_AT", "TIMESTAMP(6) WITH TIME ZONE", 0, None, "N", None, 6),
    ),
}

#: The complete supported constraint shape, one entry per metadata table.  Each
#: check is the canonical form of the condition ALL_CONSTRAINTS holds for the
#: DDL above; Oracle stores the condition as written, so these were recorded
#: from Oracle Database 23ai Free 23.9.0.25.7 on 2026-09-13 rather than guessed.
#: Oracle's NOT NULL constraints are check rows too; they are recognised by
#: shape and compared against the column layout instead.
_EXPECTED_CONSTRAINTS: dict[str, md.ExpectedConstraints] = {
    HISTORY_TABLE: {
        "keys": (md.ExpectedKey("P", "MIGRATION_ID"), md.ExpectedKey("U", "SEQ")),
        "checks": (
            "SEQ > 0",
            "LANGUAGE IN ( 'sql' , 'python' )",
            "MODE IN ( 'atomic' , 'restartable' )",
            "STATUS IN ( 'ACTIVE' , 'SUCCESS' )",
            "ATTEMPT IS NULL OR ATTEMPT > 0",
            "STATUS <> 'ACTIVE' OR ( MODE = 'restartable' AND ATTEMPT IS NOT NULL "
            "AND FINISHED_AT IS NULL )",
            "STATUS <> 'SUCCESS' OR FINISHED_AT IS NOT NULL",
        ),
    },
    PROGRESS_TABLE: {
        "keys": (
            md.ExpectedKey("P", "MIGRATION_ID,PROG_KEY"),
            md.ExpectedKey("R", "MIGRATION_ID", (HISTORY_TABLE, "MIGRATION_ID")),
        ),
        "checks": (
            "LENGTH ( PROG_KEY ) BETWEEN 1 AND 128",
            "LENGTH ( PROG_VALUE ) >= 1",
        ),
    },
    META_TABLE: {
        "keys": (md.ExpectedKey("P", "META_KEY"),),
        "checks": ("META_KEY = 'singleton'",),
    },
}
