"""Lexical scanning and statement normalisation (spec Sections 5.3 and 14.2 group 9)."""

from __future__ import annotations

import pytest

from migr8.errors import SqlSyntaxError
from migr8.sqltext import (
    StatementKind,
    TokenKind,
    classify,
    normalize,
    significant_tokens,
    tokenize,
)


def lead_words(sql: str) -> tuple[str, ...]:
    return normalize(sql).lead


# --- literals and comments -----------------------------------------------------

def test_literal_containing_a_semicolon_and_slash_is_preserved():
    sql = "INSERT INTO t (c) VALUES ('a;b/c');"
    statement = normalize(sql)
    assert statement.text == "INSERT INTO t (c) VALUES ('a;b/c')"
    assert statement.kind is StatementKind.SQL


def test_doubled_quote_escape_does_not_end_a_literal():
    sql = "UPDATE t SET c = 'it''s; fine' WHERE id = 1;"
    assert normalize(sql).text.endswith("WHERE id = 1")
    tokens = [t for t in tokenize(sql) if t.kind is TokenKind.STRING]
    assert [t.text for t in tokens] == ["'it''s; fine'"]


def test_line_comment_containing_a_terminator_is_not_a_statement_end():
    sql = "SELECT 1 FROM DUAL -- trailing ; and / here\n"
    statement = normalize(sql)
    assert statement.text.endswith("here")
    assert statement.lead[0] == "SELECT"


def test_block_comment_containing_a_terminator_is_preserved():
    sql = "SELECT /* ; and / inside */ 1 FROM DUAL;"
    assert "/* ; and / inside */" in normalize(sql).text


def test_block_comments_do_not_nest():
    sql = "SELECT /* outer /* inner */ 1 FROM DUAL"
    tokens = [t for t in tokenize(sql) if t.kind is TokenKind.COMMENT]
    assert tokens[0].text == "/* outer /* inner */"


def test_unterminated_block_comment_is_rejected():
    with pytest.raises(SqlSyntaxError, match="unterminated block comment"):
        normalize("SELECT 1 /* never closed")


def test_unterminated_string_is_rejected():
    with pytest.raises(SqlSyntaxError, match="unterminated string literal"):
        normalize("SELECT 'oops FROM DUAL")


def test_quoted_identifier_is_preserved():
    sql = 'SELECT "Odd;Name" FROM t;'
    assert normalize(sql).text == 'SELECT "Odd;Name" FROM t'


def test_unterminated_quoted_identifier_is_rejected():
    with pytest.raises(SqlSyntaxError, match="unterminated quoted identifier"):
        normalize('SELECT "Odd FROM t')


@pytest.mark.parametrize("literal", [
    "q'[a;b/c]'", "q'{a;b}'", "q'(a;b)'", "q'<a;b>'", "q'!a;b!'", "Q'#a;b#'",
    "nq'[unicode;]'", "NQ'{x}'", "n'plain;'",
])
def test_alternative_and_national_quoting_forms_are_scanned(literal):
    sql = f"INSERT INTO t (c) VALUES ({literal});"
    statement = normalize(sql)
    assert literal in statement.text
    assert statement.text.endswith(")")


def test_unterminated_alternative_quote_is_rejected():
    with pytest.raises(SqlSyntaxError, match="unterminated alternative-quoted"):
        normalize("INSERT INTO t (c) VALUES (q'[never closed);")


def test_alternative_quote_with_whitespace_delimiter_is_refused():
    with pytest.raises(SqlSyntaxError, match="whitespace as its delimiter"):
        normalize("SELECT q' x ' FROM DUAL")


# --- significant tokens, not line prefixes -------------------------------------

def test_multiline_update_whose_line_starts_with_set_is_valid_sql():
    sql = "UPDATE orders\nSET region = 'EU'\nWHERE id = 1;"
    statement = normalize(sql)
    assert statement.kind is StatementKind.SQL
    assert statement.lead[:2] == ("UPDATE", "ORDERS")


