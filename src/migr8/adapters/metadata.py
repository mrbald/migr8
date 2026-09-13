"""Shared pieces of the logical metadata layout (spec Sections 8.2 and 8.4).

Physical types are adapter-owned.  What is shared is the logical column order,
the initialization-state classification, and the rule that nothing is ever
recreated once the completion marker exists.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import NamedTuple, TypedDict

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


class ExpectedKey(NamedTuple):
    """One primary, unique or foreign key the supported layout requires.

    ``kind`` and ``columns`` are spelled in each adapter's own dictionary
    vocabulary, so semantics are compared rather than database-generated
    constraint names.  ``columns`` is in key order, not table order.
    ``references`` is what a foreign key must point at: a key that exists but
    targets something else does not carry the relationship the layout depends on.
    """

    kind: str
    columns: str
    #: ``(logical table, comma-joined columns)``.  The adapter resolves the table
    #: to its own physical, schema-qualified name before comparing.  The schema
    #: is part of the comparison: a same-named history table in another
    #: namespace would otherwise satisfy the check while linking this
    #: namespace's progress rows to someone else's history.
    references: tuple[str, str] | None = None


class ExpectedConstraints(TypedDict):
    """The constraint shape one metadata table must have.

    ``keys`` lists the required keys.  ``checks`` is the *complete* set of check
    conditions the table may have, each spelled as
    :func:`canonical_condition` renders what this engine stores for it: a check
    that is missing, altered or added is damage.  Substring containment was not
    enough -- appending ``OR 1=1`` to every required condition leaves each
    fragment present while the constraint enforces nothing.
    """

    keys: tuple[ExpectedKey, ...]
    checks: tuple[str, ...]


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


def compact_definition(text: str, *, drop_parens: bool = False) -> str:
    """Normalise a stored definition to a whitespace- and quote-free comparison key.

    Dictionary views render the same expression with different spacing and
    parenthesisation across versions and engines, so a form the layout depends
    on exactly -- the one-ACTIVE index expression -- is compared with no
    whitespace at all rather than by guessing one rendering.
    """
    pattern = r'[\s"()]+' if drop_parens else r'[\s"]+'
    return re.sub(pattern, "", (text or "").upper())


#: One token of a stored CHECK condition.  Literals come first so a quote never
#: starts an identifier, and the multi-character operators come before the
#: single characters they start with.
_CONDITION_TOKEN = re.compile(
    r"'(?:[^']|'')*'"
    r'|"(?:[^"]|"")*"'
    r"|[A-Za-z_][A-Za-z0-9_$#]*"
    r"|\d+(?:\.\d+)*"
    r"|<>|!=|<=|>=|\|\||::"
    r"|[()\[\],.+\-*/%<>=]"
    r"|\s+"
)

#: A column NOT NULL constraint as Oracle stores it: an ordinary CHECK row.
#: Nullability is compared against the dictionary's own column layout, and
#: Oracle reports a column as nullable the moment such a constraint stops being
#: enforced, so these are recognised here instead of being restated column by
#: column in every expected set.
_NOT_NULL_CONDITION = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]* IS NOT NULL$", re.IGNORECASE)


def canonical_condition(text: object, *, fold: Callable[[str], str]) -> str | None:
    """Canonicalise one stored CHECK condition for an exact comparison.

    Whitespace and identifier spelling are folded -- an unquoted identifier
    through ``fold``, which is this engine's own case folding, and a quoted one
    by its exact content, which is what the two mean in SQL.  Everything else is
    kept token for token, parentheses included.

    String literals are preserved exactly.  ``'ACTIVE'`` and ``'active'`` are
    different values, so a comparison that upper-cased literal contents would
    accept a status column admitting values the engine never writes.

    Returns ``None`` when the text holds something this scanner does not
    recognise.  The caller reports that as damage: deciding SQL equivalence by
    inspection is how a check that enforces nothing gets accepted.
    """
    raw = text.read() if hasattr(text, "read") else text
    if raw is None:
        return None
    source = str(raw)
    tokens: list[str] = []
    position = 0
    while position < len(source):
        match = _CONDITION_TOKEN.match(source, position)
        if match is None:
            return None
        position = match.end()
        token = match.group()
        if token.isspace():
            continue
        if token.startswith("'"):
            tokens.append(token)
        elif token.startswith('"'):
            tokens.append(token[1:-1])
        else:
            tokens.append(fold(token))
    return " ".join(tokens) or None


def is_not_null_condition(condition: str) -> bool:
    """True for a canonical condition that only asserts a column is not NULL."""
    return bool(_NOT_NULL_CONDITION.match(condition))


def check_problems(
    table: str,
    rows: Iterable[tuple[object, bool, str]],
    expected: tuple[str, ...],
    *,
    fold: Callable[[str], str],
) -> list[str]:
    """Compare one table's check constraints against the complete supported set.

    ``rows`` gives ``(stored condition, whether the server enforces it, how the
    server describes that state)`` for every check constraint on the table.  The
    comparison is exact and the set is closed, so a missing condition, an
    altered one and an added one are all reported.  Nothing is altered to make
    it pass.

    The engine-owned rows in a metadata table are always valid against a check
    that enforces nothing, so the existing rows cannot stand in for this: only
    the definition says what the next write will be held to.
    """
    problems: list[str] = []
    seen: set[str] = set()
    for stored, enforced, state in rows:
        condition = canonical_condition(stored, fold=fold)
        if condition is None:
            problems.append(
                f"{table} has a check constraint whose condition is not in a form this tool "
                "recognises; no equivalence is assumed"
            )
            continue
        if is_not_null_condition(condition):
            continue  # Compared against the column layout instead.
        if condition not in expected:
            problems.append(
                f"{table} has an unsupported check constraint {condition!r}; the supported "
                "layout is fixed"
            )
            continue
        seen.add(condition)
        if not enforced:
            problems.append(f"{table} check constraint {condition!r} is {state}")
    problems.extend(
        f"{table} is missing the check constraint {condition!r}"
        for condition in expected
        if condition not in seen
    )
    return problems


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
    "CREATION_ORDER",
    "HISTORY_COLUMNS",
    "META_COLUMNS",
    "PROGRESS_COLUMNS",
    "ExpectedConstraints",
    "ExpectedKey",
    "canonical_condition",
    "check_problems",
    "classify",
    "compact_definition",
    "history_row",
    "is_not_null_condition",
    "meta_row",
    "normalise_definition",
    "parse_iso_timestamp",
    "progress_row",
]
