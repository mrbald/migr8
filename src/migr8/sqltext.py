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

from .errors import SqlSyntaxError

#: Opening/closing delimiter pairs for Oracle alternative quoting, q'<d>...<d>'.
_ALT_QUOTE_PAIRS = {"[": "]", "{": "}", "(": ")", "<": ">"}

#: PostgreSQL dollar quoting: ``$$ ... $$`` or ``$tag$ ... $tag$``.  Oracle never
#: opens a token with ``$`` (it only appears inside identifiers such as
#: ``V$SESSION``), so recognising this form here does not change Oracle input.
_DOLLAR_TAG_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")

#: Top-level SQL*Plus / client commands.  Recognised only to produce a precise
#: "unsupported command" diagnostic; they are never sent to the server.
SQLPLUS_COMMANDS = frozenset({
    "@", "@@", "ACCEPT", "APPEND", "ARCHIVE", "ATTRIBUTE", "BREAK", "BTITLE",
    "CHANGE", "CLEAR", "COLUMN", "COMPUTE", "CONNECT", "COPY", "DEFINE",
    "DESC", "DESCRIBE", "DISCONNECT", "EDIT", "EXECUTE", "EXIT", "GET",
    "HELP", "HOST", "INPUT", "LIST", "PASSWORD", "PAUSE", "PRINT", "PROMPT",
    "QUIT", "RECOVER", "REMARK", "REPFOOTER", "REPHEADER", "RUN", "SAVE",
    "SET", "SHOW", "SHUTDOWN", "SPOOL", "STARTUP", "STORE", "TIMING",
    "TTITLE", "UNDEFINE", "VARIABLE", "WHENEVER", "XQUERY",
})

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
            end = _try_scan_dollar_quoted(sql, index)
            if end is not None:
                tokens.append(Token(TokenKind.STRING, sql[index:end], index, end))
                index = end
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
    upper = sql[start:start + 3].upper()
    if upper.startswith("NQ'"):
        return _scan_alt_quoted(sql, start, start + 3)
    if upper.startswith("Q'"):
        return _scan_alt_quoted(sql, start, start + 2)
    if upper.startswith("N'"):
        # A national character literal uses ordinary single-quote rules.
        return _scan_plain_string(sql, start + 1)
    return None


def _scan_alt_quoted(sql: str, start: int, body: int) -> int:
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
        raise SqlSyntaxError(
            f"unterminated alternative-quoted literal opened with q'{opener}"
        )
    return end + len(terminator)


def significant_tokens(sql: str) -> list[Token]:
    """Tokens with comments removed."""
    return [tok for tok in tokenize(sql) if tok.significant]


# --- Statement classification -------------------------------------------------

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
_PLSQL_DEFINITION_OBJECTS = frozenset({
    "PROCEDURE", "FUNCTION", "PACKAGE", "TRIGGER", "TYPE",
})

_CREATE_MODIFIERS = ("OR", "REPLACE", "EDITIONABLE", "NONEDITIONABLE", "EDITIONING")


@dataclass(frozen=True, slots=True)
class Statement:
    """One normalised, classified executable statement or block."""

    #: Text to submit to the driver, with terminator handling already applied.
    text: str
    kind: StatementKind
    #: Significant leading words, upper-cased, up to four.
    lead: tuple[str, ...]

    @property
    def first_token(self) -> str:
        return self.lead[0] if self.lead else ""

    @property
    def is_plsql(self) -> bool:
        return self.kind is not StatementKind.SQL


def classify(sql: str) -> StatementKind:
    """Classify ``sql`` without normalising it."""
    tokens = significant_tokens(sql)
    if not tokens:
        raise SqlSyntaxError("SQL text contains no statement")
    words = [tok.upper for tok in tokens if tok.kind is TokenKind.WORD]
    if not words:
        raise SqlSyntaxError(
            f"SQL text does not begin with a recognisable keyword (starts with "
            f"{tokens[0].text!r})"
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
    return text[line_start:token.start].strip() == "" and text[token.end:line_end].strip() == ""


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
        return len(significant) == 1 and significant[0].kind is TokenKind.PUNCT \
            and significant[0].text in (";", "/")

    if only_terminator():
        raise SqlSyntaxError("SQL text contains only a terminator")

    # Remove a single trailing end-of-file slash.
    last = significant[-1]
    if last.kind is TokenKind.PUNCT and last.text == "/" and _standalone_slash(text, last):
        text = text[:last.start] + text[last.end:]
        significant = significant_tokens(text)
        if not significant:
            raise SqlSyntaxError("SQL text contains no statement")
        if only_terminator():
            raise SqlSyntaxError("SQL text contains only a terminator")

    kind = classify(text)

    if kind is StatementKind.SQL:
        last = significant[-1]
        if last.kind is TokenKind.PUNCT and last.text == ";":
            text = text[:last.start] + text[last.end:]
            significant = significant_tokens(text)
        if any(tok.kind is TokenKind.PUNCT and tok.text == ";" for tok in significant):
            raise SqlSyntaxError(
                "SQL text appears to contain more than one statement; this tool executes "
                "one statement or block per SQL file and has no statement splitter"
            )
    else:
        last = significant[-1]
        if not (last.kind is TokenKind.PUNCT and last.text == ";"):
            raise SqlSyntaxError(
                "a PL/SQL block or stored definition must end with ';'"
            )

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

    words = tuple(tok.upper for tok in significant if tok.kind is TokenKind.WORD)[:4]
    first = significant[0]
    if first.kind is TokenKind.PUNCT and first.text == "@":
        raise SqlSyntaxError(
            "'@' script inclusion is a SQL*Plus command and is not supported"
        )
    if words and words[0] in SQLPLUS_COMMANDS and kind is StatementKind.SQL:
        if not _is_also_valid_sql(words):
            raise SqlSyntaxError(
                f"{words[0]} is an unsupported SQL*Plus or client command; "
                "this tool has no SQL*Plus interpreter"
            )
    return Statement(text=stripped, kind=kind, lead=words)


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
