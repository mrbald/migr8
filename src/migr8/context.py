"""The Python migration facade (spec Section 9.2).

The driver connection, cursors and commit methods are not exposed.  That is API
discipline, not sandboxing: migration code is trusted, and the engine's
safeguards catch honest mistakes rather than proving arbitrary effects safe.

The atomic and restartable facades are separate classes, so ``ctx.transaction``
and ``ctx.ddl`` genuinely do not exist in an atomic migration.
"""

from __future__ import annotations

import logging
from typing import Literal

from .adapters.base import Adapter, Boundary, OutcomeClass
from .diagnostics import RunLog
from .errors import MigrationFailedError, UnknownOutcomeError, UsageError
from .latch import RunLatch
from .manifest import Mode
from .model import PROGRESS_KEY_MAX_CHARS, PROGRESS_VALUE_MAX_BYTES, CapturedUnit
from .paths import check_relative_path
from .sqltext import normalize
from .testing import hooks

LOGGER = logging.getLogger("migr8.migration")


class _BaseContext:
    """Members available in every execution mode."""

    def __init__(
        self,
        *,
        adapter: Adapter,
        unit: CapturedUnit,
        latch: RunLatch,
        attempt: int | None,
        run_log: RunLog,
    ) -> None:
        self._adapter = adapter
        self._unit = unit
        self._latch = latch
        self._attempt = attempt
        self._log = run_log
        self._in_batch = False

    # --- identity ---------------------------------------------------------------------------------

    @property
    def migration_id(self) -> str:
        return self._unit.id

    @property
    def attempt(self) -> int | None:
        return self._attempt

    # --- internals --------------------------------------------------------------------------------

    @property
    def _mode(self) -> Mode:
        return self._unit.mode

    def _prepare(self, sql: str):
        self._latch.check()
        statement = normalize(sql)
        self._adapter.admit_statement(statement, mode=self._mode, in_batch=self._in_batch)
        return statement

    def _guard_call(self, operation: str, func, *args, phase: str = "migration_execution"):
        """Run a non-commit-capable driver call and classify any failure.

        A call that is not commit-capable can still lose the transport.  When it
        does, the outcome of *that call* is unknown, so the run is latched.
        """
        try:
            return func(*args)
        except Exception as exc:
            if self._adapter.classify_exception(exc) is OutcomeClass.COMMUNICATION_FAILURE:
                raise self._latch.latch_unknown(
                    UnknownOutcomeError(
                        str(exc),
                        operation=operation,
                        phase=phase,
                        migration_id=self.migration_id,
                    )
                ) from exc
            raise

    @property
    def batch_open(self) -> bool:
        """True when a batch was entered and never exited.

        Always False in atomic mode, which has no batches; the engine's
        post-return check can therefore ask any context.
        """
        return False

    # --- SQL --------------------------------------------------------------------------------------

    def execute(self, sql: str, params: object | None = None) -> int:
        """Execute one statement.  Returns the affected-row count for supported DML."""
        statement = self._prepare(sql)
        return self._guard_call("execute", self._adapter.execute, statement, params)

    def executemany(self, sql: str, parameter_sets: list[object]) -> int:
        """Execute supported DML over parameter sets; returns the total row count.

        Per-row error continuation is disabled: the first error raises.
        """
        if not isinstance(parameter_sets, list):
            parameter_sets = list(parameter_sets)
        statement = self._prepare(sql)
        if not parameter_sets:
            return 0
        return self._guard_call("executemany", self._adapter.executemany, statement, parameter_sets)

    def query(self, sql: str, params: object | None = None) -> list[tuple]:
        """Execute one query and return a list of tuples in selected-column order."""
        statement = self._prepare(sql)
        return self._guard_call("query", self._adapter.query, statement, params)

    # --- unit-local files -------------------------------------------------------------------------

    def sql(self, relative_path: str) -> str:
        """Read text from a regular SQL file inside this migration's staged unit."""
        self._latch.check()
        check_relative_path(relative_path, what=f"ctx.sql() path for {self.migration_id!r}")
        root = self._unit.source_dir
        candidate = (root / relative_path).resolve(strict=False)
        if root.resolve(strict=False) not in candidate.parents:
            raise UsageError(
                f"ctx.sql({relative_path!r}) escapes the unit of {self.migration_id!r}"
            )
        if relative_path not in self._unit.relpaths:
            raise UsageError(
                f"ctx.sql({relative_path!r}) is not a fingerprinted file of {self.migration_id!r}"
            )
        if candidate.is_symlink() or not candidate.is_file():
            raise UsageError(f"ctx.sql({relative_path!r}) is not a regular file in the staged unit")
        return candidate.read_text(encoding="utf-8")

    # --- logging ----------------------------------------------------------------------------------

    def log(self, message: str, **fields: object) -> None:
        """Structured log line correlated with this run and migration.

        The line goes to the same event log as the engine's own events, so an
        author's progress notes interleave with the phases around them. Fields
        are the author's choice and the author's responsibility: do not pass
        credentials or sensitive bind values.
        """
        self._log.event(
            "migration_log",
            migration=self.migration_id,
            attempt=self._attempt,
            message=message,
            **fields,
        )


class AtomicContext(_BaseContext):
    """Atomic mode: one engine-owned transaction, no batch and no DDL.

    ``transaction`` and ``ddl`` are intentionally absent rather than present and
    raising, so author code cannot reach for them at all.
    """


