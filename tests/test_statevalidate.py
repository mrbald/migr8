"""Pure durable-state validation (spec Sections 8.3 and 14.2 group 2)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from migr8.errors import MetadataDamagedError, RecoveryRequiredError, ValidationError
from migr8.manifest import Language, Manifest, MigrationDef, Mode
from migr8.model import Capture, CapturedUnit, HistoryRow, ProgressRow, Snapshot, Status
from migr8.statevalidate import (
    build_plan,
    check_recovery_admission,
    require_no_recovery_needed,
    validate_snapshot_shape,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def fp(tag: str) -> str:
    return "fp1:" + (tag * 64)[:64]


def unit(position: int, migration_id: str, *, mode=Mode.RESTARTABLE,
         language=Language.PYTHON, fingerprint: str | None = None) -> CapturedUnit:
    definition = MigrationDef(
        position=position, id=migration_id, raw_path=migration_id, language=language,
        mode=mode, entry="migration.py", required=(), unit_dir=Path("/nonexistent"),
    )
    return CapturedUnit(
        definition=definition, source_dir=Path("/nonexistent"),
        fingerprint=fingerprint or fp(str(position)), relpaths=("migration.py",), staged=True,
    )


def capture(*units: CapturedUnit) -> Capture:
    manifest = Manifest(
        path=Path("/nonexistent/manifest.toml"), directory=Path("/nonexistent"),
        migrations=tuple(u.definition for u in units),
    )
    return Capture(manifest=manifest, units=units, staged=True)


def success(position: int, migration_id: str, fingerprint: str, *, mode="restartable",
            language="python", attempt: int | None = 1, first: str | None = None) -> HistoryRow:
    return HistoryRow(
        seq=position, migration_id=migration_id, fingerprint=fingerprint,
        first_fingerprint=first or fingerprint, language=language, mode=mode,
        status=Status.SUCCESS, attempt=None if mode == "atomic" else attempt,
        started_at=T0, last_attempt_at=T0, finished_at=T0, tool_version="test",
    )


def active(position: int, migration_id: str, fingerprint: str, *, attempt: int = 1,
           mode="restartable", language="python", first: str | None = None) -> HistoryRow:
    return HistoryRow(
        seq=position, migration_id=migration_id, fingerprint=fingerprint,
        first_fingerprint=first or fingerprint, language=language, mode=mode,
        status=Status.ACTIVE, attempt=attempt, started_at=T0, last_attempt_at=T0,
        finished_at=None, tool_version="test",
    )


# --- valid states ----------------------------------------------------------------

def test_empty_history_is_valid_and_everything_is_pending():
    cap = capture(unit(1, "a"), unit(2, "b"))
    plan = build_plan(cap, Snapshot(history=(), progress=()))
    assert plan.success_count == 0
    assert [u.id for u in plan.pending] == ["a", "b"]
    assert plan.active is None


def test_full_successful_history_leaves_nothing_pending():
    cap = capture(unit(1, "a"), unit(2, "b"))
    snapshot = Snapshot(
        history=(success(1, "a", fp("1")), success(2, "b", fp("2"))), progress=()
    )
    plan = build_plan(cap, snapshot)
    assert plan.success_count == 2
    assert plan.pending == ()


def test_prefix_plus_active_is_valid():
    cap = capture(unit(1, "a"), unit(2, "b"), unit(3, "c"))
    snapshot = Snapshot(
        history=(success(1, "a", fp("1")), active(2, "b", fp("2"))),
        progress=(ProgressRow("b", "last_id", "10"),),
    )
    plan = build_plan(cap, snapshot)
    assert plan.success_count == 1
    assert plan.active is not None and plan.active.migration_id == "b"
    assert [u.id for u in plan.pending] == ["b", "c"]
    assert not plan.active_fingerprint_changed


# --- internal damage (exit 7) -----------------------------------------------------

def test_position_gap_is_damage():
    snapshot = Snapshot(history=(success(1, "a", fp("1")), success(3, "c", fp("3"))),
                        progress=())
    with pytest.raises(MetadataDamagedError, match="not consecutive"):
        validate_snapshot_shape(snapshot)


def test_duplicate_position_is_damage():
    snapshot = Snapshot(history=(success(1, "a", fp("1")), success(1, "b", fp("2"))),
                        progress=())
    with pytest.raises(MetadataDamagedError, match="position 1 is used by both"):
        validate_snapshot_shape(snapshot)


def test_two_active_rows_are_damage():
    snapshot = Snapshot(history=(active(1, "a", fp("1")), active(2, "b", fp("2"))),
                        progress=())
    with pytest.raises(MetadataDamagedError, match="more than one ACTIVE"):
        validate_snapshot_shape(snapshot)


def test_active_not_at_the_next_position_is_damage():
    snapshot = Snapshot(
        history=(success(1, "a", fp("1")), success(2, "b", fp("2")), active(2, "c", fp("3"))),
        progress=(),
    )
    with pytest.raises(MetadataDamagedError):
        validate_snapshot_shape(snapshot)


def test_success_after_an_active_row_is_damage():
    """ACTIVE must be the last row and immediately after the successful prefix."""
    snapshot = Snapshot(history=(active(1, "a", fp("1")), success(2, "b", fp("2"))),
                        progress=())
    with pytest.raises(MetadataDamagedError, match="ACTIVE must be the next position"):
        validate_snapshot_shape(snapshot)


def test_non_success_before_the_active_row_is_damage():
    snapshot = Snapshot(history=(
        active(1, "a", fp("1")), active(2, "b", fp("2")),
    ), progress=())
    with pytest.raises(MetadataDamagedError, match="more than one ACTIVE"):
        validate_snapshot_shape(snapshot)


def test_active_marked_atomic_is_damage():
    snapshot = Snapshot(history=(active(1, "a", fp("1"), mode="atomic"),), progress=())
    with pytest.raises(MetadataDamagedError, match="ACTIVE implies restartable"):
        validate_snapshot_shape(snapshot)


def test_active_without_attempt_is_damage():
    snapshot = Snapshot(history=(
        HistoryRow(
            seq=1, migration_id="a", fingerprint=fp("1"), first_fingerprint=fp("1"),
            language="python", mode="restartable", status=Status.ACTIVE, attempt=None,
            started_at=T0, last_attempt_at=T0, finished_at=None,
        ),
    ), progress=())
    with pytest.raises(MetadataDamagedError, match="invalid attempt"):
        validate_snapshot_shape(snapshot)


def test_success_without_completion_time_is_damage():
    snapshot = Snapshot(history=(
        HistoryRow(
            seq=1, migration_id="a", fingerprint=fp("1"), first_fingerprint=fp("1"),
            language="python", mode="restartable", status=Status.SUCCESS, attempt=1,
            started_at=T0, last_attempt_at=T0, finished_at=None,
        ),
    ), progress=())
    with pytest.raises(MetadataDamagedError, match="no completion time"):
        validate_snapshot_shape(snapshot)


def test_atomic_success_with_an_attempt_count_is_damage():
    snapshot = Snapshot(history=(
        HistoryRow(
            seq=1, migration_id="a", fingerprint=fp("1"), first_fingerprint=fp("1"),
            language="sql", mode="atomic", status=Status.SUCCESS, attempt=3,
            started_at=T0, last_attempt_at=T0, finished_at=T0,
        ),
    ), progress=())
    with pytest.raises(MetadataDamagedError, match="atomic attempts are not counted"):
        validate_snapshot_shape(snapshot)


@pytest.mark.parametrize("bad", ["", "nope", "fp2:" + "0" * 64, "fp1:xyz"])
def test_unsupported_stored_fingerprint_format_is_damage(bad):
    snapshot = Snapshot(history=(success(1, "a", bad),), progress=())
    with pytest.raises(MetadataDamagedError, match="fingerprint"):
        validate_snapshot_shape(snapshot)


@pytest.mark.parametrize("field,value", [
    ("language", "perl"), ("mode", "eventual"), ("status", "FAILED"),
])
def test_corrupt_enumerated_field_is_damage(field, value):
    base = dict(
        seq=1, migration_id="a", fingerprint=fp("1"), first_fingerprint=fp("1"),
        language="python", mode="restartable", status=Status.SUCCESS, attempt=1,
        started_at=T0, last_attempt_at=T0, finished_at=T0,
    )
    base[field] = value
    with pytest.raises(MetadataDamagedError, match="unsupported"):
        validate_snapshot_shape(Snapshot(history=(HistoryRow(**base),), progress=()))


def test_non_positive_position_is_damage():
    snapshot = Snapshot(history=(success(0, "a", fp("1")),), progress=())
    with pytest.raises(MetadataDamagedError, match="non-positive position"):
        validate_snapshot_shape(snapshot)


# --- progress ownership -----------------------------------------------------------

def test_progress_attached_to_success_is_damage():
    snapshot = Snapshot(history=(success(1, "a", fp("1")),),
                        progress=(ProgressRow("a", "k", "v"),))
    with pytest.raises(MetadataDamagedError, match="attached to successful migration"):
        validate_snapshot_shape(snapshot)


def test_orphaned_progress_is_damage():
    snapshot = Snapshot(history=(active(1, "a", fp("1")),),
                        progress=(ProgressRow("ghost", "k", "v"),))
    with pytest.raises(MetadataDamagedError, match="unknown migration"):
        validate_snapshot_shape(snapshot)


def test_progress_for_a_non_active_migration_is_damage():
    snapshot = Snapshot(
        history=(success(1, "a", fp("1")), active(2, "b", fp("2"))),
        progress=(ProgressRow("a", "k", "v"),),
    )
    with pytest.raises(MetadataDamagedError, match="attached to successful migration"):
        validate_snapshot_shape(snapshot)


def test_empty_progress_value_is_damage():
    snapshot = Snapshot(history=(active(1, "a", fp("1")),),
                        progress=(ProgressRow("a", "k", ""),))
    with pytest.raises(MetadataDamagedError, match="empty value"):
        validate_snapshot_shape(snapshot)


# --- manifest disagreement (exit 2) ------------------------------------------------

def test_changed_successful_source_is_a_validation_failure():
    cap = capture(unit(1, "a", fingerprint=fp("9")))
    snapshot = Snapshot(history=(success(1, "a", fp("1")),), progress=())
    with pytest.raises(ValidationError, match="Successful history is immutable"):
        build_plan(cap, snapshot)


def test_renamed_successful_identity_is_a_validation_failure():
    cap = capture(unit(1, "renamed", fingerprint=fp("1")))
    snapshot = Snapshot(history=(success(1, "a", fp("1")),), progress=())
    with pytest.raises(ValidationError, match="identities and positions are permanent"):
        build_plan(cap, snapshot)


def test_changed_stored_mode_of_a_successful_row_is_a_validation_failure():
    cap = capture(unit(1, "a", mode=Mode.ATOMIC, fingerprint=fp("1")))
    snapshot = Snapshot(history=(success(1, "a", fp("1"), mode="restartable"),), progress=())
    with pytest.raises(ValidationError, match="execution mode is immutable"):
        build_plan(cap, snapshot)


def test_changed_stored_language_of_a_successful_row_is_a_validation_failure():
    cap = capture(unit(1, "a", language=Language.SQL, fingerprint=fp("1")))
    snapshot = Snapshot(history=(success(1, "a", fp("1"), language="python"),), progress=())
    with pytest.raises(ValidationError, match="recorded language"):
        build_plan(cap, snapshot)


def test_history_longer_than_the_manifest_is_a_validation_failure():
    cap = capture(unit(1, "a", fingerprint=fp("1")))
    snapshot = Snapshot(
        history=(success(1, "a", fp("1")), success(2, "b", fp("2"))), progress=()
    )
    with pytest.raises(ValidationError, match="not in the manifest"):
        build_plan(cap, snapshot)


def test_active_declared_atomic_in_the_manifest_is_a_validation_failure():
    cap = capture(unit(1, "a", mode=Mode.ATOMIC, fingerprint=fp("1")))
    snapshot = Snapshot(history=(active(1, "a", fp("1")),), progress=())
    with pytest.raises(ValidationError, match="restartable mode cannot change"):
        build_plan(cap, snapshot)


# --- recovery admission ------------------------------------------------------------

def test_unchanged_active_is_an_ordinary_retry():
    cap = capture(unit(1, "a", fingerprint=fp("1")))
    plan = build_plan(cap, Snapshot(history=(active(1, "a", fp("1")),), progress=()))
    require_no_recovery_needed(plan)


def test_changed_active_without_the_flag_reports_the_exact_command():
    cap = capture(unit(1, "a", fingerprint=fp("9")))
    plan = build_plan(cap, Snapshot(history=(active(1, "a", fp("1")),), progress=()))
    assert plan.active_fingerprint_changed
    with pytest.raises(RecoveryRequiredError) as info:
        require_no_recovery_needed(plan)
    message = str(info.value)
    assert fp("1") in message and fp("9") in message
    assert "migrate --recover a" in message
    assert info.value.exit_code == 2


def test_recover_requires_an_active_migration():
    cap = capture(unit(1, "a", fingerprint=fp("1")))
    plan = build_plan(cap, Snapshot(history=(), progress=()))
    with pytest.raises(ValidationError, match="requires an ACTIVE restartable migration"):
        check_recovery_admission(plan, "a")


def test_recover_with_the_wrong_id_is_refused():
    cap = capture(unit(1, "a", fingerprint=fp("9")), unit(2, "b"))
    plan = build_plan(cap, Snapshot(history=(active(1, "a", fp("1")),), progress=()))
    with pytest.raises(ValidationError, match="does not match the active migration"):
        check_recovery_admission(plan, "b")


def test_recover_never_admits_an_edit_to_a_successful_migration():
    cap = capture(unit(1, "a", fingerprint=fp("9")))
    snapshot = Snapshot(history=(success(1, "a", fp("1")),), progress=())
    with pytest.raises(ValidationError, match="Successful history is immutable"):
        build_plan(cap, snapshot)


def test_first_fingerprint_is_retained_across_a_recovery_edit():
    """The plan compares the current fingerprint, not the first one."""
    cap = capture(unit(1, "a", fingerprint=fp("3")))
    snapshot = Snapshot(
        history=(active(1, "a", fp("2"), attempt=2, first=fp("1")),), progress=()
    )
    plan = build_plan(cap, snapshot)
    assert plan.active_fingerprint_changed
    assert plan.active.first_fingerprint == fp("1")
    check_recovery_admission(plan, "a")


def test_first_and_latest_equality_does_not_prove_no_edit_occurred():
    """A to B and back to A leaves equal fingerprints (spec Section 8.2)."""
    cap = capture(unit(1, "a", fingerprint=fp("1")))
    snapshot = Snapshot(
        history=(active(1, "a", fp("1"), attempt=3, first=fp("1")),), progress=()
    )
    plan = build_plan(cap, snapshot)
    assert not plan.active_fingerprint_changed
    assert plan.active.attempt == 3  # only the counter hints that edits happened