def test_plsql_exit_when_is_not_treated_as_a_client_command():
    sql = (
        "BEGIN\n"
        "  LOOP\n"
        "    EXIT WHEN TRUE;\n"
        "  END LOOP;\n"
        "END;"
    )
    statement = normalize(sql)
    assert statement.kind is StatementKind.PLSQL_BLOCK
    assert statement.text.endswith("END;")


def test_set_role_is_real_sql_but_sqlplus_set_is_refused():
    assert normalize("SET ROLE ALL;").lead[0] == "SET"
    with pytest.raises(SqlSyntaxError, match="unsupported SQL\\*Plus"):
        normalize("SET SERVEROUTPUT ON")


@pytest.mark.parametrize("command", [
    "SPOOL out.log", "DESCRIBE orders", "PROMPT hello", "CONNECT scott/tiger",
    "WHENEVER SQLERROR EXIT 1", "SHOW ERRORS", "VARIABLE x NUMBER", "EXECUTE p",
])
def test_top_level_sqlplus_commands_are_refused(command):
    with pytest.raises(SqlSyntaxError, match="unsupported SQL\\*Plus"):
        normalize(command)


def test_script_inclusion_is_refused():
    with pytest.raises(SqlSyntaxError, match="SQL\\*Plus command"):
        normalize("@other.sql")


# --- terminator handling --------------------------------------------------------

def test_one_trailing_terminator_is_removed_from_plain_sql():
    assert normalize("SELECT 1 FROM DUAL;").text == "SELECT 1 FROM DUAL"


def test_no_terminator_is_fine():
    assert normalize("SELECT 1 FROM DUAL").text == "SELECT 1 FROM DUAL"


def test_two_statements_in_one_file_are_refused():
    with pytest.raises(SqlSyntaxError, match="more than one statement"):
        normalize("SELECT 1 FROM DUAL; SELECT 2 FROM DUAL;")


def test_trailing_standalone_slash_is_an_end_of_file_convenience():
    sql = "CREATE OR REPLACE PROCEDURE p IS BEGIN NULL; END;\n/\n"
    statement = normalize(sql)
    assert statement.kind is StatementKind.PLSQL_DEFINITION
    assert statement.text.endswith("END;")
    assert "/" not in statement.text.splitlines()[-1]


def test_slash_is_not_a_statement_separator():
    with pytest.raises(SqlSyntaxError, match="more than one statement"):
        normalize("SELECT 1 FROM DUAL;\n/\nSELECT 2 FROM DUAL;")


def test_plsql_final_semicolon_is_preserved():
    sql = "BEGIN NULL; END;"
    assert normalize(sql).text == "BEGIN NULL; END;"


def test_plsql_without_final_semicolon_is_refused():
    with pytest.raises(SqlSyntaxError, match="must end with ';'"):
        normalize("BEGIN NULL; END")


def test_only_a_terminator_is_refused():
    with pytest.raises(SqlSyntaxError, match="only a terminator"):
        normalize(";")


def test_empty_sql_is_refused():
    with pytest.raises(SqlSyntaxError, match="no statement"):
        normalize("   \n-- just a comment\n")


def test_nul_byte_is_refused():
    with pytest.raises(SqlSyntaxError, match="NUL byte"):
        normalize("SELECT 1\x00")


# --- stored PL/SQL grammar -------------------------------------------------------

@pytest.mark.parametrize("header", [
    "CREATE PROCEDURE p IS BEGIN NULL; END;",
    "CREATE OR REPLACE PROCEDURE p IS BEGIN NULL; END;",
    "CREATE OR REPLACE EDITIONABLE FUNCTION f RETURN NUMBER IS BEGIN RETURN 1; END;",
    "CREATE NONEDITIONABLE PACKAGE pkg AS PROCEDURE p; END;",
    "CREATE OR REPLACE PACKAGE BODY pkg AS PROCEDURE p IS BEGIN NULL; END; END;",
    "CREATE OR REPLACE TRIGGER trg BEFORE INSERT ON t BEGIN NULL; END;",
    "CREATE TYPE ty AS OBJECT (x NUMBER);",
    "CREATE OR REPLACE TYPE BODY ty AS END;",
])
def test_stored_plsql_definitions_are_classified(header):
    assert classify(header) is StatementKind.PLSQL_DEFINITION


