"""Damaged metadata that the definition checks must not accept (spec Section 8.3).

Section 8.3 requires validating constraints, their participating columns and
expressions, their enabled/validated state, and the one-ACTIVE index's
uniqueness and usability. Each case here is damage a real database can be left
in: a key the server has stopped enforcing, a foreign key pointing somewhere
else, or an index whose expression admits one key per row and so enforces
nothing.

These drive the inspection code over dictionary rows shaped exactly as the live
servers return them; the renderings were recorded from Oracle Database 23ai Free
23.9.0.25.7 and PostgreSQL 17.5 on 2026-09-13. Live mutation of the real objects
is in ``tests/integration``.
"""

from __future__ import annotations

import pytest

from migr8.model import ACTIVE_INDEX, HISTORY_TABLE, META_TABLE, PROGRESS_TABLE

oracledb = pytest.importorskip("oracledb")
psycopg = pytest.importorskip("psycopg")

from migr8.adapters.oracle import _EXPECTED_CONSTRAINTS as _EXPECTED_ORACLE  # noqa: E402
from migr8.adapters.oracle import OracleAdapter  # noqa: E402
from migr8.adapters.postgres import _EXPECTED_CONSTRAINTS as _EXPECTED_PG  # noqa: E402
from migr8.adapters.postgres import PostgresAdapter  # noqa: E402

# --- Oracle ---------------------------------------------------------------------------

SCHEMA = "APP"

#: ``(type, status, validated, search_condition, columns, r_owner, r_table, r_columns)``,
#: exactly as the adapter selects them.  The search conditions are what
#: ALL_CONSTRAINTS held for the runner's own DDL on 23ai: Oracle stores the text
#: as written, newline and all.
HEALTHY_HISTORY_ROWS = [
    ("P", "ENABLED", "VALIDATED", None, "MIGRATION_ID", None, None, None),
    ("U", "ENABLED", "VALIDATED", None, "SEQ", None, None, None),
    # Oracle records each NOT NULL column as an ordinary check constraint.
    ("C", "ENABLED", "VALIDATED", '"SEQ" IS NOT NULL', "SEQ", None, None, None),
    ("C", "ENABLED", "VALIDATED", '"STATUS" IS NOT NULL', "STATUS", None, None, None),
    ("C", "ENABLED", "VALIDATED", "seq > 0", "SEQ", None, None, None),
    ("C", "ENABLED", "VALIDATED", "language IN ('sql','python')", "LANGUAGE", None, None, None),
    ("C", "ENABLED", "VALIDATED", "\"MODE\" IN ('atomic','restartable')", "MODE", None, None, None),
    ("C", "ENABLED", "VALIDATED", "status IN ('ACTIVE','SUCCESS')", "STATUS", None, None, None),
    ("C", "ENABLED", "VALIDATED", "attempt IS NULL OR attempt > 0", "ATTEMPT", None, None, None),
    (
        "C",
        "ENABLED",
        "VALIDATED",
        "status <> 'ACTIVE' OR (\"MODE\" = 'restartable'\n"
        "                       AND attempt IS NOT NULL AND finished_at IS NULL)",
        "ATTEMPT,FINISHED_AT,MODE,STATUS",
        None,
        None,
        None,
    ),
    (
        "C",
        "ENABLED",
        "VALIDATED",
        "status <> 'SUCCESS' OR finished_at IS NOT NULL",
        "FINISHED_AT,STATUS",
        None,
        None,
        None,
    ),
]

HEALTHY_PROGRESS_ROWS = [
    ("P", "ENABLED", "VALIDATED", None, "MIGRATION_ID,PROG_KEY", None, None, None),
    (
        "R",
        "ENABLED",
        "VALIDATED",
        None,
        "MIGRATION_ID",
        SCHEMA,
        HISTORY_TABLE.upper(),
        "MIGRATION_ID",
    ),
    (
        "C",
        "ENABLED",
        "VALIDATED",
        "LENGTH(prog_key) BETWEEN 1 AND 128",
        "PROG_KEY",
        None,
        None,
        None,
    ),
    ("C", "ENABLED", "VALIDATED", "LENGTH(prog_value) >= 1", "PROG_VALUE", None, None, None),
]