class ProgressFacade:
    """The transactional progress store (spec Sections 8.2 and 9.2)."""

    __slots__ = ("_context",)

    def __init__(self, context: RestartableContext) -> None:
        self._context = context

    def get(self, key: str, default: str | None = None) -> str | None:
        ctx = self._context
        ctx._latch.check()
        _check_progress_key(key)
        value = ctx._guard_call("progress_get", ctx._adapter.progress_get, ctx.migration_id, key)
        return default if value is None else value

    def set(self, key: str, value: str) -> None:
        """Write a checkpoint inside the current batch transaction.

        Never commits independently: the write is part of the batch and becomes
        durable with it.
        """
        ctx = self._context
        ctx._latch.check()
        _check_progress_key(key)
        _check_progress_value(value)
        if not ctx._in_batch:
            raise UsageError(
                "ctx.progress.set() is only available inside a ctx.transaction() block, so a "
                "checkpoint always commits with the batch it describes",
                migration_id=ctx.migration_id,
            )
        ctx._guard_call("progress_set", ctx._adapter.progress_set, ctx.migration_id, key, value)


def _check_progress_key(key: str) -> None:
    if not isinstance(key, str) or not key:
        raise UsageError("progress keys must be non-empty strings")
    if len(key) > PROGRESS_KEY_MAX_CHARS:
        raise UsageError(f"progress key exceeds {PROGRESS_KEY_MAX_CHARS} characters: {len(key)}")
    if not key.isascii():
        raise UsageError(f"progress key {key!r} must be ASCII")


def _check_progress_value(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise UsageError(
            "progress values must be non-empty strings; the empty string is rejected so "
            "Oracle's empty-string/NULL handling cannot change this API"
        )
    size = len(value.encode("utf-8"))
    if size > PROGRESS_VALUE_MAX_BYTES:
        raise UsageError(f"progress value exceeds {PROGRESS_VALUE_MAX_BYTES} UTF-8 bytes: {size}")


class _BatchContext:
    """One batch transaction, as an explicit object rather than a generator.

    Using a real context manager keeps "entered but never exited" observable:
    a generator-based version would be closed non-deterministically by garbage
    collection, which would hide exactly the contract breach the engine must
    report.
    """

    __slots__ = ("_owner",)

    def __init__(self, owner: RestartableContext) -> None:
        self._owner = owner

    def __enter__(self) -> RestartableContext:
        owner = self._owner
        owner._latch.check()
        if owner._in_batch:
            raise UsageError(
                "ctx.transaction() blocks do not nest", migration_id=owner.migration_id
            )
        if owner._adapter.has_open_transaction():
            raise UsageError(
                "a transaction is already open when entering ctx.transaction(); the batch "
                "must own its transaction",
                migration_id=owner.migration_id,
            )
        owner._adapter.begin()
        owner._in_batch = True
        owner._open_batch = self
        return owner

    def __exit__(self, exc_type: object, exc: object, tb: object) -> Literal[False]:
        owner = self._owner
        owner._in_batch = False
        owner._open_batch = None
        if exc_type is not None:
            # After an unknown commit outcome no further SQL may be issued, so
            # the batch is abandoned without a rollback (spec Section 7.2).
            if not owner._latch.unknown:
                try:
                    owner._adapter.rollback()
                except Exception:
                    LOGGER.warning("rollback after batch failure did not complete cleanly")
            return False
        owner._commit_batch()
        return False


class RestartableContext(_BaseContext):
    """Restartable mode: explicit batches, supported DDL, and the progress store."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.progress = ProgressFacade(self)
        self._open_batch: _BatchContext | None = None

    @property
    def batch_open(self) -> bool:
        """True when a batch was entered and never exited."""
        return self._open_batch is not None

    def transaction(self) -> _BatchContext:
        """One batch transaction.  Commits on clean exit, rolls back on failure.

        Inside the block the atomic transaction-control rules apply: no DDL, no
        manual commit or rollback, and no hidden transaction boundaries.
        """
        self._latch.check()
        return _BatchContext(self)

    def _commit_batch(self) -> None:
        try:
            self._adapter.durable_commit(Boundary.RESTARTABLE_BATCH, migration_id=self.migration_id)
        except UnknownOutcomeError:
            raise
        except Exception as exc:
            raise MigrationFailedError(
                f"batch commit was rejected by the server: {exc}",
                phase="restartable_batch",
                migration_id=self.migration_id,
            ) from exc

    def ddl(self, sql: str) -> None:
        """Execute one admitted DDL statement outside any batch, with no open transaction."""
        self._latch.check()
        if self._in_batch:
            raise UsageError(
                "ctx.ddl() is not available inside a ctx.transaction() block",
                migration_id=self.migration_id,
            )
        statement = normalize(sql)
        self._adapter.admit_ddl(statement)
        if self._adapter.has_open_transaction():
            raise UsageError(
                "ctx.ddl() requires no open transaction: the engine will not use a pre-DDL "
                "implicit commit to flush unrelated pending work",
                migration_id=self.migration_id,
            )
        hooks.fire(Boundary.RESTARTABLE_DDL, hooks.BEFORE_DDL)
        self._guard_call(
            "DDL execution",
            self._adapter.execute_ddl,
            statement,
            phase="restartable_ddl",
        )
        hooks.fire(Boundary.RESTARTABLE_DDL, hooks.AFTER_DDL)


def build_context(
    *, adapter: Adapter, unit: CapturedUnit, latch: RunLatch, attempt: int | None, run_log: RunLog
) -> _BaseContext:
    cls = RestartableContext if unit.mode is Mode.RESTARTABLE else AtomicContext
    return cls(adapter=adapter, unit=unit, latch=latch, attempt=attempt, run_log=run_log)
