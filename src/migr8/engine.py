"""Execution orchestration for ``migrate`` (spec Sections 5, 7 and 11.1).

The engine owns the state machine and every durable transition.  It never
contains database-specific SQL: anything engine-specific is asked of the
adapter.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import socket
import time
import uuid
from dataclasses import asdict, dataclass, field

from .adapters.base import Adapter, Boundary, RunnerInfo
from .checks import preflight, verify_bindings
from .config import Config
from .context import build_context
from .diagnostics import RunLog
from .errors import (
    OUTCOME_NAMES,
    ContractViolationError,
    Exit,
    MetadataDamagedError,
    Migr8Error,
    MigrationFailedError,
    RecoveryRequiredError,
    UnknownOutcomeError,
    ValidationError,
)
from .latch import RunLatch
from .loader import check_result, load_entry
from .manifest import Language, Mode
from .model import Capture, CapturedUnit, MetadataReport, MetadataState, Plan, Snapshot
from .sqltext import StatementKind, normalize
from .statevalidate import (
    build_plan,
    check_recovery_admission,
    require_no_recovery_needed,
)
from .version import TOOL_VERSION

LOGGER = logging.getLogger("migr8.engine")


@dataclass(slots=True)
class RunReport:
    """What a ``migrate`` run did.

    This is the machine-readable outcome record: ``--json`` prints it verbatim,
    so a failure can be diagnosed, or alerted on, without rerunning anything.
    """

    exit_code: Exit = Exit.OK
    run_id: str = ""
    executed: list[str] = field(default_factory=list)
    message: str | None = None
    warnings: list[str] = field(default_factory=list)
    #: Set when the run must not touch the connection again.
    connection_discarded: bool = False
    #: Where the failure happened, for root-cause analysis.
    failed_migration: str | None = None
    phase: str | None = None
    #: Populated on exit 2 when an amended active migration needs admitting.
    recovery_command: str | None = None
    adapter: str | None = None
    namespace: str | None = None
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.exit_code == Exit.OK

    def to_json(self) -> str:
        payload = asdict(self)
        payload["exit_code"] = int(self.exit_code)
        payload["outcome"] = OUTCOME_NAMES[Exit(self.exit_code)]
        return json.dumps(payload, indent=2)


def runner_info(run_id: str) -> RunnerInfo:
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - unusual environments
        user = "unknown"
    return RunnerInfo(
        host=socket.gethostname(),
        user=user,
        pid=os.getpid(),
        tool_version=TOOL_VERSION,
        run_id=run_id,
    )


class Engine:
    """One ``migrate`` run against one namespace."""

    def __init__(
        self,
        *,
        config: Config,
        adapter: Adapter,
        capture: Capture,
        recover_id: str | None = None,
        log: RunLog | None = None,
    ) -> None:
        self.config = config
        self.adapter = adapter
        self.capture = capture
        self.recover_id = recover_id
        self.run_id = log.run_id if log is not None else uuid.uuid4().hex
        self.log = log if log is not None else RunLog(self.run_id)
        self.latch = RunLatch()
        self.report = RunReport(run_id=self.run_id, adapter=adapter.name)
        adapter.latch = self.latch
        adapter.runner = runner_info(self.run_id)

    # --- top level --------------------------------------------------------------------------------

    def run(self) -> RunReport:
        started = time.monotonic()
        connected = False
        self.log.event(
            "run_start",
            command="migrate",
            adapter=self.adapter.name,
            manifest=str(self.capture.manifest.path),
            units=len(self.capture.units),
            recover=self.recover_id,
        )
        try:
            preflight(self.capture, self.adapter)
            self.log.event("preflight_passed")
            self.adapter.connect()
            connected = True
            self.report.namespace = self.adapter.normalized_namespace()
            self.log.event(
                "connected",
                namespace=self.report.namespace,
                server=self.adapter.server_description(),
                session=self.adapter.session_identity(),
            )
            self.adapter.acquire_lock()
            self.log.event("lock_acquired", binding=self.adapter.lock_binding())
            metadata = self.adapter.inspect_metadata()
            self._admit_recovery_namespace(metadata)
            self.adapter.prepare_storage()
            self._initialize_if_needed(metadata)
            plan = self._build_plan()
            self._execute(plan)
        except BaseException as exc:
            return self._terminate(exc, connected=connected, started=started)
        if connected:
            self._close_quietly()
        self.log.event("run_end", outcome="ok", executed=list(self.report.executed))
        return self._finish(started)

    def _terminate(self, exc: BaseException, *, connected: bool, started: float) -> RunReport:
        """Report one terminal failure, with the run latch deciding the outcome.

        A latched unknown outcome or contract violation outranks whatever
        exception arrived here.  Author code that catches the latched error and
        returns, or replaces it with one of its own, must not be able to change
        what the run reports (spec Sections 7.2 and 9.2).
        """
        error = self.latch.error or (exc if isinstance(exc, Migr8Error) else None)
        if error is None:
            return self._handle_interruption(exc, connected=connected, started=started)
        code = Exit(error.exit_code)
        # No retry, no reconnect and no cleanup SQL after an unknown outcome.
        discarded = code is Exit.UNKNOWN_OUTCOME
        self._fail(error, code, discarded=discarded)
        if isinstance(error, RecoveryRequiredError):
            self.report.recovery_command = f"migr8 migrate --recover {error.migration_id}"
        if connected and discarded:
            self.adapter.discard()
        elif connected:
            self._close_quietly()
        return self._finish(started)

    def _fail(self, error: Migr8Error, code: Exit, *, discarded: bool = False) -> None:
        self.report.exit_code = code
        self.report.message = error.report()
        self.report.failed_migration = error.migration_id
        self.report.phase = error.phase
        self.report.connection_discarded = discarded
        self.log.event(
            "run_end",
            outcome=OUTCOME_NAMES[code],
            exit_code=int(code),
            phase=error.phase,
            migration=error.migration_id,
            detail=error.message,
            connection_discarded=discarded,
        )

    def _handle_interruption(
        self, exc: BaseException, *, connected: bool, started: float
    ) -> RunReport:
        """Treat an unlatched interruption as an ordinary failure.

        An interruption inside a commit-capable operation latches the run, and
        :meth:`_terminate` reports that instead.  Reaching here means nothing
        durable is in doubt: uncommitted work is rolled back and a restartable
        migration stays ACTIVE.
        """
        kind = type(exc).__name__
        interrupted = isinstance(exc, KeyboardInterrupt)
        error = MigrationFailedError(
            f"the run was interrupted ({kind}); uncommitted work was rolled back and any "
            "restartable migration remains ACTIVE. Rerun to continue."
            if interrupted
            else f"the run failed unexpectedly ({self._describe(exc)}); uncommitted work was "
            "rolled back and any restartable migration remains ACTIVE",
            phase="interrupted" if interrupted else "internal_error",
        )
        if connected:
            self._rollback_quietly()
        self._fail(error, Exit.MIGRATION_FAILED)
        if connected:
            self._close_quietly()
        if not interrupted:
            # The traceback carries the driver's own message, which quotes the
            # data that produced it, so it is not part of default output.
            LOGGER.debug("unexpected failure during migrate", exc_info=True)
        return self._finish(started)

    def _describe(self, exc: BaseException) -> str:
        """Name a failure without reproducing what the driver said.

        The engine's own errors are written for an operator and carry no driver
        text, so they pass through unchanged.  Anything else is named by type
        and engine error code (spec Section 11.5).
        """
        if isinstance(exc, Migr8Error):
            return exc.report()
        return self.adapter.describe_exception(exc)

    def _finish(self, started: float) -> RunReport:
        self.report.duration_seconds = round(time.monotonic() - started, 4)
        return self.report

    def _close_quietly(self) -> None:
        try:
            self.adapter.close()
        except Exception as exc:  # pragma: no cover - best-effort teardown
            LOGGER.warning("closing the session did not complete cleanly: %s", self._describe(exc))

    # --- initialization ---------------------------------------------------------------------------

    def _admit_recovery_namespace(self, report: MetadataReport) -> None:
        """Refuse an impossible ``--recover`` before any metadata exists (spec Section 10.1).

        Section 10.1 requires missing ACTIVE state to fail before metadata
        mutation.  A namespace without completed metadata holds no ACTIVE
        identity at all, so the refusal belongs here, under the lock but ahead of
        storage preparation and initialization: Oracle's initialization DDL
        commits independently and would be left behind by a later refusal.
        Plain ``migrate`` keeps its recoverable initialization.
        """
        if self.recover_id is None or report.state is MetadataState.COMPLETE:
            return
        if report.state is MetadataState.DAMAGED:
            raise MetadataDamagedError(
                f"migration metadata is damaged or incompatible, so --recover "
                f"{self.recover_id} was not considered; nothing is recreated. "
                + "; ".join(report.problems)
            )
        raise ValidationError(
            f"--recover {self.recover_id} requires an ACTIVE restartable migration, and this "
            f"namespace has no initialized migration metadata ({report.state.value}). No "
            "metadata object was created and no migration ran; run migrate without --recover "
            "to initialize."
        )

    def _initialize_if_needed(self, report: MetadataReport) -> None:
        if report.state is MetadataState.DAMAGED:
            raise MetadataDamagedError(
                "migration metadata is damaged or incompatible; nothing is recreated. "
                + "; ".join(report.problems)
            )
        if report.state in (MetadataState.ABSENT, MetadataState.INCOMPLETE_COMPATIBLE):
            LOGGER.info("initializing migration metadata (%s)", report.state.value)
            self.adapter.initialize()
            report = self.adapter.inspect_metadata()
            if report.state is not MetadataState.COMPLETE:
                raise MetadataDamagedError(
                    "metadata is not complete after initialization: "
                    + "; ".join(report.problems or ("unknown reason",))
                )
        verify_bindings(self.adapter, report.meta)

    # --- planning ---------------------------------------------------------------------------------

    def _build_plan(self) -> Plan:
        snapshot: Snapshot = self.adapter.read_snapshot(consistent=True)
        plan = build_plan(self.capture, snapshot)
        if self.recover_id is None:
            require_no_recovery_needed(plan)
        else:
            check_recovery_admission(plan, self.recover_id)
            if not plan.active_fingerprint_changed:
                LOGGER.info(
                    "--recover %s requested but the active definition is unchanged; "
                    "this is an ordinary retry",
                    self.recover_id,
                )
        return plan

    # --- execution --------------------------------------------------------------------------------

    def _execute(self, plan: Plan) -> None:
        self.log.event(
            "plan",
            successful=plan.success_count,
            pending=[unit.id for unit in plan.pending],
            active=plan.active.migration_id if plan.active else None,
        )
        for unit in plan.pending:
            active = (
                plan.active
                if (plan.active is not None and plan.active.migration_id == unit.id)
                else None
            )
            started = time.monotonic()
            self.log.event(
                "migration_start",
                migration=unit.id,
                position=unit.position,
                mode=unit.mode.value,
                language=unit.language.value,
                fingerprint=unit.fingerprint,
            )
            if unit.mode is Mode.ATOMIC:
                self._run_atomic(unit)
            else:
                self._run_restartable(unit, active)
            self.report.executed.append(unit.id)
            self.log.event(
                "migration_done",
                migration=unit.id,
                seconds=round(time.monotonic() - started, 4),
            )

    # --- atomic -----------------------------------------------------------------------------------

    def _run_atomic(self, unit: CapturedUnit) -> None:
        """Spec Section 5.1.  Work and the SUCCESS row commit together."""
        adapter = self.adapter
        adapter.begin()
        identity = adapter.establish_transaction_identity()
        try:
            self._invoke(unit, attempt=None)
            # A latched unknown outcome or contract violation cannot be cleared
            # by author code catching its exception and returning normally
            # (spec Sections 7.2 and 9.2).
            self.latch.check()
            self._require_valid(unit)
            self._check_transaction_identity(unit, identity)
            adapter.insert_success_row(
                seq=unit.position,
                migration_id=unit.id,
                fingerprint=unit.fingerprint,
                language=unit.language,
                mode=unit.mode,
            )
        except UnknownOutcomeError:
            raise
        except (ContractViolationError, MigrationFailedError):
            # Roll back what remains; no success row is written.  After a contract
            # violation, durable effects of non-compliant code may still need
            # remediation.
            self._rollback_quietly()
            raise
        except Exception as exc:
            # Every other failure during execution, including a facade contract
            # rejection, is an ordinary migration failure: the transaction is
            # rolled back and no history row is written.
            self._rollback_quietly()
            raise self._latched_or(
                MigrationFailedError(
                    f"atomic migration {unit.id!r} failed and was rolled back: "
                    f"{self._describe(exc)}",
                    phase="migration_execution",
                    migration_id=unit.id,
                ),
            ) from exc
        adapter.durable_commit(Boundary.ATOMIC_COMPLETION, migration_id=unit.id)

    def _latched_or(self, ordinary: Migr8Error) -> Migr8Error:
        """The latched error if there is one, otherwise the ordinary failure.

        Author code that swallows a latched failure and raises its own exception
        does not get to downgrade the outcome.
        """
        return self.latch.error if self.latch.error is not None else ordinary

    def _check_transaction_identity(self, unit: CapturedUnit, established: str | None) -> None:
        if established is None:
            return  # This adapter states a different enforcement boundary.
        current = self.adapter.read_transaction_identity()
        if current == established:
            return
        detail = "no transaction is open" if current is None else f"identity is now {current!r}"
        raise self.latch.latch_violation(
            ContractViolationError(
                f"atomic migration {unit.id!r} broke its transaction: established "
                f"{established!r} but {detail}. No success row was written. Durable effects "
                "of the migration may require manual remediation.",
                phase="transaction_identity",
                migration_id=unit.id,
            )
        )

    def _rollback_quietly(self) -> None:
        if self.latch.unknown:
            return  # No SQL after an unknown outcome.
        try:
            self.adapter.rollback()
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("rollback did not complete cleanly: %s", self._describe(exc))

    # --- restartable ------------------------------------------------------------------------------

    def _run_restartable(self, unit: CapturedUnit, active) -> None:
        """Spec Section 5.2.  Admission commits before any migration code runs."""
        adapter = self.adapter
        if active is None:
            adapter.insert_active_row(
                seq=unit.position,
                migration_id=unit.id,
                fingerprint=unit.fingerprint,
                language=unit.language,
            )
            attempt = 1
        else:
            adapter.update_active_attempt(
                migration_id=unit.id,
                fingerprint=unit.fingerprint,
                language=unit.language,
            )
            # The snapshot behind ``active`` was read under the namespace lock,
            # which is still held, so nothing else can have written history since.
            attempt = active.attempt + 1
        self.log.event("admitted", migration=unit.id, attempt=attempt)

        context = None
        try:
            context = self._invoke(unit, attempt=attempt)
        except UnknownOutcomeError:
            raise
        except (ContractViolationError, MigrationFailedError):
            self._post_return_cleanup(unit)
            raise
        except Exception as exc:
            self._post_return_cleanup(unit)
            raise self._latched_or(
                MigrationFailedError(
                    f"restartable migration {unit.id!r} failed; it remains ACTIVE and will be "
                    f"re-entered from its entry point on the next run: {self._describe(exc)}",
                    phase="migration_execution",
                    migration_id=unit.id,
                ),
            ) from exc

        # A latched unknown outcome or contract violation cannot be cleared by
        # author code catching its exception (spec Sections 7.2 and 9.2).
        self.latch.check()

        if (context is not None and context.batch_open) or adapter.has_open_transaction():
            self._rollback_quietly()
            raise MigrationFailedError(
                f"restartable migration {unit.id!r} returned with an open transaction; "
                "uncommitted work was rolled back and the migration remains ACTIVE. The "
                "engine does not silently commit forgotten work.",
                phase="post_return_state",
                migration_id=unit.id,
            )

        self._require_valid(unit)

        adapter.complete_active_row(migration_id=unit.id)

    def _require_valid(self, unit: CapturedUnit) -> None:
        """Run the read-only final-validity check; warnings never fail the run."""
        validity = self.adapter.check_required_objects(unit.definition.required)
        self.report.warnings.extend(validity.warnings)
        if validity.failures:
            raise MigrationFailedError(
                "declared required objects are not valid after execution: "
                + "; ".join(validity.failures),
                phase="final_validity",
                migration_id=unit.id,
            )

    def _post_return_cleanup(self, unit: CapturedUnit) -> None:
        """Roll back remaining uncommitted work; the ACTIVE row is retained.

        The latch is checked before anything else, and not only inside
        ``_rollback_quietly``: ``has_open_transaction`` is itself a round trip on
        Oracle, and no SQL at all may follow an unknown outcome (spec Section 7.2).
        """
        if self.latch.unknown:
            return
        if self.adapter.has_open_transaction():
            self._rollback_quietly()

    # --- invocation -------------------------------------------------------------------------------

    def _invoke(self, unit: CapturedUnit, *, attempt: int | None):
        """Run the migration; returns its context object for Python migrations."""
        if unit.language is Language.SQL:
            self._invoke_sql(unit)
            return None
        return self._invoke_python(unit, attempt=attempt)

    def _invoke_sql(self, unit: CapturedUnit) -> None:
        """Run a SQL entry file through the same operation guard as the facade.

        Admission differs between the two entry paths; classification does not.
        Whether a call can commit follows from the execution context the engine
        established, not from the statement's leading token (spec Section 7.2).
        """
        text = (unit.source_dir / unit.definition.entry).read_text(encoding="utf-8")
        statement = normalize(text)
        if unit.mode is Mode.ATOMIC:
            self.adapter.admit_statement(statement, mode=Mode.ATOMIC, in_batch=False)
            self._guarded(unit, "execute", self.adapter.execute, statement, None)
            return
        if statement.kind is StatementKind.PLSQL_BLOCK:
            # A procedural block in restartable mode may own its transactions;
            # it is trusted to commit all intended work before returning.
            self._guarded(
                unit, "execute", self.adapter.execute, statement, None, commit_capable=True
            )
            return
        self.adapter.admit_ddl(statement)
        if self.adapter.has_open_transaction():
            raise MigrationFailedError(
                f"migration {unit.id!r} cannot run DDL with a transaction already open",
                phase="migration_execution",
                migration_id=unit.id,
            )
        self._guarded(
            unit,
            "DDL execution",
            self.adapter.execute_ddl,
            statement,
            phase="restartable_ddl",
            commit_capable=True,
        )

    def _guarded(
        self,
        unit: CapturedUnit,
        operation: str,
        func,
        *args,
        phase: str = "migration_execution",
        commit_capable: bool = False,
    ):
        return self.adapter.guarded(
            func,
            *args,
            operation=operation,
            phase=phase,
            migration_id=unit.id,
            commit_capable=commit_capable,
        )

    def _invoke_python(self, unit: CapturedUnit, *, attempt: int | None):
        loaded = load_entry(
            migration_id=unit.id, staged_dir=unit.source_dir, entry=unit.definition.entry
        )
        ctx = build_context(
            adapter=self.adapter,
            unit=unit,
            latch=self.latch,
            attempt=attempt,
            run_log=self.log,
        )
        try:
            check_result(loaded.migrate(ctx), migration_id=unit.id)
        finally:
            loaded.unload()
        return ctx


__all__ = ["Engine", "RunReport"]
