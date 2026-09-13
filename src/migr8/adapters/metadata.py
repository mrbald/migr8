"""Shared pieces of the logical metadata layout (spec Sections 8.2 and 8.4).

Physical types are adapter-owned.  What is shared is the logical column order,
the initialization-state classification, and the rule that nothing is ever
recreated once the completion marker exists.
"""

from __future__ import annotations

import re
from datetime import datetime

from ..model import (
    ACTIVE_INDEX,
    HISTORY_TABLE,
    META_TABLE,
    PROGRESS_TABLE,
    HistoryRow,
    MetadataReport,
    MetadataState,
    MetaRow,
    ProgressRow,
)

#: Logical history columns, in the order every adapter selects them.
HISTORY_COLUMNS = (
    "seq",
    "migration_id",
    "fingerprint",
    "first_fingerprint",
    "language",
    "mode",
    "status",
    "attempt",
    "started_at",
    "last_attempt_at",
    "finished_at",
    "runner_host",
    "runner_user",
    "runner_pid",
    "db_session",
    "tool_version",
)

PROGRESS_COLUMNS = ("migration_id", "prog_key", "prog_value", "updated_at")

META_COLUMNS = (
    "meta_key",
    "layout_version",
    "adapter",
    "lock_provider",
    "lock_binding",
    "target_namespace",
    "initialized_at",
)

#: Creation order.  Objects are created in this fixed order so an interruption
#: leaves a prefix that the next run can recognise and complete.
CREATION_ORDER = (HISTORY_TABLE, ACTIVE_INDEX, PROGRESS_TABLE, META_TABLE)


def history_row(values: tuple, parse_timestamp) -> HistoryRow:
    mapped = dict(zip(HISTORY_COLUMNS, values, strict=True))
    return HistoryRow(
        seq=int(mapped["seq"]),
        migration_id=mapped["migration_id"],
        fingerprint=mapped["fingerprint"],
        first_fingerprint=mapped["first_fingerprint"],
        language=mapped["language"],
        mode=mapped["mode"],
        status=mapped["status"],
        attempt=None if mapped["attempt"] is None else int(mapped["attempt"]),
        started_at=parse_timestamp(mapped["started_at"]),
        last_attempt_at=parse_timestamp(mapped["last_attempt_at"]),
        finished_at=parse_timestamp(mapped["finished_at"]),
        runner_host=mapped["runner_host"],
        runner_user=mapped["runner_user"],
        runner_pid=None if mapped["runner_pid"] is None else int(mapped["runner_pid"]),
        db_session=mapped["db_session"],
        tool_version=mapped["tool_version"],
    )


def progress_row(values: tuple, parse_timestamp) -> ProgressRow:
    mapped = dict(zip(PROGRESS_COLUMNS, values, strict=True))
    return ProgressRow(
        migration_id=mapped["migration_id"],
        prog_key=mapped["prog_key"],
        prog_value=mapped["prog_value"],
        updated_at=parse_timestamp(mapped["updated_at"]),
    )


def meta_row(values: tuple, parse_timestamp) -> MetaRow:
    mapped = dict(zip(META_COLUMNS, values, strict=True))
    return MetaRow(
        layout_version=int(mapped["layout_version"]),
        adapter=mapped["adapter"],
        lock_provider=mapped["lock_provider"],
        lock_binding=mapped["lock_binding"],
        target_namespace=mapped["target_namespace"],
        initialized_at=parse_timestamp(mapped["initialized_at"]),
    )


def normalise_definition(text: str, *, drop_parens: bool = False) -> str:
    """Collapse whitespace and quoting so a stored definition compares by meaning.

    Constraint and index definitions come back formatted by the database, and
    their generated names differ per installation, so the comparison is against
    the normalised text rather than the name.  PostgreSQL also parenthesises
    check conditions more than Oracle does, hence ``drop_parens``.
    """
    pattern = r'[\s"()]+' if drop_parens else r'[\s"]+'
    return re.sub(pattern, " ", (text or "").upper()).strip()


def parse_iso_timestamp(value: object) -> datetime | None:
    """Parse a database-produced ISO-8601 timestamp string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace(" ", "T"))


def classify(
    *,
    present: set[str],
    problems: list[str],
    meta: MetaRow | None,
    history_count: int | None,
    progress_count: int | None,
) -> MetadataReport:
    """Decide the initialization state (spec Section 8.4).

    ``problems`` already contains every definition incompatibility the adapter
    found.  ``history_count``/``progress_count`` are ``None`` when the table does
    not exist.
    """
    expected = set(CREATION_ORDER)
    missing = sorted(expected - present)
    present_sorted = tuple(sorted(present))

    if meta is not None:
        # The marker exists: the complete layout must exist and match.  Nothing
        # is recreated, and no object is altered to make validation pass.
        reasons = list(problems)
        if missing:
            reasons.append(
                "initialization marker is present but these objects are missing: "
                + ", ".join(missing)
            )
        if reasons:
            return MetadataReport(
                state=MetadataState.DAMAGED,
                meta=meta,
                problems=tuple(reasons),
                present_objects=present_sorted,
                missing_objects=tuple(missing),
            )
        return MetadataReport(
            state=MetadataState.COMPLETE,
            meta=meta,
            present_objects=present_sorted,
            missing_objects=(),
        )

    if not present:
        return MetadataReport(state=MetadataState.ABSENT, missing_objects=tuple(missing))

    reasons = list(problems)
    if history_count:
        reasons.append(
            f"{HISTORY_TABLE} contains {history_count} row(s) but the initialization marker "
            "is absent; this is damage, not an incomplete initialization"
        )
    if progress_count:
        reasons.append(
            f"{PROGRESS_TABLE} contains {progress_count} row(s) but the initialization marker "
            "is absent"
        )
    if reasons:
        return MetadataReport(
            state=MetadataState.DAMAGED,
            problems=tuple(reasons),
            present_objects=present_sorted,
            missing_objects=tuple(missing),
        )
    return MetadataReport(
        state=MetadataState.INCOMPLETE_COMPATIBLE,
        present_objects=present_sorted,
        missing_objects=tuple(missing),
    )


__all__ = [
    "HISTORY_COLUMNS",
    "PROGRESS_COLUMNS",
    "META_COLUMNS",
    "CREATION_ORDER",
    "history_row",
    "progress_row",
    "meta_row",
    "normalise_definition",
    "parse_iso_timestamp",
    "classify",
]
