"""Oracle-aware lexical scanning of a single statement or block (spec Section 5.3).

There is deliberately no multi-statement splitter and no SQL*Plus interpreter.
The scanner exists to answer three narrow questions about one unit of SQL:

* what are its leading significant tokens, so mode admission can be checked;
* is it a stored PL/SQL definition or an anonymous block, so its final
  semicolon is preserved rather than stripped;
* does it contain a lexical form this implementation cannot classify safely,
  in which case it is rejected with a specific error.

Anything the scanner cannot classify is refused.  It never guesses, and it
never inspects line prefixes: a multiline ``UPDATE`` whose second line starts
with ``SET``, and a PL/SQL ``EXIT WHEN``, are ordinary valid input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum, auto
from itertools import pairwise

from .errors import SqlSyntaxError

#: Opening/closing delimiter pairs for Oracle alternative quoting, q'<d>...<d>'.
_ALT_QUOTE_PAIRS = {"[": "]", "{": "}", "(": ")", "<": ">"}

#: PostgreSQL dollar quoting: ``$$ ... $$`` or ``$tag$ ... $tag$``.  Oracle never
#: opens a token with ``$`` (it only appears inside identifiers such as
#: ``V$SESSION``), so recognising this form here does not change Oracle input.
_DOLLAR_TAG_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")

#: Top-level SQL*Plus / client commands.  Recognised only to produce a precise
#: "unsupported command" diagnostic; they are never sent to the server.
SQLPLUS_COMMANDS = frozenset(
    {
        "@",
        "@@",
        "ACCEPT",
        "APPEND",
        "ARCHIVE",
        "ATTRIBUTE",
        "BREAK",
        "BTITLE",
        "CHANGE",
        "CLEAR",
        "COLUMN",
        "COMPUTE",
        "CONNECT",
        "COPY",
        "DEFINE",
        "DESC",
        "DESCRIBE",
        "DISCONNECT",
        "EDIT",
        "EXECUTE",
        "EXIT",
        "GET",
        "HELP",
        "HOST",
        "INPUT",
        "LIST",
        "PASSWORD",
        "PAUSE",
        "PRINT",
        "PROMPT",
        "QUIT",
        "RECOVER",
        "REMARK",
        "REPFOOTER",
        "REPHEADER",
        "RUN",
        "SAVE",
        "SET",
        "SHOW",
        "SHUTDOWN",
        "SPOOL",
        "STARTUP",
        "STORE",
        "TIMING",
        "TTITLE",
        "UNDEFINE",
        "VARIABLE",
        "WHENEVER",
        "XQUERY",
    }
)


class TokenKind(StrEnum):
    WORD = auto()
    NUMBER = auto()
    STRING = auto()
    QUOTED_IDENT = auto()
    PUNCT = auto()
    COMMENT = auto()


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    #: Exact source text, including quotes and comment markers.
    text: str
    start: int
    end: int

    @property
    def upper(self) -> str:
        """Upper-cased text; meaningful for WORD and PUNCT tokens only."""
        return self.text.upper()

    @property
    def significant(self) -> bool:
        return self.kind is not TokenKind.COMMENT


def tokenize(sql: str) -> list[Token]:
    """Split ``sql`` into tokens, preserving literal and comment contents exactly.

    Raises :class:`SqlSyntaxError` for an unterminated literal, comment or
    quoted identifier, and for an alternative-quote form this scanner does not
    support.
    """
    tokens: list[Token] = []
    index = 0
    length = len(sql)
    while index < length:
        ch = sql[index]
        if ch in " \t\r\n\f\v":
            index += 1
            continue
        if ch == "-" and sql.startswith("--", index):
            end = sql.find("\n", index)
            end = length if end < 0 else end
            tokens.append(Token(TokenKind.COMMENT, sql[index:end], index, end))
            index = end
            continue
        if ch == "/" and sql.startswith("/*", index):
            # Oracle block comments do not nest.
            end = sql.find("*/", index + 2)
            if end < 0:
                raise SqlSyntaxError("unterminated block comment")
            end += 2
            tokens.append(Token(TokenKind.COMMENT, sql[index:end], index, end))
            index = end
            continue
        if ch == '"':
            end = _scan_quoted_ident(sql, index)
            tokens.append(Token(TokenKind.QUOTED_IDENT, sql[index:end], index, end))
            index = end
            continue
        if ch == "'":
            end = _scan_plain_string(sql, index)
            tokens.append(Token(TokenKind.STRING, sql[index:end], index, end))
            index = end
            continue
        if ch == "$":
            dollar_end = _try_scan_dollar_quoted(sql, index)
            if dollar_end is not None:
                tokens.append(Token(TokenKind.STRING, sql[index:dollar_end], index, dollar_end))
                index = dollar_end
                continue
        alt = _try_scan_prefixed_string(sql, index)
        if alt is not None:
            end = alt
            tokens.append(Token(TokenKind.STRING, sql[index:end], index, end))
            index = end
            continue
        if ch.isdigit() or (ch == "." and index + 1 < length and sql[index + 1].isdigit()):
            end = _scan_number(sql, index)
            tokens.append(Token(TokenKind.NUMBER, sql[index:end], index, end))
            index = end
            continue
        if _is_ident_start(ch):
            end = index + 1
            while end < length and _is_ident_part(sql[end]):
                end += 1
            tokens.append(Token(TokenKind.WORD, sql[index:end], index, end))
            index = end
            continue
        tokens.append(Token(TokenKind.PUNCT, ch, index, index + 1))
        index += 1
    return tokens


def _is_ident_start(ch: str) -> bool:
    # ':' is included so a driver bind placeholder scans as one token.  '&' is
    # not: SQL*Plus substitution is unsupported and must not hide inside a word.
    return ch.isalpha() or ch in "_$#:"


def _is_ident_part(ch: str) -> bool:
    return ch.isalnum() or ch in "_$#"


def _scan_number(sql: str, start: int) -> int:
    index = start
    length = len(sql)
    while index < length and (sql[index].isdigit() or sql[index] == "."):
        index += 1
    if index < length and sql[index] in "eE":
        probe = index + 1
        if probe < length and sql[probe] in "+-":
            probe += 1
        if probe < length and sql[probe].isdigit():
            index = probe
            while index < length and sql[index].isdigit():
                index += 1
    return index


def _scan_quoted_ident(sql: str, start: int) -> int:
    end = sql.find('"', start + 1)
    if end < 0:
        raise SqlSyntaxError("unterminated quoted identifier")
    return end + 1


def _scan_plain_string(sql: str, start: int) -> int:
    """Scan ``'...'`` honouring the doubled-quote escape."""
    index = start + 1
    length = len(sql)
    while index < length:
        if sql[index] == "'":
            if index + 1 < length and sql[index + 1] == "'":
                index += 2
                continue
            return index + 1
        index += 1
    raise SqlSyntaxError("unterminated string literal")


def _try_scan_dollar_quoted(sql: str, start: int) -> int | None:
    """Scan a PostgreSQL dollar-quoted string starting at ``start``.

    Returns ``None`` when the text is not an opening tag, so the caller can
    continue with ordinary token rules.  An opening tag with no matching closing
    tag is an unterminated literal and is rejected rather than guessed at.
    """
    match = _DOLLAR_TAG_RE.match(sql, start)
    if match is None:
        return None
    tag = match.group(0)
    end = sql.find(tag, match.end())
    if end < 0:
        raise SqlSyntaxError(f"unterminated dollar-quoted string opened with {tag}")
    return end + len(tag)


def _try_scan_prefixed_string(sql: str, start: int) -> int | None:
    """Scan ``N'..'``, ``Q'<d>..<d>'`` and ``NQ'<d>..<d>'`` forms.

    Returns ``None`` when the text at ``start`` is not one of those forms, so
    the caller can continue as an ordinary identifier.
    """
    upper = sql[start : start + 3].upper()
    if upper.startswith("NQ'"):
        return _scan_alt_quoted(sql, start + 3)
    if upper.startswith("Q'"):
        return _scan_alt_quoted(sql, start + 2)
    if upper.startswith("N'"):
        # A national character literal uses ordinary single-quote rules.
        return _scan_plain_string(sql, start + 1)
    return None


def _scan_alt_quoted(sql: str, body: int) -> int:
    if body >= len(sql):
        raise SqlSyntaxError("unterminated alternative-quoted literal")
    opener = sql[body]
    if opener in " \t\r\n":
        raise SqlSyntaxError(
            "alternative-quoted literal uses whitespace as its delimiter, which is not supported"
        )
    closer = _ALT_QUOTE_PAIRS.get(opener, opener)
    terminator = closer + "'"
    end = sql.find(terminator, body + 1)
    if end < 0:
        raise SqlSyntaxError(f"unterminated alternative-quoted literal opened with q'{opener}")
    return end + len(terminator)


def significant_tokens(sql: str) -> list[Token]:
    """Tokens with comments removed."""
    return [tok for tok in tokenize(sql) if tok.significant]


# --- Statement classification ---------------------------------------------------------------------


class StatementKind(StrEnum):
    #: An ordinary SQL statement; one trailing terminator may be removed.
    SQL = auto()
    #: An anonymous PL/SQL block (DECLARE/BEGIN); its final ';' is part of it.
    PLSQL_BLOCK = auto()
    #: A stored PL/SQL definition (CREATE [OR REPLACE] PROCEDURE/... ).
    PLSQL_DEFINITION = auto()


#: Object kinds whose CREATE form carries a PL/SQL body terminated by ';'.
#: ``LIBRARY`` is intentionally absent: it creates an object but is not a
#: PL/SQL body and must not be classified as one.
_PLSQL_DEFINITION_OBJECTS = frozenset(
    {
        "PROCEDURE",
        "FUNCTION",
        "PACKAGE",
        "TRIGGER",
        "TYPE",
    }
)

_CREATE_MODIFIERS = ("OR", "REPLACE", "EDITIONABLE", "NONEDITIONABLE", "EDITIONING")


@dataclass(frozen=True, slots=True)
class Statement:
    """One normalised, classified executable statement or block."""

    #: Text to submit to the driver, with terminator handling already applied.
    text: str
    kind: StatementKind
    #: Significant leading words, upper-cased, up to four.
    lead: tuple[str, ...]
    #: Every identifier the statement actually names, upper-cased: bare words
    #: plus quoted identifiers with their quotes removed.  Comments and string
    #: literals contribute nothing, so a name mentioned in prose is not a
    #: reference.  Callers match object names against this rather than scanning
    #: the raw text, where ``m8_history`` would also hit ``custom8_history``.
    #: The body of a ``DO`` statement is the exception: its identifiers are
    #: included, because the body is code that arrives as one string token.
    names: frozenset[str] = frozenset()
    #: Single-quoted strings that stand where an identifier belongs (after
    #: ``FROM``, ``INTO``, ``UPDATE``, ``JOIN``, ``TABLE`` and the like, or after
    #: a ``.``), upper-cased with the quotes removed.  SQLite reads such a string
    #: as a table name, so its adapter matches these like ``names``; elsewhere
    #: the same string is a literal (``TRIM(BOTH ' ' FROM 'x')``) and is ignored.
    string_names: frozenset[str] = frozenset()
    #: For a statement that starts with ``WITH``, the upper-cased verb that
    #: follows the common-table-expression list (``SELECT``, ``DELETE``, ...);
    #: empty for any other statement or when no such verb is found.
    cte_verb: str = ""
    #: For a statement that starts with ``WITH``, the upper-cased DML verbs
    #: (``INSERT``, ``UPDATE``, ``DELETE``, ``MERGE``) that open a CTE body:
    #: the first word inside ``AS (``, ``AS MATERIALIZED (`` or ``AS NOT
    #: MATERIALIZED (``.  PostgreSQL runs such a body as a data-modifying
    #: statement whatever the main verb is.
    cte_body_verbs: frozenset[str] = frozenset()

    @property
    def first_token(self) -> str:
        return self.lead[0] if self.lead else ""


#: Words after which a SQLite single-quoted string names an object.  The list
#: covers the object-naming positions of INSERT, REPLACE, UPDATE, DELETE, DROP,
#: ALTER, CREATE and joins; ``OR REPLACE`` and the other conflict words precede
#: the table in ``UPDATE OR ROLLBACK 't'``.
_IDENTIFIER_POSITION_WORDS = frozenset(
    {
        "FROM",
        "INTO",
        "UPDATE",
        "JOIN",
        "TABLE",
        "VIEW",
        "INDEX",
        "TRIGGER",
        "EXISTS",
        "ON",
        "TO",
        "REFERENCES",
        "REPLACE",
        "ROLLBACK",
        "ABORT",
        "FAIL",
        "IGNORE",
    }
)


def _string_value(token: Token) -> str | None:
    """The contents of a plain ``'...'`` token, or ``None`` for any other string."""
    if token.kind is not TokenKind.STRING or not token.text.startswith("'"):
        return None
    return token.text[1:-1].replace("''", "'")


def _strings_in_identifier_position(significant: list[Token]) -> frozenset[str]:
    found: set[str] = set()
    for previous, token in pairwise(significant):
        value = _string_value(token)
        if value is None:
            continue
        if (previous.kind is TokenKind.WORD and previous.upper in _IDENTIFIER_POSITION_WORDS) or (
            previous.kind is TokenKind.PUNCT and previous.text == "."
        ):
            found.add(value.upper())
    return frozenset(found)


def _identifier_words(tokens: list[Token]) -> set[str]:
    """Upper-cased bare words and quoted identifiers among ``tokens``."""
    names = {tok.upper for tok in tokens if tok.kind is TokenKind.WORD}
    names.update(tok.text[1:-1].upper() for tok in tokens if tok.kind is TokenKind.QUOTED_IDENT)
    return names


#: Verbs that can follow a ``WITH`` list.
_CTE_MAIN_VERBS = frozenset({"SELECT", "VALUES", "INSERT", "REPLACE", "UPDATE", "DELETE", "MERGE"})


def _cte_main_verb(significant: list[Token]) -> str:
    """The verb after the CTE list of a ``WITH`` statement, or ``""``.

    Each CTE body and column list sits inside parentheses, so the first of
    these verbs at parenthesis depth zero belongs to the main statement.  Names
    and the words ``RECURSIVE``, ``AS``, ``NOT`` and ``MATERIALIZED`` are never
    one of them unquoted.
    """
    depth = 0
    for token in significant[1:]:
        if token.kind is TokenKind.PUNCT:
            if token.text == "(":
                depth += 1
            elif token.text == ")":
                depth -= 1
        elif depth == 0 and token.kind is TokenKind.WORD and token.upper in _CTE_MAIN_VERBS:
            return token.upper
    return ""


#: Verbs that make a CTE body a data-modifying statement on PostgreSQL.
_CTE_BODY_DML_VERBS = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE"})


def _cte_body_verbs(significant: list[Token]) -> frozenset[str]:
    """The DML verbs that open a CTE body of a ``WITH`` statement.

    A body opens at a ``(`` at parenthesis depth zero that follows ``AS``,
    ``AS MATERIALIZED`` or ``AS NOT MATERIALIZED``; its first word is at depth
    one.  PostgreSQL accepts a data-modifying CTE only at the top level of the
    statement, so a deeper body is not searched.
    """
    found: set[str] = set()
    depth = 0
    for index, token in enumerate(significant):
        if token.kind is TokenKind.PUNCT and token.text == "(":
            if depth == 0 and _follows_as(significant, index):
                body = significant[index + 1] if index + 1 < len(significant) else None
                if (
                    body is not None
                    and body.kind is TokenKind.WORD
                    and body.upper in _CTE_BODY_DML_VERBS
                ):
                    found.add(body.upper)
            depth += 1
        elif token.kind is TokenKind.PUNCT and token.text == ")":
            depth -= 1
    return frozenset(found)


def _follows_as(significant: list[Token], index: int) -> bool:
    """True when ``AS``, ``AS MATERIALIZED`` or ``AS NOT MATERIALIZED`` ends just before *index*."""
    before = [
        token.upper if token.kind is TokenKind.WORD else ""
        for token in significant[max(0, index - 3) : index]
    ]
    return (
        before[-1:] == ["AS"]
        or before[-2:] == ["AS", "MATERIALIZED"]
        or before[-3:] == ["AS", "NOT", "MATERIALIZED"]
    )


#: Word characters for the fallback scan of a DO body the tokenizer refuses.
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*")


def _do_body_names(significant: list[Token]) -> set[str]:
    """Identifiers inside the body of a PostgreSQL ``DO`` statement.

    The body arrives as one string token, dollar-quoted or single-quoted, so its
    words are invisible to the token scan.  The body is tokenized again with the
    same rules; a body this scanner cannot tokenize (a backslash-escaped quote
    in an ``E'...'`` string, for example) contributes every word it contains,
    which errs towards refusing.  Strings inside the body are not examined, so dynamic SQL built
    from a string literal stays outside this guard.
    """
    names: set[str] = set()
    for token in significant:
        if token.kind is not TokenKind.STRING:
            continue
        if token.text.startswith("'"):
            body = token.text[1:-1].replace("''", "'")
        else:
            tag_end = token.text.index("$", 1) + 1
            body = token.text[tag_end : len(token.text) - tag_end]
        try:
            names |= _identifier_words(significant_tokens(body))
        except SqlSyntaxError:
            names.update(word.upper() for word in _WORD_RE.findall(body))
    return names


def classify(sql: str) -> StatementKind:
    """Classify ``sql`` without normalising it."""
    tokens = significant_tokens(sql)
    if not tokens:
        raise SqlSyntaxError("SQL text contains no statement")
    words = [tok.upper for tok in tokens if tok.kind is TokenKind.WORD]
    if not words:
        raise SqlSyntaxError(
            f"SQL text does not begin with a recognisable keyword (starts with {tokens[0].text!r})"
        )
    first = words[0]
    if first in ("DECLARE", "BEGIN"):
        return StatementKind.PLSQL_BLOCK
    if first == "CREATE":
        index = 1
        while index < len(words) and words[index] in _CREATE_MODIFIERS:
            index += 1
        if index < len(words) and words[index] in _PLSQL_DEFINITION_OBJECTS:
            # CREATE TYPE ... AS OBJECT / TABLE OF has no PL/SQL body, but it
            # is still terminated the same way in a file, and Oracle accepts
            # the specification text as submitted.  Treat the whole family as
            # a definition so no terminator is removed from a body.
            return StatementKind.PLSQL_DEFINITION
        return StatementKind.SQL
    return StatementKind.SQL


def _standalone_slash(text: str, token: Token) -> bool:
    """True when ``token`` is a ``/`` alone on its line, i.e. a script separator."""
    line_start = text.rfind("\n", 0, token.start) + 1
    line_end = text.find("\n", token.end)
    line_end = len(text) if line_end < 0 else line_end
    return text[line_start : token.start].strip() == "" and text[token.end : line_end].strip() == ""


def normalize(sql: str) -> Statement:
    """Normalise one statement for submission (spec Section 5.3).

    * A final standalone ``/`` outside literals and comments is accepted as an
      end-of-file convenience and removed.  It is not a statement separator, so
      any *other* standalone ``/`` is refused rather than split on.
    * For a non-PL/SQL statement, at most one trailing ``;`` outside comments
      and literals is removed; a remaining bare ``;`` means the file holds more
      than one statement and is refused.
    * A PL/SQL block or stored definition keeps its final ``;``.
    """
    if "\x00" in sql:
        raise SqlSyntaxError("SQL text contains a NUL byte")
    text = sql
    significant = significant_tokens(text)
    if not significant:
        raise SqlSyntaxError("SQL text contains no statement")

    def only_terminator() -> bool:
        return (
            len(significant) == 1
            and significant[0].kind is TokenKind.PUNCT
            and significant[0].text in (";", "/")
        )

    if only_terminator():
        raise SqlSyntaxError("SQL text contains only a terminator")

    # Remove a single trailing end-of-file slash.
    last = significant[-1]
    if last.kind is TokenKind.PUNCT and last.text == "/" and _standalone_slash(text, last):
        text = text[: last.start] + text[last.end :]
        significant = significant_tokens(text)
        if not significant:
            raise SqlSyntaxError("SQL text contains no statement")
        if only_terminator():
            raise SqlSyntaxError("SQL text contains only a terminator")

    kind = classify(text)

    if kind is StatementKind.SQL:
        last = significant[-1]
        if last.kind is TokenKind.PUNCT and last.text == ";":
            text = text[: last.start] + text[last.end :]
            significant = significant_tokens(text)
        if any(tok.kind is TokenKind.PUNCT and tok.text == ";" for tok in significant):
            raise SqlSyntaxError(
                "SQL text appears to contain more than one statement; this tool executes "
                "one statement or block per SQL file and has no statement splitter"
            )
    else:
        last = significant[-1]
        if not (last.kind is TokenKind.PUNCT and last.text == ";"):
            raise SqlSyntaxError("a PL/SQL block or stored definition must end with ';'")

    # Any remaining standalone slash was a separator attempt, not an operator:
    # division never occupies a whole line by itself.
    for tok in significant:
        if tok.kind is TokenKind.PUNCT and tok.text == "/" and _standalone_slash(text, tok):
            raise SqlSyntaxError(
                "a standalone '/' is an end-of-file convenience, not a statement separator; "
                "this tool executes one statement or block per SQL file"
            )

    stripped = text.strip()
    if not stripped:
        raise SqlSyntaxError("SQL text is empty after normalisation")

    word_tokens = [tok.upper for tok in significant if tok.kind is TokenKind.WORD]
    words = tuple(word_tokens)[:4]
    # Quoted identifiers are folded to upper case here as well.  A lower-case
    # quoted name is a different object on Oracle, so treating both forms as a
    # reference is the conservative direction.
    name_set = _identifier_words(significant)
    if words and words[0] == "DO":
        name_set |= _do_body_names(significant)
    names = frozenset(name_set)
    string_names = _strings_in_identifier_position(significant)
    first = significant[0]
    if first.kind is TokenKind.PUNCT and first.text == "@":
        raise SqlSyntaxError("'@' script inclusion is a SQL*Plus command and is not supported")
    if (
        words
        and words[0] in SQLPLUS_COMMANDS
        and kind is StatementKind.SQL
        and not _is_also_valid_sql(words)
    ):
        raise SqlSyntaxError(
            f"{words[0]} is an unsupported SQL*Plus or client command; "
            "this tool has no SQL*Plus interpreter"
        )
    is_with = bool(words) and words[0] == "WITH"
    return Statement(
        text=stripped,
        kind=kind,
        lead=words,
        names=names,
        string_names=string_names,
        cte_verb=_cte_main_verb(significant) if is_with else "",
        cte_body_verbs=_cte_body_verbs(significant) if is_with else frozenset(),
    )


def _is_also_valid_sql(words: tuple[str, ...]) -> bool:
    """Distinguish a real SQL statement from a same-named SQL*Plus command.

    ``SET`` heads the SQL*Plus ``SET`` command but also the SQL statements
    ``SET ROLE`` and ``SET CONSTRAINT(S)``; ``SET TRANSACTION`` is real SQL too
    but is rejected elsewhere as transaction control.
    """
    if not words:
        return False
    if words[0] == "SET":
        return len(words) > 1 and words[1] in ("ROLE", "CONSTRAINT", "CONSTRAINTS", "TRANSACTION")
    if words[0] == "EXECUTE":
        # EXECUTE IMMEDIATE only exists inside PL/SQL, which classifies as a block.
        return False
    return False