#: Exactly what ALL_IND_EXPRESSIONS returns for the supported index on 23ai: the
#: searched CASE in the DDL comes back rewritten as the simple form.
ORACLE_HEALTHY_EXPRESSION = "CASE \"STATUS\" WHEN 'ACTIVE' THEN 1 END "


class _FakeCursor:
    def __init__(self, answers):
        self._answers, self._rows = answers, []

    def execute(self, sql, **binds):
        for needle, rows in self._answers:
            if needle in sql:
                self._rows = rows
                return self
        raise AssertionError(f"unexpected query: {sql[:90]}")

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


def _oracle(answers):
    adapter = OracleAdapter.__new__(OracleAdapter)
    adapter._schema = SCHEMA
    adapter.metadata_schema = SCHEMA
    adapter._cursor = lambda: _FakeCursor(answers)
    return adapter


#: Oracle folds unquoted names, so the dictionary reports M8_HISTORY.
ORACLE_HISTORY = HISTORY_TABLE.upper()


def _oracle_index(expression, *, uniqueness="UNIQUE", status="VALID", columns=1, count=1):
    return _oracle(
        [
            ("FROM all_indexes", [(uniqueness, status, None, HISTORY_TABLE.upper())]),
            ("all_ind_expressions", [(expression,)] * count),
            ("FROM all_ind_columns", [(columns,)]),
        ]
    )


def test_oracle_accepts_the_index_the_ddl_creates():
    assert _oracle_index(ORACLE_HEALTHY_EXPRESSION)._index_problems({ACTIVE_INDEX}) == []


@pytest.mark.parametrize(
    ("label", "expression"),
    [
        # Mentions both required words, but keys on a per-row value, so every
        # ACTIVE row gets its own key and two of them never collide.
        ("keyed on seq", 'CASE "STATUS" WHEN \'ACTIVE\' THEN "SEQ" END '),
        ("keyed on the status column", '"STATUS"'),
        ("concatenation", "'ACTIVE' || \"STATUS\" "),
        ("inverted", "CASE \"STATUS\" WHEN 'SUCCESS' THEN 1 END "),
    ],
)
def test_oracle_refuses_an_index_expression_that_enforces_nothing(label, expression):
    problems = _oracle_index(expression)._index_problems({ACTIVE_INDEX})
    assert problems, f"{label} was accepted"
    assert ACTIVE_INDEX in problems[0]


def test_oracle_refuses_an_index_over_more_than_one_expression():
    adapter = _oracle_index(ORACLE_HEALTHY_EXPRESSION, columns=2, count=2)
    assert any("expression(s)" in problem for problem in adapter._index_problems({ACTIVE_INDEX}))


def test_oracle_accepts_the_healthy_constraint_layout():
    assert (
        _oracle([("FROM all_constraints", HEALTHY_HISTORY_ROWS)])._constraint_problems(
            {HISTORY_TABLE}
        )
        == []
    )
    assert (
        _oracle([("FROM all_constraints", HEALTHY_PROGRESS_ROWS)])._constraint_problems(
            {PROGRESS_TABLE}
        )
        == []
    )


@pytest.mark.parametrize(
    ("label", "status", "validated"),
    [
        ("disabled", "DISABLED", "NOT VALIDATED"),
        ("enabled but not validated", "ENABLED", "NOT VALIDATED"),
    ],
)
def test_oracle_refuses_a_key_the_server_is_not_enforcing(label, status, validated):
    rows = [
        ("P", status, validated, None, "MIGRATION_ID", None, None, None),
        *HEALTHY_HISTORY_ROWS[1:],
    ]
    problems = _oracle([("FROM all_constraints", rows)])._constraint_problems({HISTORY_TABLE})
    assert problems, f"a {label} primary key was accepted"
    assert f"{status}/{validated}" in problems[0]


