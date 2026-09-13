"""Pure validation of durable state against a manifest capture (spec Section 8.3).

This module performs no I/O.  It is given an already-read consistent snapshot
and an already-fingerprinted capture, and it either returns a :class:`Plan` or
raises.  Keeping it pure is what makes the state machine testable without a
database.

Two failure classes are distinguished deliberately, because they mean different
things to an operator:

``MetadataDamagedError`` (exit 7)
    The metadata rows are internally inconsistent or use an unsupported
    layout/format.  No manifest could make them valid.  Examples: a position
    gap, two ACTIVE rows, an ACTIVE row recorded as atomic, a stored
    fingerprint in an unknown format, progress attached to a SUCCESS row.

``ValidationError`` (exit 2)
    The metadata is self-consistent but does not agree with the manifest or its
    source.  Examples: a SUCCESS row whose fingerprint differs from the current
    source, an id or position that does not match, history longer than the
    manifest.
"""

from __future__ import annotations

from .errors import MetadataDamagedError, RecoveryRequiredError, ValidationError
from .fingerprint import is_supported_fingerprint
from .manifest import Mode
from .model import (
    LAYOUT_VERSION,
    Capture,
    HistoryRow,
    Plan,
    Snapshot,
    Status,
)


def validate_snapshot_shape(snapshot: Snapshot) -> None:
    """Check everything that is wrong regardless of which manifest is in hand."""
    if snapshot.meta is not None and snapshot.meta.layout_version != LAYOUT_VERSION:
        raise MetadataDamagedError(
            f"metadata layout_version {snapshot.meta.layout_version} is not supported "
            f"by this tool (expected {LAYOUT_VERSION}); history is not rewritten"
        )

    rows = sorted(snapshot.history, key=lambda row: row.seq)

    seen_seq: dict[int, str] = {}
    seen_ids: dict[str, int] = {}
    for row in rows:
        if row.seq < 1:
            raise MetadataDamagedError(
                f"history row {row.migration_id!r} has non-positive position {row.seq}"
            )
        if row.seq in seen_seq:
            raise MetadataDamagedError(
                f"history position {row.seq} is used by both {seen_seq[row.seq]!r} "
                f"and {row.migration_id!r}"
            )
        seen_seq[row.seq] = row.migration_id
        if row.migration_id in seen_ids:
            raise MetadataDamagedError(
                f"history contains duplicate migration id {row.migration_id!r}"
            )
        seen_ids[row.migration_id] = row.seq

    for index, row in enumerate(rows, start=1):
        if row.seq != index:
            raise MetadataDamagedError(
                f"history positions are not consecutive from 1: expected {index}, "
                f"found {row.seq} for {row.migration_id!r}"
            )

    for row in rows:
        _validate_row(row)

    actives = [row for row in rows if row.is_active]
    if len(actives) > 1:
        names = ", ".join(repr(row.migration_id) for row in actives)
        raise MetadataDamagedError(f"more than one ACTIVE history row: {names}")

    if actives:
        active = actives[0]
        successes = [row for row in rows if row.is_success]
        # This one comparison is the whole rule. Positions are already known to
        # be consecutive from 1, every status is already known to be ACTIVE or
        # SUCCESS, and there is already known to be exactly one ACTIVE row, so
        # "the ACTIVE row sits immediately after the successful prefix" implies
        # both "everything before it is SUCCESS" and "nothing follows it".
        if active.seq != len(successes) + 1:
            raise MetadataDamagedError(
                f"ACTIVE migration {active.migration_id!r} is at position {active.seq} but the "
                f"successful prefix has length {len(successes)}; ACTIVE must be the next position"
            )

    _validate_progress(snapshot, rows)


