"""``validate`` and ``status``: read-only application operations (spec Section 11.2).

Neither command initializes metadata, stages units, imports migration code,
executes migration SQL, recompiles anything, or takes the migration lock.  Both
use one short consistent metadata read.
"""

from __future__ import annotations

from .adapters.base import Adapter
from .checks import preflight, verify_bindings
from .errors import (
    Exit,
    MetadataDamagedError,
    Migr8Error,
    RecoveryRequiredError,
)
from .model import Capture, MetadataState, Snapshot
from .reporting import Report, status_from_row
from .statevalidate import build_plan, require_no_recovery_needed


def _base_report(command: str, adapter: Adapter, capture: Capture, state: MetadataState) -> Report:
    return Report(
        command=command,
        adapter=adapter.name,
        server=adapter.server_description(),
        namespace=adapter.normalized_namespace(),
        metadata_state=state.value,
        initialized=state is MetadataState.COMPLETE,
        pending_count=len(capture.units),
    )


def _pending_only(report: Report, capture: Capture) -> Report:
    """Fill the migration list when no history can be read."""
    report.migrations = [
        status_from_row(
            unit.position,
            unit.id,
            unit.mode.value,
            unit.language.value,
            unit.fingerprint,
            None,
        )
        for unit in capture.units
    ]
    return report


def _fill(
    report: Report, capture: Capture, snapshot: Snapshot, adapter: Adapter, *, with_liveness: bool
) -> None:
    by_id = {row.migration_id: row for row in snapshot.history}
    entries = []
    success = 0
    pending = 0
    active_id = None
    for unit in capture.units:
        row = by_id.get(unit.id)
        entry = status_from_row(
            unit.position,
            unit.id,
            unit.mode.value,
            unit.language.value,
            unit.fingerprint,
            row,
        )
        if row is None:
            pending += 1
        elif row.is_success:
            success += 1
        else:
            pending += 1
            active_id = row.migration_id
            if with_liveness:
                verdict, detail = adapter.probe_session_liveness(row.db_session)
                entry.session_liveness = verdict
                entry.session_liveness_detail = detail
        entries.append(entry)
    report.migrations = entries
    report.success_count = success
    report.pending_count = pending
    report.active_id = active_id


def _prepare(command: str, adapter: Adapter, capture: Capture) -> tuple[Report, Snapshot | None]:
    """Connect, inspect and read.  Returns the report and a snapshot when readable."""
    report_state = adapter.inspect_metadata()
    report = _base_report(command, adapter, capture, report_state.state)
    if report_state.state is MetadataState.DAMAGED:
        report.problem_kind = "metadata damaged"
        report.problem = "; ".join(report_state.problems)
        report.exit_code = int(Exit.METADATA_DAMAGED)
        _pending_only(report, capture)
        return report, None
    if report_state.state in (MetadataState.ABSENT, MetadataState.INCOMPLETE_COMPATIBLE):
        report.problem_kind = "not initialized"
        report.problem = (
            "the namespace has no completed migration metadata. Read-only commands only; "
            "run migrate to initialize. Absence of metadata is not proof that application "
            "objects have never been migrated."
        )
        report.exit_code = int(Exit.NOT_INITIALIZED)
        _pending_only(report, capture)
        return report, None
    try:
        verify_bindings(adapter, report_state.meta)
    except Migr8Error as exc:
        report.problem_kind = "binding mismatch"
        report.problem = exc.report()
        report.exit_code = int(exc.exit_code)
        _pending_only(report, capture)
        return report, None
    snapshot = adapter.read_snapshot(consistent=True)
    return report, snapshot


def _run(
    command: str,
    adapter: Adapter,
    capture: Capture,
    *,
    with_liveness: bool,
    recovery_suffix: str = "",
) -> Report:
    """The shared body of both read-only commands.

    They differ only in whether the active migration's session is probed and in
    how much the recovery-required message spells out, so the sequence itself
    exists once: preflight, connect, inspect, read, then validate the plan
    without acting on it.
    """
    preflight(capture, adapter)
    adapter.connect()
    try:
        report, snapshot = _prepare(command, adapter, capture)
        if snapshot is None:
            return report
        _fill(report, capture, snapshot, adapter, with_liveness=with_liveness)
        try:
            plan = build_plan(capture, snapshot)
            require_no_recovery_needed(plan)
        except RecoveryRequiredError as exc:
            report.problem_kind = "recovery required"
            report.problem = exc.message + recovery_suffix
            report.recovery_command = f"migr8 migrate --recover {exc.migration_id}"
            report.exit_code = int(exc.exit_code)
        except Migr8Error as exc:
            report.problem_kind = (
                "metadata damaged" if isinstance(exc, MetadataDamagedError) else "validation"
            )
            report.problem = exc.report()
            report.exit_code = int(exc.exit_code)
        return report
    finally:
        adapter.close()


def run_status(adapter: Adapter, capture: Capture) -> Report:
    """Always describe the namespace; the exit code still reflects any failure."""
    return _run("status", adapter, capture, with_liveness=True)


def run_validate(adapter: Adapter, capture: Capture) -> Report:
    """Check the manifest and history contracts.  Modifies nothing."""
    return _run(
        "validate",
        adapter,
        capture,
        with_liveness=False,
        recovery_suffix=(
            " This is a recovery-required condition, not permission to modify the active marker."
        ),
    )


__all__ = ["run_status", "run_validate"]