def test_oracle_refuses_a_foreign_key_pointing_at_another_table():
    rows = [
        HEALTHY_PROGRESS_ROWS[0],
        ("R", "ENABLED", "VALIDATED", None, "MIGRATION_ID", SCHEMA, "OTHER_TABLE", "MIGRATION_ID"),
        *HEALTHY_PROGRESS_ROWS[2:],
    ]
    problems = _oracle([("FROM all_constraints", rows)])._constraint_problems({PROGRESS_TABLE})
    assert any("references APP.OTHER_TABLE" in problem for problem in problems)


def test_oracle_refuses_a_foreign_key_into_a_same_named_table_in_another_schema():
    """The target's owner is part of its identity, not a detail to drop."""
    rows = [
        HEALTHY_PROGRESS_ROWS[0],
        (
            "R",
            "ENABLED",
            "VALIDATED",
            None,
            "MIGRATION_ID",
            "OTHER_SCHEMA",
            HISTORY_TABLE.upper(),
            "MIGRATION_ID",
        ),
        *HEALTHY_PROGRESS_ROWS[2:],
    ]
    problems = _oracle([("FROM all_constraints", rows)])._constraint_problems({PROGRESS_TABLE})
    assert any("references OTHER_SCHEMA." in problem for problem in problems)


def test_oracle_refuses_a_foreign_key_with_no_resolvable_target():
    rows = [
        HEALTHY_PROGRESS_ROWS[0],
        ("R", "ENABLED", "VALIDATED", None, "MIGRATION_ID", None, None, None),
        *HEALTHY_PROGRESS_ROWS[2:],
    ]
    problems = _oracle([("FROM all_constraints", rows)])._constraint_problems({PROGRESS_TABLE})
    assert any("references nothing" in problem for problem in problems)


def _tautological(rows):
    """Append ``OR 1=1`` to every declared check, leaving the NOT NULL rows alone."""
    return [
        (*row[:3], f"{row[3]} OR 1=1", *row[4:])
        if row[0] == "C" and not str(row[3] or "").endswith('" IS NOT NULL')
        else row
        for row in rows
    ]


def test_oracle_refuses_a_check_constraint_made_tautological():
    """``OR 1=1`` leaves every required fragment present and enforces nothing."""
    rows = _tautological(HEALTHY_HISTORY_ROWS)
    problems = _oracle([("FROM all_constraints", rows)])._constraint_problems({HISTORY_TABLE})
    declared = len(_EXPECTED_ORACLE[HISTORY_TABLE]["checks"])
    # Every declared condition is reported twice: the tautology is unsupported
    # and the condition it replaced is missing.
    assert len(problems) == 2 * declared, problems
    assert sum("unsupported check constraint" in p for p in problems) == declared
    assert sum("missing the check constraint" in p for p in problems) == declared


def test_oracle_refuses_an_added_check_constraint():
    rows = [
        *HEALTHY_HISTORY_ROWS,
        ("C", "ENABLED", "VALIDATED", "seq < 5", "SEQ", None, None, None),
    ]
    problems = _oracle([("FROM all_constraints", rows)])._constraint_problems({HISTORY_TABLE})
    assert any("unsupported check constraint 'SEQ < 5'" in problem for problem in problems)


def test_oracle_refuses_a_check_constraint_it_cannot_read():
    rows = [
        *HEALTHY_HISTORY_ROWS,
        ("C", "ENABLED", "VALIDATED", "seq ~ 3", "SEQ", None, None, None),
    ]
    problems = _oracle([("FROM all_constraints", rows)])._constraint_problems({HISTORY_TABLE})
    assert any("not in a form this tool recognises" in problem for problem in problems)


