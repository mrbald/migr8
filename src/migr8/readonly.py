"""``validate`` and ``status``: read-only application operations (spec Section 11.2).

Neither command initializes metadata, stages units, imports migration code,
executes migration SQL, recompiles anything, or takes the migration lock.  Both
use one short consistent metadata read.

``validate --offline`` makes no connection at all.  It is the plan lint a
pipeline can run before a target exists, and it answers a strictly smaller
question: whether this plan is well formed and admissible on the selected
backend.  It cannot say that the SQL is valid on the server, that the privileges
are there, or that a restartable migration converges.  Only the engine's own
check, under the namespace lock, decides whether a plan may be applied.
"""

from __future__ import annotations

import json
from pathlib import Path

from .adapters.base import Adapter
from .checks import CHECKS, preflight, verify_bindings
from .errors import (
    Exit,
    MetadataDamagedError,
    Migr8Error,
    RecoveryRequiredError,
    UsageError,
    ValidationError,
)
from .model import Capture, MetadataState, Snapshot
from .reporting import MigrationStatus, Report, status_from_row
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


def verify_baseline(capture: Capture, baseline: Path) -> None:
    """Compare the plan against a previously approved plan artifact.

    A single manifest cannot show that a published migration was edited: the
    manifest and the unit change together, and the result is internally
    consistent.  The approved artifact is the other side of that comparison, so
    a pipeline holds published identity, order and fingerprint by keeping one
    and checking against it.

    Every entry the artifact records must appear at the same position, with the
    same id and the same fingerprint.  Entries after them are new work and are
    not constrained.  This is a source-to-source check: it says nothing about
    what any database has recorded, and an ACTIVE migration amended through
    ``migrate --recover`` is a deliberate operator action that changes the
    artifact once it is approved again.
    """
    try:
        document = json.loads(baseline.read_text(encoding="utf-8"))
    except OSError as exc:
        raise UsageError(
            f"the baseline plan {baseline} cannot be read: {exc.strerror or type(exc).__name__}"
        ) from exc
    except ValueError as exc:
        raise UsageError(f"the baseline plan {baseline} is not JSON") from exc
    recorded = document.get("migrations") if isinstance(document, dict) else None
    if not isinstance(recorded, list) or not all(isinstance(item, dict) for item in recorded):
        raise UsageError(
            f"the baseline plan {baseline} is not a migr8 JSON report with a migrations list"
        )

    differences = []
    for entry in recorded:
        position = entry.get("position")
        unit = capture.at_position(position) if isinstance(position, int) else None
        if unit is None:
            differences.append(
                f"position {position} holds {entry.get('id')!r} in the approved plan and "
                "nothing here"
            )
            continue
        if unit.id != entry.get("id"):
            differences.append(
                f"position {position} is {unit.id!r} here and {entry.get('id')!r} in the "
                "approved plan"
            )
        elif unit.fingerprint != entry.get("current_fingerprint"):
            differences.append(
                f"{unit.id!r} has fingerprint {unit.fingerprint} here and "
                f"{entry.get('current_fingerprint')} in the approved plan"
            )
    if differences:
        raise ValidationError(
            f"the plan differs from the approved plan {baseline}: " + "; ".join(differences),
            phase="baseline",
        )


def run_offline(adapter: Adapter, capture: Capture, *, baseline: Path | None = None) -> Report:
    """Lint the plan with no database connection and no secret.

    Nothing is imported, executed or compiled into bytecode, and no namespace is
    touched.  Exit 0 here means the plan is well formed and admissible on this
    backend; it is not a statement about any target.
    """
    report = Report(
        command="validate --offline",
        adapter=adapter.name,
        server="not connected",
        namespace=adapter.normalized_namespace(),
        metadata_state="not read",
        initialized=False,
        pending_count=0,
    )
    report.checks = list(CHECKS)
    report.migrations = [
        MigrationStatus(
            position=unit.position,
            id=unit.id,
            state="UNREAD",
            mode=unit.mode.value,
            language=unit.language.value,
            current_fingerprint=unit.fingerprint,
        )
        for unit in capture.units
    ]
    if baseline is not None:
        report.checks.append(f"published identity, order and fingerprints against {baseline}")
    try:
        preflight(capture, adapter)
        if baseline is not None:
            verify_baseline(capture, baseline)
    except Migr8Error as exc:
        report.problem_kind = "validation"
        report.problem = exc.report()
        report.exit_code = int(exc.exit_code)
    return report


def _run(
    command: str,
    adapter: Adapter,
    capture: Capture,
    *,
    with_liveness: bool,
    recovery_suffix: str = "",
    baseline: Path | None = None,
) -> Report:
    """The shared body of both read-only commands.

    They differ only in whether the active migration's session is probed and in
    how much the recovery-required message spells out, so the sequence itself
    exists once: preflight, connect, inspect, read, then validate the plan
    without acting on it.
    """
    preflight(capture, adapter)
    if baseline is not None:
        verify_baseline(capture, baseline)
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


def run_validate(adapter: Adapter, capture: Capture, *, baseline: Path | None = None) -> Report:
    """Check the manifest and history contracts.  Modifies nothing."""
    return _run(
        "validate",
        adapter,
        capture,
        with_liveness=False,
        baseline=baseline,
        recovery_suffix=(
            " This is a recovery-required condition, not permission to modify the active marker."
        ),
    )


__all__ = ["run_offline", "run_status", "run_validate", "verify_baseline"]