def test_create_library_is_not_a_plsql_body():
    sql = "CREATE OR REPLACE LIBRARY lib AS '/tmp/lib.so';"
    assert classify(sql) is StatementKind.SQL
    # Its single trailing terminator is therefore removed, unlike a package body.
    assert normalize(sql).text.endswith("'/tmp/lib.so'")


@pytest.mark.parametrize("sql", [
    "CREATE TABLE t (id NUMBER);",
    "CREATE UNIQUE INDEX i ON t (id);",
    "CREATE VIEW v AS SELECT 1 FROM DUAL;",
    "CREATE MATERIALIZED VIEW mv AS SELECT 1 FROM DUAL;",
    "CREATE SEQUENCE s;",
])
def test_other_create_statements_are_plain_sql(sql):
    assert classify(sql) is StatementKind.SQL


def test_declare_block_is_an_anonymous_plsql_block():
    sql = "DECLARE x NUMBER; BEGIN x := 1; END;"
    assert classify(sql) is StatementKind.PLSQL_BLOCK


def test_leading_comment_does_not_change_classification():
    sql = "-- a note\n/* another */\nBEGIN NULL; END;"
    assert classify(sql) is StatementKind.PLSQL_BLOCK
    assert lead_words(sql)[0] == "BEGIN"


def test_tokens_without_a_keyword_are_refused():
    with pytest.raises(SqlSyntaxError, match="recognisable keyword"):
        normalize("(1 + 2)")


def test_significant_tokens_exclude_comments():
    kinds = [t.kind for t in significant_tokens("-- c\nSELECT 1")]
    assert TokenKind.COMMENT not in kinds


# --- PostgreSQL dollar quoting ---------------------------------------------------

def test_dollar_quoted_body_protects_semicolons():
    sql = "DO $$ BEGIN INSERT INTO t VALUES (1); COMMIT; END $$"
    statement = normalize(sql)
    assert statement.kind is StatementKind.SQL
    assert statement.lead[0] == "DO"
    assert statement.text == sql
    strings = [t.text for t in tokenize(sql) if t.kind is TokenKind.STRING]
    assert strings == ["$$ BEGIN INSERT INTO t VALUES (1); COMMIT; END $$"]


def test_tagged_dollar_quoting_is_scanned():
    sql = "DO $body$ SELECT 'a;b'; $body$"
    strings = [t.text for t in tokenize(sql) if t.kind is TokenKind.STRING]
    assert strings == ["$body$ SELECT 'a;b'; $body$"]


def test_nested_different_dollar_tags_are_preserved():
    sql = "DO $outer$ BEGIN EXECUTE $inner$ x;y $inner$; END $outer$"
    assert normalize(sql).text == sql


def test_unterminated_dollar_quote_is_rejected():
    with pytest.raises(SqlSyntaxError, match="unterminated dollar-quoted"):
        normalize("DO $$ BEGIN NULL;")


def test_oracle_identifiers_containing_dollar_still_scan_as_words():
    sql = "SELECT sid FROM v$session WHERE audsid = 1"
    words = [t.text for t in tokenize(sql) if t.kind is TokenKind.WORD]
    assert "v$session" in words
    assert normalize(sql).lead[:2] == ("SELECT", "SID")


def test_dollar_quoted_trailing_terminator_is_still_removed():
    sql = "DO $$ BEGIN NULL; END $$;"
    assert normalize(sql).text == "DO $$ BEGIN NULL; END $$"