def test_oracle_refuses_a_check_constraint_the_server_stopped_enforcing():
    rows = [
        (*row[:1], "DISABLED", "NOT VALIDATED", *row[3:]) if row[3] == "seq > 0" else row
        for row in HEALTHY_HISTORY_ROWS
    ]
    problems = _oracle([("FROM all_constraints", rows)])._constraint_problems({HISTORY_TABLE})
    assert any("DISABLED/NOT VALIDATED" in problem for problem in problems)


# --- PostgreSQL -----------------------------------------------------------------------

#: ``(indisunique, indisvalid, indisready, indexdef, table, indnatts, indpred, indkey names)``
PG_HEALTHY_PREDICATE = "((status)::text = 'ACTIVE'::text)"


class _FakeConnection:
    def __init__(self, answers):
        self._answers = answers

    def execute(self, sql, params=None):
        for needle, rows in self._answers:
            if needle in sql:
                return _FakeCursor([(needle, rows)]).execute(needle)
        raise AssertionError(f"unexpected query: {sql[:90]}")


PG_SCHEMA = "public"


def _postgres(answers):
    adapter = PostgresAdapter.__new__(PostgresAdapter)
    adapter._schema = PG_SCHEMA
    adapter.metadata_schema = PG_SCHEMA
    adapter._conn = _FakeConnection(answers)  # ``_db`` is a read-only property over this
    return adapter


def _pg_index(*, columns="status", predicate=PG_HEALTHY_PREDICATE, natts=1):
    definition = f"CREATE UNIQUE INDEX {ACTIVE_INDEX} ON public.{HISTORY_TABLE} USING btree (...)"
    return _postgres(
        [
            (
                "FROM pg_index",
                [(True, True, True, definition, HISTORY_TABLE, natts, predicate, columns)],
            )
        ]
    )


def test_postgres_accepts_the_index_the_ddl_creates():
    assert _pg_index()._index_problems({ACTIVE_INDEX}) == []


def test_postgres_refuses_an_index_keyed_on_another_column():
    """A predicate that names ACTIVE is not enough: the key must be the status."""
    problems = _pg_index(columns="seq")._index_problems({ACTIVE_INDEX})
    assert any("indexes (seq)" in problem for problem in problems)


def test_postgres_refuses_an_index_with_no_predicate():
    problems = _pg_index(predicate=None)._index_problems({ACTIVE_INDEX})
    assert any("not restricted" in problem for problem in problems)


def test_postgres_refuses_an_index_restricted_to_another_status():
    problems = _pg_index(predicate="((status)::text = 'SUCCESS'::text)")._index_problems(
        {ACTIVE_INDEX}
    )
    assert any("not restricted" in problem for problem in problems)


def test_postgres_refuses_a_multi_column_index():
    problems = _pg_index(columns="status,seq", natts=2)._index_problems({ACTIVE_INDEX})
    assert any("indexes (status,seq)" in problem for problem in problems)


#: ``(contype, convalidated, constraintdef, columns, r_schema, r_table, r_columns)``,
#: with the constraint definitions exactly as ``pg_get_constraintdef`` rendered
#: the runner's own DDL on PostgreSQL 17.5.
PG_HEALTHY_PROGRESS = [
    (
        "c",
        True,
        "CHECK (((char_length((prog_key)::text) >= 1) AND (char_length((prog_key)::text) <= 128)))",
        "prog_key",
        None,
        None,
        None,
    ),
    (
        "c",
        True,
        "CHECK ((char_length((prog_value)::text) >= 1))",
        "prog_value",
        None,
        None,
        None,
    ),
    (
        "p",
        True,
        "PRIMARY KEY (migration_id, prog_key)",
        "migration_id,prog_key",
        None,
        None,
        None,
    ),
    (
        "f",
        True,
        f"FOREIGN KEY (migration_id) REFERENCES {HISTORY_TABLE}(migration_id)",
        "migration_id",
        PG_SCHEMA,
        HISTORY_TABLE,
        "migration_id",
    ),
]