def _validate_row(row: HistoryRow) -> None:
    if row.status not in (Status.ACTIVE, Status.SUCCESS):
        raise MetadataDamagedError(
            f"history row {row.migration_id!r} has unsupported status {row.status!r}"
        )
    if row.language not in ("sql", "python"):
        raise MetadataDamagedError(
            f"history row {row.migration_id!r} has unsupported language {row.language!r}"
        )
    if row.mode not in ("atomic", "restartable"):
        raise MetadataDamagedError(
            f"history row {row.migration_id!r} has unsupported mode {row.mode!r}"
        )
    for label, value in (
        ("fingerprint", row.fingerprint),
        ("first_fingerprint", row.first_fingerprint),
    ):
        if not value:
            raise MetadataDamagedError(f"history row {row.migration_id!r} has an empty {label}")
        if not is_supported_fingerprint(value):
            raise MetadataDamagedError(
                f"history row {row.migration_id!r} {label} {value!r} uses an unsupported "
                f"fingerprint format; this tool does not rewrite history"
            )
    if row.is_active:
        if row.mode != Mode.RESTARTABLE:
            raise MetadataDamagedError(
                f"ACTIVE history row {row.migration_id!r} is recorded as {row.mode!r}; "
                "ACTIVE implies restartable"
            )
        if row.attempt is None or row.attempt < 1:
            raise MetadataDamagedError(
                f"ACTIVE history row {row.migration_id!r} has invalid attempt {row.attempt!r}"
            )
        if row.finished_at is not None:
            raise MetadataDamagedError(
                f"ACTIVE history row {row.migration_id!r} has a completion time"
            )
        if row.started_at is None:
            raise MetadataDamagedError(f"ACTIVE history row {row.migration_id!r} has no start time")
    else:
        if row.finished_at is None:
            raise MetadataDamagedError(
                f"SUCCESS history row {row.migration_id!r} has no completion time"
            )
        if row.mode == Mode.ATOMIC and row.attempt is not None:
            raise MetadataDamagedError(
                f"atomic history row {row.migration_id!r} records attempt {row.attempt}; "
                "atomic attempts are not counted"
            )
        if row.mode == Mode.RESTARTABLE and (row.attempt is None or row.attempt < 1):
            raise MetadataDamagedError(
                f"restartable history row {row.migration_id!r} has invalid attempt {row.attempt!r}"
            )


def _validate_progress(snapshot: Snapshot, rows: list[HistoryRow]) -> None:
    by_id = {row.migration_id: row for row in rows}
    seen: set[tuple[str, str]] = set()
    for entry in snapshot.progress:
        key = (entry.migration_id, entry.prog_key)
        if key in seen:
            raise MetadataDamagedError(
                f"duplicate progress key {entry.prog_key!r} for {entry.migration_id!r}"
            )
        seen.add(key)
        owner = by_id.get(entry.migration_id)
        if owner is None:
            raise MetadataDamagedError(
                f"progress row {entry.prog_key!r} references unknown migration "
                f"{entry.migration_id!r}"
            )
        # These two refusals are the whole rule. Every history row is already
        # known to be ACTIVE or SUCCESS and at most one is ACTIVE, so a progress
        # row that names a known, unfinished migration names the ACTIVE one.
        if owner.is_success:
            raise MetadataDamagedError(
                f"progress row {entry.prog_key!r} is attached to successful migration "
                f"{entry.migration_id!r}; restartable completion deletes progress"
            )
        if not entry.prog_value:
            raise MetadataDamagedError(
                f"progress row {entry.prog_key!r} for {entry.migration_id!r} has an empty value"
            )


