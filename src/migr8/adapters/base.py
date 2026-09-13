"""The database adapter contract.

The engine owns the state machine; the adapter owns what is genuinely
engine-specific: session setup, the namespace lock, the physical metadata
layout, transaction identity, execution and error classification.

Everything that is a *rule* rather than a dialect lives here and exists once.
The history transitions, the progress store and statement admission are
implemented in this class, so three adapters cannot drift on which columns a
recovery may touch, whether an affected-row count was checked, or which leading
tokens a batch admits. An adapter supplies the dialect, not the policy:

* ``paramstyle``, ``now_expression``, ``supports_on_conflict``, ``quote_char``,
  ``metadata_schema`` and ``column_overrides`` describe how to write the SQL;
* ``statement_policy`` describes which statements are admitted where;
* ``_metadata_execute`` and ``_metadata_query`` run engine-owned SQL on a path
  separate from the migration facade, as the specification requires.

Nothing Oracle-specific belongs in the engine, and no adapter may emulate
another engine's behaviour to make a shared test pass.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

from ..config import Config
from ..errors import (
    MetadataDamagedError,
    UnknownOutcomeError,
    UnsupportedCapabilityError,
    UsageError,
)
from ..latch import RunLatch
from ..manifest import Language, Mode, RequiredObject
from ..model import (
    HISTORY_TABLE,
    LAYOUT_VERSION,
    META_SINGLETON_KEY,
    META_TABLE,
    PROGRESS_TABLE,
    RESERVED_OBJECT_NAMES,
    MetadataReport,
    MetaRow,
    Snapshot,
)
from ..sqltext import Statement, StatementKind
from ..testing import hooks
from . import metadata as md


class OutcomeClass(StrEnum):
    """How a failed database call must be interpreted (spec Section 7.2)."""

    #: The outcome is definite and the call had no durable effect: either the
    #: server rejected it, or the driver refused it before submitting anything.
    SERVER_REJECTION = "server_rejection"
    #: The call's outcome is unknown.  This is the conservative default.
    COMMUNICATION_FAILURE = "communication_failure"


class Boundary(StrEnum):
    """Engine-owned durable transitions (spec Section 7.1)."""

    #: One metadata object created and made durable.  Independently recoverable.
    METADATA_OBJECT_CREATED = "metadata_object_created"
    #: The m8_meta singleton marker, committed last once the layout is verified.
    INITIALIZATION_COMPLETE = "initialization_complete"
    ATOMIC_COMPLETION = "atomic_completion"
    RESTARTABLE_ADMISSION = "restartable_admission"
    RESTARTABLE_BATCH = "restartable_batch"
    RESTARTABLE_COMPLETION = "restartable_completion"
    #: Oracle DDL commits independently, so it is a durable transition too.
    RESTARTABLE_DDL = "restartable_ddl"


@dataclass(frozen=True, slots=True)
class ValidityResult:
    """Outcome of the read-only final-validity check (spec Section 6)."""

    #: Declarations that failed, with a reason each.  Non-empty means failure.
    failures: tuple[str, ...] = ()
    #: Compiler warnings, reported but never a cause of failure on their own.
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunnerInfo:
    """Latest-attempt diagnostics stored with a history transition."""

    host: str
    user: str
    pid: int
    tool_version: str
    run_id: str


@dataclass(slots=True)
class Capabilities:
    """What an adapter implements, reported separately from what was tested."""

    adapter: str
    transactional_ddl_in_atomic: bool
    oracle_style_required_objects: bool
    database_backed_lock: bool
    transaction_identity_tripwire: bool
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class StatementPolicy:
    """Declarative statement admission for one engine (spec Section 5.3).

    The policy says which leading tokens are admitted where. The *rules* about
    which context applies -- atomic, inside a batch, outside a batch -- are
    enforced once, in :meth:`Adapter.admit_statement`.
    """

    #: Admitted inside an atomic transaction or a restartable batch.
    atomic: frozenset[str]
    #: Admitted outside a batch in restartable mode: reads only.
    query: frozenset[str]
    #: Procedural forms that may own their own transactions in restartable mode.
    procedural: frozenset[str]
    #: The explicit ``ctx.ddl()`` allow-list.
    ddl: frozenset[str]
    #: Never admitted through the facade, in any context.
    forbidden: frozenset[str]
    #: Whether the engine has PL/SQL blocks and stored definitions at all.
    allows_plsql: bool = False


class Adapter(ABC):
    """One physical database session for the whole run."""

    #: Adapter identity recorded in ``m8_meta``.
    name: ClassVar[str]
    #: The driver's base exception, so shared metadata code can catch the one
    #: thing this engine raises rather than swallowing every exception.
    driver_error: ClassVar[type[Exception]] = Exception

    # --- dialect: how engine-owned metadata SQL is written ---------------------

    #: ``"named"`` for ``:name`` placeholders, ``"pyformat"`` for ``%(name)s``.
    #: Shared metadata SQL is always written with ``:name`` and translated.
    paramstyle: ClassVar[str] = "named"
    #: SQL expression returning the database's own current timestamp.
    now_expression: ClassVar[str] = "CURRENT_TIMESTAMP"
    #: True when ``INSERT ... ON CONFLICT`` is available; False selects ``MERGE``.
    supports_on_conflict: ClassVar[bool] = True
    #: One-row table for the MERGE form, for engines without ON CONFLICT.
    one_row_table: ClassVar[str] = "dual"
    #: Identifier quote character.
    quote_char: ClassVar[str] = '"'
    #: Case an unquoted identifier folds to on this engine.  Oracle folds to
    #: upper, so quoting must follow: ``"mode"`` and ``"MODE"`` are different
    #: columns, and only the second matches a table created with ``mode``.
    identifier_case: ClassVar[str] = "lower"

    def __init__(self, config: Config) -> None:
        self.config = config
        #: Set by the engine so durable commits can latch the run (spec Section 7.2).
        self.latch: RunLatch | None = None
        #: Set by the engine; recorded as latest-attempt diagnostics.
        self.runner: RunnerInfo | None = None
        #: Schema or owner qualifying metadata objects; None when the engine has none.
        self.metadata_schema: str | None = None
        self._engine_transaction = False

    # --- engine-owned durable commit ------------------------------------------

    def durable_commit(self, boundary: Boundary, *, migration_id: str | None = None) -> None:
        """Commit an engine-owned transition, classifying the outcome honestly.

        A communication failure here is an unknown outcome: the run is latched,
        no retry is attempted, and the caller must discard the connection
        without issuing further SQL.
        """
        hooks.fire(boundary, hooks.BEFORE_COMMIT)
        try:
            self.commit()
        except BaseException as exc:
            # An interruption while a commit is in flight is as unknown as a lost
            # acknowledgement: the request may already be durable. Only a
            # positively identified definite failure escapes as itself.
            if isinstance(exc, Exception) and \
                    self.classify_exception(exc) is OutcomeClass.SERVER_REJECTION:
                raise
            error = UnknownOutcomeError(
                f"{type(exc).__name__}: {exc}" if not isinstance(exc, Exception)
                else str(exc),
                operation=f"{boundary.value} commit",
                phase=boundary.value,
                migration_id=migration_id,
            )
            if self.latch is not None:
                self.latch.latch_unknown(error)
            raise error from exc
        hooks.fire(boundary, hooks.AFTER_COMMIT)

    # --- capability reporting -------------------------------------------------

    @abstractmethod
    def capabilities(self) -> Capabilities:
        ...

    @abstractmethod
    def server_description(self) -> str:
        """A recorded server banner or version string for test evidence."""

    @abstractmethod
    def normalized_namespace(self) -> str:
        """The target namespace as the database spells it, not as the caller did."""

    @abstractmethod
    def lock_binding(self) -> str:
        """A stable string identifying the configured lock, stored in ``m8_meta``."""

    @abstractmethod
    def session_identity(self) -> str | None:
        """Optional diagnostics identifying this physical session."""

    def probe_session_liveness(self, db_session: str | None) -> tuple[str, str]:
        """Optional diagnostic: is the recorded session still present?

        Returns ``(verdict, explanation)`` where verdict is ``"present"``,
        ``"absent"`` or ``"unknown"``.  A match requires a full usable identity,
        not a reusable session number alone, and session existence is never
        proof that a migration is executing or holding the lock.
        """
        return ("unknown", f"the {self.name} adapter provides no session-liveness diagnostic")

    # --- lifecycle -------------------------------------------------------------

    @abstractmethod
    def connect(self) -> None:
        """Open the single physical session and establish required settings."""

    def prepare_storage(self) -> None:
        """Controlled, explicitly invoked storage preparation.

        Only ``migrate`` calls this, under the namespace lock, so a read-only
        command can never change storage settings as a side effect.
        """
        return

    @abstractmethod
    def close(self) -> None:
        """Release the lock if held and close the session cleanly."""

    @abstractmethod
    def discard(self) -> None:
        """Drop the connection without issuing any further SQL.

        Called after an unknown outcome.  It must not roll back, commit, or run
        cleanup statements.
        """

    # --- namespace lock --------------------------------------------------------

    @abstractmethod
    def acquire_lock(self) -> None:
        """Acquire the namespace lock, held across every commit until the run ends."""

    @abstractmethod
    def release_lock(self) -> None:
        ...

    # --- metadata --------------------------------------------------------------

    def inspect_metadata(self) -> MetadataReport:
        """Inspect the namespace without creating or altering anything.

        The sequence is the same on every engine: which objects exist, whether
        their definitions match, then the marker.  Only the dictionary queries
        behind :meth:`_objects_present` and :meth:`_definition_problems` are
        engine-specific, and forcing those into one shape would obscure all three.
        """
        present = self._objects_present()
        problems = self._definition_problems(present)
        meta = None
        if META_TABLE in present:
            try:
                meta, rows = self._read_meta()
            except self.driver_error as exc:
                problems.append(f"{META_TABLE} cannot be read: {exc}")
            else:
                if meta is not None and rows != 1:
                    problems.append(
                        f"{META_TABLE} holds {rows} rows; exactly one is expected"
                    )
        return md.classify(
            present=present,
            problems=problems,
            meta=meta,
            history_count=self._count(HISTORY_TABLE, present),
            progress_count=self._count(PROGRESS_TABLE, present),
        )

    def initialize(self) -> None:
        """Create missing metadata objects in fixed order, verify, then mark complete.

        The marker is written last and only once the whole layout has been
        re-inspected, so an interruption leaves a recognisable prefix rather than
        a namespace that claims to be initialized.  This exists once because it
        is the rule; only the physical creation is delegated.
        """
        present = self._objects_present()
        for name in md.CREATION_ORDER:
            if name not in present:
                self._create_metadata_object(name)

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
            f"INSERT INTO {self.metadata_name(META_TABLE)} "
            f"({self._columns(*md.META_COLUMNS)}) "
            "VALUES (:meta_key, :layout_version, :adapter, :lock_provider, "
            ":lock_binding, :target_namespace, {now})",
            {
                "meta_key": META_SINGLETON_KEY,
                "layout_version": LAYOUT_VERSION,
                "adapter": self.name,
                "lock_provider": self.config.lock.provider,
                "lock_binding": self.lock_binding(),
                "target_namespace": self.normalized_namespace(),
            },
        )
        self.durable_commit(Boundary.INITIALIZATION_COMPLETE)

    def _read_meta(self) -> tuple[MetaRow | None, int]:
        """Read the singleton marker and the marker table's row count."""
        meta_table = self.metadata_name(META_TABLE)
        count = int(self._fetch(f"SELECT COUNT(*) FROM {meta_table}", {})[0][0])
        rows = self._fetch(
            f"SELECT {self._columns(*md.META_COLUMNS)} FROM {meta_table} "
            f"WHERE {self.metadata_column('meta_key')} = :meta_key",
            {"meta_key": META_SINGLETON_KEY},
        )
        if not rows:
            return None, count
        return md.meta_row(tuple(rows[0]), md.parse_iso_timestamp), count

    def _count(self, table: str, present: set[str]) -> int | None:
        """Row count of a metadata table, or None when it does not exist."""
        if table not in present:
            return None
        return int(self._fetch(f"SELECT COUNT(*) FROM {self.metadata_name(table)}", {})[0][0])

    @abstractmethod
    def _objects_present(self) -> set[str]:
        """Which metadata objects exist, named as ``md.CREATION_ORDER`` names them.

        Engines that fold identifiers must fold back: the caller compares these
        against the logical lower-case names, never against the dictionary's
        spelling.
        """

    @abstractmethod
    def _definition_problems(self, present: set[str]) -> list[str]:
        """Every way the existing objects differ from the supported layout."""

    @abstractmethod
    def _create_metadata_object(self, name: str) -> None:
        """Create one metadata object and make it durable on its own.

        Engines with transactional DDL wrap it in an engine transaction and a
        ``METADATA_OBJECT_CREATED`` commit; Oracle's DDL commits by itself.
        """

    @abstractmethod
    def read_snapshot(self, *, consistent: bool) -> Snapshot:
        """Read history, progress and the marker as one consistent view."""

    # --- engine-owned SQL path -------------------------------------------------

    @abstractmethod
    def _metadata_execute(self, sql: str, params: Mapping[str, object]) -> int:
        """Run one engine-owned metadata statement; return the affected-row count.

        This path is separate from the migration facade on purpose: engine SQL is
        never subject to statement admission, and migration SQL never reaches
        here.
        """

    @abstractmethod
    def _metadata_query(self, sql: str, params: Mapping[str, object]) -> list[tuple]:
        """Run one engine-owned metadata query."""

    # --- identifier and placeholder rendering ----------------------------------

    def quoted(self, identifier: str) -> str:
        quote = self.quote_char
        return f"{quote}{identifier}{quote}"

    def metadata_name(self, logical: str) -> str:
        """Quoted, schema-qualified physical name of a metadata object.

        Qualifying internally means migration code cannot redirect engine writes
        by changing name resolution.
        """
        physical = self.quoted(self._physical_object_name(logical))
        if self.metadata_schema is None:
            return physical
        return f"{self.quoted(self.metadata_schema)}.{physical}"

    def metadata_column(self, logical: str) -> str:
        """Quoted physical column name.

        Quoting is safe only because the name is folded to the engine's own case
        first: a reserved word such as Oracle's ``MODE`` then needs no special
        case, and no caller can inject an identifier here.
        """
        return self.quoted(self._fold(logical))

    def _physical_object_name(self, logical: str) -> str:
        return self._fold(logical)

    def _fold(self, identifier: str) -> str:
        return identifier.upper() if self.identifier_case == "upper" else identifier.lower()

    def _render(self, sql: str) -> str:
        """Translate shared ``:name`` placeholders and ``{now}`` for this engine.

        Placeholders are translated *before* ``{now}`` is substituted, for the
        same reason :meth:`_binds` reads placeholder names from the unrendered
        template: an engine's timestamp expression must not be able to look like
        a placeholder.  A cast such as ``now()::timestamptz`` would otherwise
        become ``now():%(timestamptz)s``.
        """
        rendered = sql
        if self.paramstyle == "pyformat":
            rendered = re.sub(r":([a-z_][a-z0-9_]*)", r"%(\1)s", rendered)
        elif self.paramstyle != "named":  # pragma: no cover - guarded at import
            raise UsageError(f"unsupported paramstyle {self.paramstyle!r}")
        return rendered.replace("{now}", self.now_expression)

    def _columns(self, *logical: str) -> str:
        return ", ".join(self.metadata_column(name) for name in logical)

    def _binds(self, sql: str, params: Mapping[str, object]) -> dict[str, object]:
        """Narrow ``params`` to the placeholders this statement actually uses.

        python-oracledb rejects a bind value with no matching placeholder, while
        sqlite3 and psycopg ignore it. Narrowing here means one shared statement
        can be built from a superset of binds on every engine. A placeholder with
        no value is still an error, so a typo fails loudly rather than binding
        NULL.

        Placeholder names are read from the unrendered template, before ``{now}``
        is substituted, so an engine's timestamp expression cannot be mistaken
        for a placeholder.
        """
        names = set(re.findall(r":([a-z_][a-z0-9_]*)", sql))
        missing = sorted(names - set(params))
        if missing:
            raise UsageError(
                f"engine-owned SQL is missing bind values for: {', '.join(missing)}"
            )
        return {name: params[name] for name in names}

    def _exec(self, sql: str, params: Mapping[str, object]) -> int:
        """Render and run one engine-owned metadata statement."""
        return self._metadata_execute(self._render(sql), self._binds(sql, params))

    def _fetch(self, sql: str, params: Mapping[str, object]) -> list[tuple]:
        """Render and run one engine-owned metadata query."""
        return self._metadata_query(self._render(sql), self._binds(sql, params))

    # --- history transitions (shared; spec Sections 8.2, 8.5 and 10.1) ---------

    _HISTORY_INSERT_COLUMNS = (
        "seq", "migration_id", "fingerprint", "first_fingerprint", "language",
        "mode", "status", "attempt", "started_at", "last_attempt_at",
        "finished_at", "runner_host", "runner_user", "runner_pid", "db_session",
        "tool_version",
    )

    def _runner_binds(self) -> dict[str, object]:
        runner = self.runner
        return {
            "runner_host": runner.host if runner else None,
            "runner_user": runner.user if runner else None,
            "runner_pid": runner.pid if runner else None,
            "db_session": self.session_identity(),
            "tool_version": runner.tool_version if runner else "unknown",
        }

    def _insert_history_sql(self, *, status: str, attempt: str, finished: str) -> str:
        columns = self._columns(*self._HISTORY_INSERT_COLUMNS)
        return (
            f"INSERT INTO {self.metadata_name(HISTORY_TABLE)} ({columns}) VALUES "
            f"(:seq, :migration_id, :fingerprint, :fingerprint, :language, "
            f":exec_mode, '{status}', {attempt}, {{now}}, {{now}}, {finished}, "
            f":runner_host, :runner_user, :runner_pid, :db_session, :tool_version)"
        )

    def insert_success_row(self, *, seq: int, migration_id: str, fingerprint: str,
                           language: Language, mode: Mode) -> None:
        """Insert a SUCCESS row inside the caller's already-open transaction.

        Atomic attempts are not counted, so ``attempt`` is NULL.
        """
        self._exec(
            self._insert_history_sql(status="SUCCESS", attempt="NULL", finished="{now}"),
            {
                "seq": seq, "migration_id": migration_id, "fingerprint": fingerprint,
                "language": language.value, "exec_mode": mode.value,
                **self._runner_binds(),
            },
        )

    def insert_active_row(self, *, seq: int, migration_id: str, fingerprint: str,
                          language: Language) -> None:
        """Insert the first ACTIVE row and commit it as attempt 1.

        Both fingerprints are set to the admitted value; no migration code runs
        until this commit is acknowledged.
        """
        self.begin()
        self._exec(
            self._insert_history_sql(status="ACTIVE", attempt="1", finished="NULL"),
            {
                "seq": seq, "migration_id": migration_id, "fingerprint": fingerprint,
                "language": language.value, "exec_mode": Mode.RESTARTABLE.value,
                **self._runner_binds(),
            },
        )
        self.durable_commit(Boundary.RESTARTABLE_ADMISSION, migration_id=migration_id)

    def update_active_attempt(self, *, migration_id: str, fingerprint: str,
                              language: Language) -> None:
        """Increment the attempt counter for the matching ACTIVE row and commit.

        Position, identity, mode, the original start time and the first
        fingerprint are never in the SET list, so recovery cannot change them.
        An affected-row count other than one is metadata damage.

        The new attempt number is not read back.  The caller holds the namespace
        lock and read the previous value under it, so the incremented value is
        already known and a round trip would only restate it.
        """
        history = self.metadata_name(HISTORY_TABLE)
        attempt = self.metadata_column("attempt")
        self._metadata_execute_in_transaction(
            f"UPDATE {history} SET {attempt} = {attempt} + 1, "
            f"{self.metadata_column('fingerprint')} = :fingerprint, "
            f"{self.metadata_column('language')} = :language, "
            f"{self.metadata_column('last_attempt_at')} = {{now}}, "
            f"{self.metadata_column('runner_host')} = :runner_host, "
            f"{self.metadata_column('runner_user')} = :runner_user, "
            f"{self.metadata_column('runner_pid')} = :runner_pid, "
            f"{self.metadata_column('db_session')} = :db_session, "
            f"{self.metadata_column('tool_version')} = :tool_version "
            f"WHERE {self.metadata_column('migration_id')} = :migration_id "
            f"AND {self.metadata_column('status')} = 'ACTIVE'",
            {"fingerprint": fingerprint, "language": language.value,
             "migration_id": migration_id, **self._runner_binds()},
            what=f"admitting a new attempt for {migration_id!r}",
        )
        self.durable_commit(Boundary.RESTARTABLE_ADMISSION, migration_id=migration_id)

    def complete_active_row(self, *, migration_id: str) -> None:
        """ACTIVE to SUCCESS plus progress deletion, in one committed transaction."""
        history = self.metadata_name(HISTORY_TABLE)
        self._metadata_execute_in_transaction(
            f"UPDATE {history} SET {self.metadata_column('status')} = 'SUCCESS', "
            f"{self.metadata_column('finished_at')} = {{now}}, "
            f"{self.metadata_column('tool_version')} = :tool_version "
            f"WHERE {self.metadata_column('migration_id')} = :migration_id "
            f"AND {self.metadata_column('status')} = 'ACTIVE'",
            {"tool_version": self._runner_binds()["tool_version"],
             "migration_id": migration_id},
            what=f"completing {migration_id!r}",
        )
        self._exec(
            f"DELETE FROM {self.metadata_name(PROGRESS_TABLE)} "
            f"WHERE {self.metadata_column('migration_id')} = :migration_id",
            {"migration_id": migration_id},
        )
        self.durable_commit(Boundary.RESTARTABLE_COMPLETION, migration_id=migration_id)

    def _metadata_execute_in_transaction(self, sql: str, params: Mapping[str, object],
                                         *, what: str) -> None:
        """Open a transaction, run one update, and require exactly one affected row.

        Returns normally only when exactly one row was affected, so callers need
        no further check.
        """
        self.begin()
        affected = self._exec(sql, params)
        if affected != 1:
            self.rollback()
            raise MetadataDamagedError(
                f"{what} affected {affected} rows; exactly one ACTIVE row was expected"
            )

    # --- progress store (shared; spec Sections 8.2 and 9.2) --------------------

    def progress_get(self, migration_id: str, key: str) -> str | None:
        rows = self._fetch(
            f"SELECT {self.metadata_column('prog_value')} "
            f"FROM {self.metadata_name(PROGRESS_TABLE)} "
            f"WHERE {self.metadata_column('migration_id')} = :migration_id "
            f"AND {self.metadata_column('prog_key')} = :prog_key",
            {"migration_id": migration_id, "prog_key": key},
        )
        return None if not rows else str(rows[0][0])

    def progress_set(self, migration_id: str, key: str, value: str) -> None:
        """Write one progress row inside the caller's open batch transaction.

        The precondition is the engine transaction this adapter opened, not the
        database's notion of uncommitted work: on Oracle the checkpoint is often
        the first write in the batch, so no transaction id exists yet.
        """
        if not self._engine_transaction:
            raise UsageError("progress writes require an open batch transaction")
        progress = self.metadata_name(PROGRESS_TABLE)
        columns = self._columns("migration_id", "prog_key", "prog_value", "updated_at")
        if self.supports_on_conflict:
            sql = (
                f"INSERT INTO {progress} ({columns}) "
                f"VALUES (:migration_id, :prog_key, :prog_value, {{now}}) "
                f"ON CONFLICT ({self._columns('migration_id', 'prog_key')}) DO UPDATE SET "
                f"{self.metadata_column('prog_value')} = :prog_value, "
                f"{self.metadata_column('updated_at')} = {{now}}"
            )
        else:
            mid = self.metadata_column("migration_id")
            pkey = self.metadata_column("prog_key")
            source = (
                f"SELECT :migration_id AS {mid}, :prog_key AS {pkey} "
                f"FROM {self.one_row_table}"
            )
            sql = (
                f"MERGE INTO {progress} target USING ({source}) source "
                f"ON (target.{mid} = source.{mid} AND target.{pkey} = source.{pkey}) "
                f"WHEN MATCHED THEN UPDATE SET "
                f"target.{self.metadata_column('prog_value')} = :prog_value, "
                f"target.{self.metadata_column('updated_at')} = {{now}} "
                f"WHEN NOT MATCHED THEN INSERT ({columns}) "
                f"VALUES (:migration_id, :prog_key, :prog_value, {{now}})"
            )
        self._exec(
            sql, {"migration_id": migration_id, "prog_key": key, "prog_value": value}
        )

    # --- transaction control (engine-owned) ------------------------------------

    @property
    def in_engine_transaction(self) -> bool:
        """True between an engine-issued begin and its commit or rollback."""
        return self._engine_transaction

    def begin(self) -> None:
        """Open an engine transaction.

        Both guards are needed. ``has_open_transaction`` alone is not enough: on
        Oracle no transaction exists until the first write, so a second ``begin``
        would otherwise be accepted and two nested scopes would silently share
        one transaction.
        """
        if self._engine_transaction:
            raise UsageError("an engine transaction is already open")
        if self.has_open_transaction():
            raise UsageError("a transaction is already open with uncommitted work")
        self._do_begin()
        self._engine_transaction = True

    def commit(self) -> None:
        self._do_commit()
        self._engine_transaction = False

    def rollback(self) -> None:
        self._do_rollback()
        self._engine_transaction = False

    @abstractmethod
    def _do_begin(self) -> None:
        """Start a transaction, or verify the engine will start one implicitly."""

    @abstractmethod
    def _do_commit(self) -> None:
        ...

    @abstractmethod
    def _do_rollback(self) -> None:
        ...

    @abstractmethod
    def has_open_transaction(self) -> bool:
        """True when a writable transaction is open, by actual driver/server state."""

    # --- atomic transaction identity ------------------------------------------

    @abstractmethod
    def establish_transaction_identity(self) -> str | None:
        """Create and capture the transaction identity before any migration code runs.

        Returning ``None`` means this adapter provides a different guard; see
        :meth:`capabilities`.
        """

    @abstractmethod
    def read_transaction_identity(self) -> str | None:
        """Read the current identity without creating a transaction."""

    # --- statement admission (shared; spec Section 5.3) -----------------------

    @property
    @abstractmethod
    def statement_policy(self) -> StatementPolicy:
        """Which leading tokens this engine admits in each context."""

    def admit_combination(self, language: Language, mode: Mode) -> None:
        """Reject an unsupported language/mode combination without changing it.

        All four combinations are supported by every shipped adapter; an adapter
        that cannot honour one overrides this and says so.
        """
        return

    def admit_required_objects(self, required: tuple[RequiredObject, ...]) -> None:
        """Reject required-object declarations this adapter cannot check.

        The default refuses any nonempty set, because a validity contract that
        is not implemented must never be silently ignored. An adapter that can
        check declarations overrides this.
        """
        if required:
            names = ", ".join(f"{obj.type} {obj.name}" for obj in required)
            raise UnsupportedCapabilityError(
                f"the {self.name} adapter cannot check required-object declarations "
                f"({names}); a validity contract must be defined and fingerprinted "
                "explicitly before it is admitted"
            )

    def admit_statement(self, statement: Statement, *, mode: Mode, in_batch: bool) -> None:
        """Admit or refuse a facade statement before submission.

        This is an honest-mistake guard, not proof of transitive effects. The
        context rules are the same for every engine; only the token sets differ.
        """
        policy = self.statement_policy
        first = statement.first_token
        if statement.kind is not StatementKind.SQL and not policy.allows_plsql:
            raise UnsupportedCapabilityError(
                f"the {self.name} adapter has no PL/SQL support; {first} is not admitted"
            )
        if first in policy.forbidden:
            raise UsageError(
                f"{' '.join(statement.lead[:2]) or first} is not admitted through the "
                "facade: transaction control, session state and maintenance statements "
                "are engine-owned"
            )
        self._extra_statement_checks(statement, mode=mode, in_batch=in_batch)
        self._reject_reserved(statement)
        if mode is Mode.ATOMIC or in_batch:
            if first not in policy.atomic:
                raise UsageError(
                    f"{first} is not admitted in an atomic transaction or a restartable "
                    f"batch; admitted leading tokens are {_listed(policy.atomic)}"
                )
            return
        # Restartable, outside a batch: reads, plus procedural forms that are
        # trusted to own their own transactions.
        if first not in policy.query | policy.procedural:
            raise UsageError(
                f"{first} outside a ctx.transaction() block is rejected in restartable "
                "mode; use ctx.transaction() for DML or ctx.ddl() for DDL"
            )

    def admit_ddl(self, statement: Statement) -> None:
        """Admit or refuse a ``ctx.ddl()`` statement against the adapter allow-list.

        A stored PL/SQL definition is DDL and is admitted here; an anonymous
        block is not, because it is procedural code rather than a DDL statement
        and has its own admission path.
        """
        policy = self.statement_policy
        first = statement.first_token
        if statement.kind is StatementKind.PLSQL_BLOCK:
            raise UsageError(
                "an anonymous PL/SQL block is not DDL; run it with ctx.execute() in a "
                "restartable migration, where it may own its own transactions"
            )
        if first not in policy.ddl:
            raise UsageError(
                f"{first or '<empty>'} is not in the {self.name} DDL allow-list "
                f"({_listed(policy.ddl)})"
            )
        self._extra_statement_checks(statement, mode=Mode.RESTARTABLE, in_batch=False)
        self._reject_reserved(statement)

    def _extra_statement_checks(self, statement: Statement, *, mode: Mode,
                               in_batch: bool) -> None:
        """Engine-specific refusals a token set cannot express."""
        return

    def _reject_reserved(self, statement: Statement) -> None:
        """Refuse any statement naming an engine-reserved metadata object.

        The comparison is against the statement's identifier tokens, not its raw
        text.  A substring scan refuses far more than it should: a table called
        ``custom8_history`` contains ``m8_history``, and so does a comment or a
        string literal that merely mentions the engine table.
        """
        for reserved in sorted(RESERVED_OBJECT_NAMES):
            if reserved.upper() in statement.names:
                raise UsageError(
                    f"migration code must not reference the reserved metadata object "
                    f"{reserved}; use the supplied progress API"
                )

    # --- execution -------------------------------------------------------------

    @abstractmethod
    def _run(self, text: str, params: object | None) -> Any:
        """Submit one migration statement and return the driver cursor.

        This is the facade's execution path, separate from ``_metadata_execute``:
        migration SQL is submitted verbatim, after admission, with the author's
        own bind parameters.
        """

    def execute(self, statement: Statement, params: object | None) -> int:
        return max(self._run(statement.text, params).rowcount, 0)

    def query(self, statement: Statement, params: object | None) -> list[tuple]:
        return [tuple(row) for row in self._run(statement.text, params).fetchall()]

    @abstractmethod
    def executemany(self, statement: Statement, parameter_sets: list[object]) -> int:
        """Not shared: the drivers differ on how per-row errors are suppressed."""

    def execute_ddl(self, statement: Statement) -> None:
        """Run DDL in its own transaction, for engines whose DDL is transactional.

        Oracle's implicit DDL commits are not emulated; that adapter overrides
        this with a plain submission.
        """
        self.begin()
        try:
            self._run(statement.text, None)
        except self.driver_error:
            self.rollback()
            raise
        self.durable_commit(Boundary.RESTARTABLE_DDL)

    # --- final validity --------------------------------------------------------

    def check_required_objects(self, required: tuple[RequiredObject, ...]) -> ValidityResult:
        """Read-only check of every declaration.  No compilation is performed.

        The default reports failure for a nonempty set, matching
        :meth:`admit_required_objects`; preflight should already have refused it.
        """
        if required:
            return ValidityResult(failures=(
                f"the {self.name} adapter cannot check required-object declarations",
            ))
        return ValidityResult()

    # --- error classification --------------------------------------------------

    @abstractmethod
    def classify_exception(self, exc: BaseException) -> OutcomeClass:
        """Classify a driver exception.

        Only a positively identified server rejection, or a driver refusal
        raised before anything was submitted, may return ``SERVER_REJECTION``.
        Anything else, including an unrecognised error, is a communication
        failure and therefore an unknown outcome from a commit-capable call.
        """


def _listed(tokens: frozenset[str]) -> str:
    return ", ".join(sorted(tokens))


def bind_params(params: object | None, *, empty: object):
    """Normalise an author's bind parameters for a DB-API driver.

    ``empty`` is what "no parameters" means to this driver, and the drivers do
    not agree: sqlite3 refuses ``None``, while psycopg treats ``None`` as "do not
    interpolate at all", which is what keeps a literal ``%`` in migration SQL
    from being read as a placeholder.
    """
    if params is None:
        return empty
    if isinstance(params, (list, tuple)):
        return tuple(params)
    if isinstance(params, dict):
        return params
    return (params,)