PG_HEALTHY_META = [
    ("p", True, "PRIMARY KEY (meta_key)", "meta_key", None, None, None),
    (
        "c",
        True,
        "CHECK (((meta_key)::text = 'singleton'::text))",
        "meta_key",
        None,
        None,
        None,
    ),
]


def test_postgres_accepts_the_healthy_constraint_layout():
    adapter = _postgres([("FROM pg_constraint", PG_HEALTHY_PROGRESS)])
    assert adapter._constraint_problems({PROGRESS_TABLE}) == []
    adapter = _postgres([("FROM pg_constraint", PG_HEALTHY_META)])
    assert adapter._constraint_problems({META_TABLE}) == []


def test_postgres_refuses_a_not_valid_foreign_key():
    rows = [
        *PG_HEALTHY_PROGRESS[:3],
        (*PG_HEALTHY_PROGRESS[3][:1], False, *PG_HEALTHY_PROGRESS[3][2:]),
    ]
    problems = _postgres([("FROM pg_constraint", rows)])._constraint_problems({PROGRESS_TABLE})
    assert any("NOT VALID" in problem for problem in problems)


def test_postgres_refuses_a_foreign_key_pointing_at_another_table():
    rows = [
        *PG_HEALTHY_PROGRESS[:3],
        (
            "f",
            True,
            "FOREIGN KEY (migration_id) REFERENCES other(id)",
            "migration_id",
            PG_SCHEMA,
            "other",
            "id",
        ),
    ]
    problems = _postgres([("FROM pg_constraint", rows)])._constraint_problems({PROGRESS_TABLE})
    assert any("references public.other(id)" in problem for problem in problems)


def test_postgres_refuses_a_foreign_key_into_a_same_named_table_in_another_schema():
    """The target's schema is part of its identity, not a detail to drop."""
    rows = [
        *PG_HEALTHY_PROGRESS[:3],
        (*PG_HEALTHY_PROGRESS[3][:4], "other_schema", HISTORY_TABLE, "migration_id"),
    ]
    problems = _postgres([("FROM pg_constraint", rows)])._constraint_problems({PROGRESS_TABLE})
    assert any("references other_schema." in problem for problem in problems)


def test_postgres_refuses_a_not_valid_primary_key():
    rows = [(*PG_HEALTHY_META[0][:1], False, *PG_HEALTHY_META[0][2:]), PG_HEALTHY_META[1]]
    problems = _postgres([("FROM pg_constraint", rows)])._constraint_problems({META_TABLE})
    assert any("primary key on (meta_key) is NOT VALID" in problem for problem in problems)


def test_postgres_refuses_a_check_constraint_made_tautological():
    rows = [
        (*row[:2], row[2][:-1] + " OR (1 = 1))", *row[3:]) if row[0] == "c" else row
        for row in PG_HEALTHY_META
    ]
    problems = _postgres([("FROM pg_constraint", rows)])._constraint_problems({META_TABLE})
    declared = len(_EXPECTED_PG[META_TABLE]["checks"])
    assert sum("unsupported check constraint" in p for p in problems) == declared
    assert sum("missing the check constraint" in p for p in problems) == declared


def test_postgres_refuses_an_added_check_constraint():
    rows = [*PG_HEALTHY_META, ("c", True, "CHECK ((layout_version > 99))", "x", None, None, None)]
    problems = _postgres([("FROM pg_constraint", rows)])._constraint_problems({META_TABLE})
    assert any("unsupported check constraint" in problem for problem in problems)


def test_postgres_refuses_a_check_constraint_it_cannot_read():
    rows = [*PG_HEALTHY_META, ("c", True, "CHECK (meta_key ~ 'x')", "meta_key", None, None, None)]
    problems = _postgres([("FROM pg_constraint", rows)])._constraint_problems({META_TABLE})
    assert any("not in a form this tool recognises" in problem for problem in problems)