def build_plan(capture: Capture, snapshot: Snapshot) -> Plan:
    """Validate ``snapshot`` against ``capture`` and return the execution plan.

    The ACTIVE fingerprint is *compared* but a mismatch does not raise here:
    the caller decides whether ``--recover`` admits it.  Every other mismatch
    raises.
    """
    validate_snapshot_shape(snapshot)

    rows = sorted(snapshot.history, key=lambda row: row.seq)
    units = capture.units

    if len(rows) > len(units):
        extra = rows[len(units)]
        raise ValidationError(
            f"history has {len(rows)} rows but the manifest declares {len(units)} migrations; "
            f"position {extra.seq} ({extra.migration_id!r}) is not in the manifest"
        )

    successes = [row for row in rows if row.is_success]
    for row in successes:
        unit = units[row.seq - 1]
        if unit.id != row.migration_id:
            raise ValidationError(
                f"successful history at position {row.seq} is {row.migration_id!r} but the "
                f"manifest has {unit.id!r}; identities and positions are permanent"
            )
        if row.language != unit.language.value:
            raise ValidationError(
                f"successful migration {row.migration_id!r} recorded language "
                f"{row.language!r} but the manifest declares {unit.language.value!r}"
            )
        if row.mode != unit.mode.value:
            raise ValidationError(
                f"successful migration {row.migration_id!r} recorded mode {row.mode!r} "
                f"but the manifest declares {unit.mode.value!r}; execution mode is immutable"
            )
        if row.fingerprint != unit.fingerprint:
            raise ValidationError(
                f"successful migration {row.migration_id!r} has changed: recorded "
                f"{row.fingerprint}, current source {unit.fingerprint}. Successful history is "
                "immutable; restore the published source."
            )

    active = next((row for row in rows if row.is_active), None)
    changed = False
    if active is not None:
        unit = units[active.seq - 1]
        if unit.id != active.migration_id:
            raise ValidationError(
                f"ACTIVE history at position {active.seq} is {active.migration_id!r} but the "
                f"manifest has {unit.id!r}"
            )
        if unit.mode is not Mode.RESTARTABLE:
            raise ValidationError(
                f"ACTIVE migration {active.migration_id!r} is declared {unit.mode.value!r} in "
                "the manifest; an active migration's restartable mode cannot change"
            )
        changed = active.fingerprint != unit.fingerprint

    success_count = len(successes)
    pending = units[success_count:]
    return Plan(
        capture=capture,
        snapshot=snapshot,
        success_count=success_count,
        active=active,
        pending=tuple(pending),
        active_fingerprint_changed=changed,
    )


def require_no_recovery_needed(plan: Plan) -> None:
    """Raise the recovery-required condition used by plain ``migrate`` and ``validate``."""
    if not plan.active_fingerprint_changed or plan.active is None:
        return
    unit = plan.capture.at_position(plan.active.seq)
    assert unit is not None
    raise RecoveryRequiredError(
        f"active migration {plan.active.migration_id!r} source has changed: recorded "
        f"{plan.active.fingerprint}, requested {unit.fingerprint}. "
        f"To admit the amended source run: migr8 migrate --recover "
        f"{plan.active.migration_id}",
        migration_id=plan.active.migration_id,
    )


def check_recovery_admission(plan: Plan, recover_id: str) -> None:
    """Validate ``migrate --recover ID`` against the plan (spec Section 10.1).

    Called only after :func:`build_plan` has already confirmed the successful
    prefix and the active row's identity, position and mode.  Everything here
    runs before any metadata mutation.
    """
    active = plan.active
    if active is None:
        raise ValidationError(
            f"--recover {recover_id} requires an ACTIVE restartable migration; "
            "there is none. Recovery never admits edits to successful migrations."
        )
    if active.migration_id != recover_id:
        raise ValidationError(
            f"--recover {recover_id} does not match the active migration "
            f"{active.migration_id!r}; recovery is valid only for the existing active identity",
            migration_id=active.migration_id,
        )
    unit = plan.capture.at_position(active.seq)
    if unit is None or unit.id != active.migration_id:
        raise ValidationError(
            f"--recover {recover_id} cannot change the active migration's position"
        )
    if unit.mode is not Mode.RESTARTABLE or active.mode != Mode.RESTARTABLE:
        raise ValidationError(
            f"--recover {recover_id} cannot change execution mode; switching an active "
            "restartable migration to atomic is not supported",
            migration_id=active.migration_id,
        )
