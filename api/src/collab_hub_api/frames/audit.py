"""The transaction-bound audited-execution primitive (issue #87, Gate E).

Every privileged action reaches the system through an interface that
authorizes *and* records it. This module is the **recording** half, and only
that: one context manager, :func:`audited`, that runs an already-authorized
mutation and its ``collab_audit_events`` row **in the same database
transaction**, committing or rolling back both together. Authorization policy
is the other half and lives in :mod:`.authorization` — the two are separate
concerns and must not be fused (an org owner and a platform operator both
perform some of the same recorded actions, so a single ``@operator_action``
decorator would either lock owners out or make its own check meaningless).

Why a context manager and not a decorator: the guarantee is the shared
transaction, not the syntax. The caller's mutation must run on **the same
connection** that writes the event row, so the primitive owns the connection
checkout and hands the body a :class:`GuardedConnection` — a handle that can
execute statements inside the transaction but **cannot complete it**. Three
layers stand between the body and a detached mutation:

1. **The Python surface is closed.** ``commit``/``rollback``/``close``/
   ``set_autocommit`` (or assignment to ``autocommit``) raise
   :class:`AuditTransactionViolation`; cursors come back wrapped so their
   ``.connection`` back-reference cannot hand out the raw connection; the
   body runs inside an explicit ``conn.transaction()`` block that only
   :func:`audited` itself can complete, so even the raw psycopg connection
   refuses a Python-level ``commit()``.
2. **Transaction-control SQL is screened.** psycopg only intercepts the
   *method* calls — ``execute("COMMIT")`` goes to the server as text — so
   the guard screens every statement (string literals, dollar-quoted
   bodies, identifiers, and comments blanked first, under **both** of
   PostgreSQL's quoting regimes; multi-statement strings split) and refuses
   statement-leading ``COMMIT``/``ROLLBACK`` (except ``ROLLBACK TO`` a
   savepoint)/``BEGIN``/``START``/``END``/``ABORT``/``PREPARE
   TRANSACTION``. A ``psycopg.sql`` composable is rendered to bytes once,
   screened, and *those same bytes* are what execute, so the screened text
   is exactly what the server receives. Residual imperfection, stated
   honestly: SQL hidden inside a ``DO`` body is blanked as a dollar-quoted
   literal, and a ``CALL``ed procedure's body is never seen at all — but
   PostgreSQL refuses transaction termination inside either when it runs in
   an explicit transaction block (``invalid transaction termination``),
   which this always is; a live test pins that.
3. **A backstop converts any unforeseen escape into a loud failure.**
   Immediately before the event insert, :func:`audited` asserts the
   connection is still inside the explicit transaction
   (``conn.info.transaction_status == INTRANS``); anything else raises
   :class:`AuditTransactionBrokenError`. It cannot un-commit a mutation,
   but it turns silent audit divergence into a named 500.

::

    with audited(db, ctx, "invitation.send", target_type="org", target_id=org_id) as ev:
        invitation = create_invitation(ev.conn, ...)   # same conn/transaction
        ev.detail = {"email_domain": domain_of(email)}  # redacted

On clean exit the event row is inserted on that connection and both writes
commit together; an exception anywhere — in the mutation, in the event
insert — rolls back both. A mutation without its event row, or an event row
without its mutation, is therefore impossible *for mutations that live in
this transaction*.

**The residual boundary, stated plainly.** Nothing here can extend the
guarantee to work the body does outside this transaction: a second, separate
``db.connection()`` checkout commits independently (do not open one inside a
body — there is no mechanical guard against it, only this contract and
review), and non-Postgres side effects (sending the SES email, writing S3)
are inherently outside any database transaction. Consumers (#89) must
sequence unrecoverable side effects **after** ``audited()`` returns, so a
rollback never has to un-send an email.

What must never appear in a row: secrets. ``detail`` is redacted by its
writer and must NEVER hold an invitation token or anything derived from one.
``actor``/``target_id`` are subs and opaque ids; the ``*_label`` fields exist
precisely so human-readable snapshots have a place that is *not* the
principal columns. All row fields are bounded and validated before the
insert — see :data:`AUDIT_LABEL_MAX_CHARS`, :data:`AUDIT_ID_MAX_CHARS`, and
:data:`AUDIT_DETAIL_MAX_BYTES`.

**Append-only is a convention, not an enforced boundary.** The application
role owns ``collab_audit_events`` (auto-migration creates it over the runtime
pool), so a ``REVOKE UPDATE, DELETE`` would prove today's ACL and nothing
more — a Postgres owner can re-grant to itself. The honest, testable claim is
that no application code path updates or deletes rows of this table; this
module is the only writer, it only INSERTs, and a test asserts the absence of
any other DML against the table across the source tree.
"""

from __future__ import annotations

import json
import string
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for annotations only
    from .auth import AuthContext

# The recorded actions of this beta, exactly as ratified (issue #87). The set
# is a closed vocabulary on purpose: a typo'd action string would create a
# row no runbook query ever finds, which is a silent audit gap. It is also a
# CHECK constraint on the table (migration version 2), so the vocabulary
# holds even for rows written from psql; widening it is a code change on the
# action's own issue PLUS an appended migration that replaces the constraint
# — a unit test pins the two lists together.
AUDIT_ACTION_INVITATION_SEND = "invitation.send"
AUDIT_ACTION_INVITATION_REDEEM = "invitation.redeem"
AUDIT_ACTION_INVITATION_REVOKE = "invitation.revoke"
AUDIT_ACTION_MEMBERSHIP_CREATE = "membership.create"
AUDIT_ACTION_ORG_CREATE = "org.create"
AUDIT_ACTION_ORG_RENAME = "org.rename"
AUDIT_ACTION_OPERATOR_MANUAL = "operator.manual"
# Appended by #180, which is what widening the set looks like: a constant here,
# membership in AUDIT_ACTIONS below, and migration **v5** replacing the table's
# CHECK — v2's DDL is released and therefore frozen text, so the constraint is
# replaced by an appended version rather than edited in place. A test reads the
# effective constraint (create plus every later replacement) back and pins it to
# AUDIT_ACTIONS, so widening one without the other fails at unit speed.
AUDIT_ACTION_SERVICE_ACCESS_GRANT = "service_access.grant"
# Appended by the platform-role sync, and widened the same way v5 was: two
# constants here, membership in AUDIT_ACTIONS below, and migration **v7**
# replacing the CHECK. Two actions rather than one because "who gained
# authority" and "who lost it" are the two questions asked of this log after an
# incident, and answering either from a single action plus a detail field means
# parsing JSON in a runbook query.
AUDIT_ACTION_PLATFORM_ROLE_GRANT = "platform_role.grant"
AUDIT_ACTION_PLATFORM_ROLE_REVOKE = "platform_role.revoke"
# The panel can take model access away as well as give it, and #180's
# `service_access.grant` had no counterpart because nothing could revoke.
AUDIT_ACTION_SERVICE_ACCESS_REVOKE = "service_access.revoke"
# Switching a connector on or off from the admin panel. Two actions rather than
# one with a flag, for the reason the platform-role pair gives: "who turned
# this off" is asked with a runbook query after an incident.
AUDIT_ACTION_CONNECTOR_ENABLE = "connector.enable"
AUDIT_ACTION_CONNECTOR_DISABLE = "connector.disable"
# Editing what invitees are told (#348). Two actions for the reason the pairs
# above give: "who changed the invitation email" and "who put it back" are
# separate questions after a bad message goes out. Widened the usual way:
# constants here, membership below, and migration **v18** replacing the CHECK,
# along with a new target type, since a template is none of the others.
AUDIT_ACTION_INVITATION_TEMPLATE_UPDATE = "invitation_template.update"
AUDIT_ACTION_INVITATION_TEMPLATE_RESET = "invitation_template.reset"

AUDIT_ACTIONS = frozenset(
    {
        AUDIT_ACTION_INVITATION_SEND,
        AUDIT_ACTION_INVITATION_REDEEM,
        AUDIT_ACTION_INVITATION_REVOKE,
        AUDIT_ACTION_MEMBERSHIP_CREATE,
        AUDIT_ACTION_ORG_CREATE,
        AUDIT_ACTION_ORG_RENAME,
        AUDIT_ACTION_OPERATOR_MANUAL,
        AUDIT_ACTION_SERVICE_ACCESS_GRANT,
        AUDIT_ACTION_PLATFORM_ROLE_GRANT,
        AUDIT_ACTION_PLATFORM_ROLE_REVOKE,
        AUDIT_ACTION_SERVICE_ACCESS_REVOKE,
        AUDIT_ACTION_CONNECTOR_ENABLE,
        AUDIT_ACTION_CONNECTOR_DISABLE,
        AUDIT_ACTION_INVITATION_TEMPLATE_UPDATE,
        AUDIT_ACTION_INVITATION_TEMPLATE_RESET,
    }
)

AUDIT_TARGET_INVITATION_TEMPLATE = "invitation_template"

AUDIT_TARGET_TYPES = frozenset({"org", "user", "invitation", "connector", AUDIT_TARGET_INVITATION_TEMPLATE})
"""The target vocabulary of this beta. Closed (and CHECK-constrained) for the
same reason as the action set: rows are found by exact-match runbook queries."""

AUDIT_LABEL_MAX_CHARS = 256
"""Bound on ``actor_label``/``target_label``. Labels are display snapshots
(an email is at most 254 characters); anything longer is not a label."""

AUDIT_ID_MAX_CHARS = 256
"""Bound on ``target_id``/``org_id``. Ids here are subs and opaque generated
values, all far shorter; the cap exists so a bug (or a hostile value that
reached a body) cannot turn the audit table into unbounded storage."""

AUDIT_DETAIL_MAX_BYTES = 4096
"""Bound on the serialized ``detail`` JSON. Detail is a redacted summary —
a handful of keys — not a payload archive."""


class AuditTransactionViolation(RuntimeError):
    """The action body tried to complete or abandon the audited transaction.

    Committing, rolling back, closing, or switching the connection to
    autocommit from inside the body — whether through the Python API or as
    transaction-control SQL text — would detach the mutation from its event
    row, the exact divergence this primitive exists to make impossible, so
    the attempt itself aborts the action and rolls everything back.
    """


class AuditTransactionBrokenError(RuntimeError):
    """The audited invariant was broken: the body escaped the transaction.

    Raised by the backstop check immediately before the event insert when the
    connection is no longer inside the explicit transaction :func:`audited`
    opened — meaning the body found a way past the guard and the SQL screen
    (or the transaction was ended out of band). By then a committed mutation
    cannot be undone; what this error guarantees is that the escape is a
    loud, named failure of the request instead of a privileged action that
    silently succeeded with no audit row.
    """


class UnknownAuditActionError(ValueError):
    """An action string outside :data:`AUDIT_ACTIONS` was offered for record."""


class UnknownAuditTargetTypeError(ValueError):
    """A target type outside :data:`AUDIT_TARGET_TYPES` was offered for record."""


class InvalidAuditFieldError(ValueError):
    """An event field was rewritten, oversized, or carried control characters."""


# --- Transaction-control SQL screening -------------------------------------
#
# psycopg's explicit transaction block intercepts the Python commit()/
# rollback() methods, not SQL text: execute("COMMIT") goes to the server and
# commits. The guard therefore screens every statement the body executes.
# Screening is textual, so it first blanks out everything that can *contain*
# the keywords without *being* them — string literals, dollar-quoted bodies,
# double-quoted identifiers, and both comment forms — then splits on ';' and
# inspects each statement's leading keywords. Only statement-leading keywords
# matter: PostgreSQL has no way to embed transaction control mid-statement. A
# DO block's body is a dollar-quoted literal and is blanked, and a CALLed
# procedure's body is not in the text at all — but PostgreSQL refuses
# transaction termination inside either when it runs in an explicit
# transaction block, which audited() always is.
#
# The blanking is a hand-written single-pass scanner rather than a union of
# regexes, because every hole found in this screen has been a *lexing*
# disagreement with the server, and only a scanner can follow the server's
# actual rules:
#
# - Block comments nest. `/* /* */ ' */ COMMIT` is one comment followed by a
#   real COMMIT. A non-greedy regex ends the comment at the first `*/`, and
#   re-running it to a fixpoint (the shape this code used to have) then reads
#   the leftover quote as a literal that swallows the COMMIT. The scanner
#   counts depth, as PostgreSQL's lexer does.
# - A prefix only introduces a token where it does not *continue an
#   identifier*, because identifiers admit letters, digits, `_` and `$` and
#   the lexer takes the longest match (PostgreSQL's lexical structure rules).
#   Two constructs here have such a prefix, and both were holes:
#     * `E'...'`. The server lexes `name'abc\'` as the identifier `name`
#       followed by an ordinary literal — a valid `name`-typed constant — so
#       treating every `[eE]'` as an E-string reads
#       `SELECT name'abc\'; COMMIT; SELECT name'x'` as one long literal and
#       lets the COMMIT through.
#     * `$tag$...$tag$`. `x$tag$` is simply the identifier `x$tag$`, so
#       `SELECT 1 AS x$tag$; COMMIT; SELECT $tag$foo$tag$;` is an aliased
#       select, a statement end, and a COMMIT — while a scanner that opens a
#       dollar quote at the first `$tag$` blanks the COMMIT away inside a
#       phantom body.
#   Both are verified live. The other quoted-literal prefixes need no such
#   rule: `U&'...'`, `B'...'` and `X'...'` never give backslash a meaning, so
#   reading them as plain literals is the server's own reading whether or not
#   the prefix was absorbed by an identifier, and `N'...'` is not a PostgreSQL
#   construct at all (it lexes as identifier plus literal, like `name'...'`).
# - Unterminated literals, comments and dollar quotes run to the end of the
#   text, exactly as the server's lexer treats them. Such a string is a
#   syntax error, and PostgreSQL parses the *whole* simple-query string
#   before executing any part of it, so nothing in it can run.
#
# Plain-literal semantics are the remaining trap, so the text is scanned
# under BOTH of PostgreSQL's quoting regimes and refused if EITHER exposes
# transaction control:
#
# - standard_conforming_strings = on (the server default): backslash is a
#   plain character in a '...' literal, which ends only via '' doubling.
#   A scanner that honored \' here would swallow real statements into a
#   phantom literal: `SELECT '\'; COMMIT; SELECT 'x'` ends its first literal
#   at the second quote and the server executes the COMMIT.
# - standard_conforming_strings = off (legacy): \' continues a '...'
#   literal, so the mirrored payload `SELECT '\', 'x; COMMIT; --'` is the
#   dangerous one there.
#
# `E'...'` honors backslash escapes under both regimes, and the two regimes
# can disagree only where a backslash occurs, so the legacy scan is skipped
# for text without one. Dual scanning is also what makes the GUC irrelevant:
# it may be off in the server's own configuration or the connection string,
# and a body may flip it mid-session (`SET standard_conforming_strings = off`
# screens clean — it is not transaction control, and refusing it would buy
# nothing against the other two routes) — but every statement is screened
# under both readings regardless, so no reading of the GUC helps an attacker.
# The cost is conservatism: text that is only a literal under one regime is
# refused if the other regime reads transaction control out of it. Refusing
# legal-but-ambiguous SQL is the right direction for an audit guard.
#
# Exactness versus over-refusal — a recorded decision, not a default. Every
# hole found here chose exactness at a lexer decision point, so refusing any
# statement whose text contains the exotic constructs at all (dollar quotes,
# E-strings, nested block comments) would retire that surface as a class.
# The exact lexer is kept deliberately: (1) plain literals, line comments and
# statement splitting must be lexed faithfully regardless — bodies genuinely
# contain them — so the scanner's irreducible core stays either way; (2) DO
# bodies are dollar-quoted, legitimate inside audited() (PostgreSQL refuses
# transaction termination within them in an explicit block, proven live),
# and psycopg composables render to arbitrary quoted text, so over-refusal
# has real casualties; and (3) the differential fuzz
# (test_audit_screen_differential_fuzz.py) now stands behind the exact lexer
# as a live CI gate, which is what the over-refusal argument was buying.
# Revisit toward over-refusal if audited() bodies remain hand-written
# INSERT/UPDATE only and the fuzz ever finds a round 6.


# The character classes below are PostgreSQL's (src/backend/parser/scan.l),
# not Python's. The server lexes *bytes* and admits every high-bit byte as an
# identifier character, so on a str the faithful translation is "any code
# point outside ASCII" — whatever Python thinks of it. `str.isalnum()` is
# False for a combining mark, a ZWJ, an emoji and NBSP, every one of which
# continues an identifier for the server: reading `SELECT 1 AS á$tag$;
# COMMIT; ...` (an `a` and a combining acute) as an opening dollar-quote
# rather than an identifier hides a statement-leading COMMIT, verified live.
# Working in code points is exact for what the lexer actually sees, because
# PostgreSQL converts the client encoding to the server encoding before
# parsing and a server encoding must be ASCII-safe — so a non-ASCII character
# is always exactly a run of high-bit bytes, never one with an ASCII byte in
# it.
_ASCII_IDENT_START = frozenset(string.ascii_letters + "_")
_ASCII_IDENT_CONT = frozenset(string.ascii_letters + string.digits + "_$")
_ASCII_TAG_CONT = frozenset(string.ascii_letters + string.digits + "_")


def _is_identifier_char(ch: str) -> bool:
    r"""scan.l ``ident_cont``: ``[A-Za-z\200-\377_0-9\$]``."""

    return ch in _ASCII_IDENT_CONT or ord(ch) >= 0x80


def _is_tag_start(ch: str) -> bool:
    r"""scan.l ``dolq_start``: ``[A-Za-z\200-\377_]`` — no digit, no dollar."""

    return ch in _ASCII_IDENT_START or ord(ch) >= 0x80


def _is_tag_char(ch: str) -> bool:
    r"""scan.l ``dolq_cont``: ``[A-Za-z\200-\377_0-9]`` — digits, but no dollar."""

    return ch in _ASCII_TAG_CONT or ord(ch) >= 0x80


def _end_of_quoted(text: str, opening: int, quote: str, *, backslash_escapes: bool) -> int:
    """Index just past the quoted run opened at *opening* (end of text if unterminated)."""

    i = opening + 1
    n = len(text)
    while i < n:
        ch = text[i]
        if backslash_escapes and ch == "\\":
            i += 2  # the escaped character cannot end the run
            continue
        if ch == quote:
            if i + 1 < n and text[i + 1] == quote:  # '' / "" doubling
                i += 2
                continue
            return i + 1
        i += 1
    return n


def _dollar_tag(text: str, start: int) -> str | None:
    """The ``$tag$`` (or ``$$``) opener at *start*, or None if it is not one."""

    n = len(text)
    i = start + 1
    if i < n and text[i] == "$":
        return "$$"
    if i < n and _is_tag_start(text[i]):
        i += 1
        while i < n and _is_tag_char(text[i]):
            i += 1
        if i < n and text[i] == "$":
            return text[start : i + 1]
    return None  # e.g. the `$1` of a positional parameter: an ordinary character


def _blank_noise(text: str, *, backslash_escapes_literals: bool) -> str:
    """Replace comments, literals and quoted identifiers with a space each.

    What is left is the statement skeleton: the text whose keywords are
    keywords. *backslash_escapes_literals* selects the quoting regime for
    plain ``'...'`` literals (see the note above); E-strings always honor
    backslash escapes.
    """

    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # Whether a prefix character can *start* a token depends on what came
        # before it, and the honest predecessor is the last character this
        # scanner emitted, not the last one in the text: after a blanked run
        # (a literal, a comment, a dollar-quoted body) the emitted space is
        # exactly the token boundary the server sees. `$a$x$a$$b$y$b$` is two
        # dollar-quoted strings on a real server, and reading the raw `$` of
        # the closing delimiter as "identifier continues" would get that
        # wrong.
        after_identifier = bool(out) and _is_identifier_char(out[-1])
        if text.startswith("--", i):
            # scan.l ends a line comment at the first `\n` OR `\r`
            # (`non_newline` is `[^\n\r]`): a lone CR really does end it, and
            # scanning only for `\n` blanks away whatever follows —
            # `SELECT 1; -- x\rCOMMIT; SELECT 2` runs the COMMIT (verified
            # live). The terminator itself stays, as a separator.
            ends = [pos for pos in (text.find("\n", i), text.find("\r", i)) if pos >= 0]
            i = min(ends) if ends else n
            out.append(" ")
        elif text.startswith("/*", i):
            depth = 1
            i += 2
            while i < n and depth:
                if text.startswith("/*", i):
                    depth += 1
                    i += 2
                elif text.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            out.append(" ")
        elif ch == "$" and not after_identifier and (tag := _dollar_tag(text, i)) is not None:
            # The closing delimiter is the exact tag, wherever it next occurs:
            # inside a dollar-quoted body nothing is lexed, so no separator
            # rule applies to the close (`$a$abc$ax$a$` is the string
            # `abc$ax`, confirmed against a live server).
            end = text.find(tag, i + len(tag))
            i = n if end < 0 else end + len(tag)
            out.append(" ")
        elif ch in "eE" and i + 1 < n and text[i + 1] == "'" and not after_identifier:
            i = _end_of_quoted(text, i + 1, "'", backslash_escapes=True)
            out.append(" ")
        elif ch == "'":
            i = _end_of_quoted(text, i, "'", backslash_escapes=backslash_escapes_literals)
            out.append(" ")
        elif ch == '"':
            i = _end_of_quoted(text, i, '"', backslash_escapes=False)
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# Statements that end, start, or abandon a transaction when they lead a
# statement. ROLLBACK and PREPARE need a second-token look: ROLLBACK TO
# (savepoint) is legitimate, PREPARE <name> AS (prepared statement) is not
# transaction control.
_DENIED_LEADING_KEYWORDS = frozenset({"COMMIT", "BEGIN", "START", "END", "ABORT"})


def _screen_skeleton(skeleton: str) -> None:
    # The last two character classes, and why Python's are safe here where
    # they were not in the scanner:
    #
    # - `str.split()` splits on Unicode whitespace; scan.l's `space` is
    #   `[ \t\n\r\f\v]`, a strict subset. So this can only cut a server token
    #   into more pieces, never fewer. If the server's leading token *is* the
    #   COMMIT keyword it is six ASCII letters containing no whitespace of
    #   either kind, so it survives here intact — the split can over-refuse
    #   (`; COMMIT` is one identifier to the server) but can never miss.
    # - `str.upper()` is Unicode-aware; the server's keyword lookup downcases
    #   ASCII only, so a token is the COMMIT keyword only if it is six ASCII
    #   letters, which `upper()` maps exactly. Again over-matching only
    #   (`commıt`, with a dotless i, uppercases into a refusal though the
    #   server sees an identifier).
    for statement in skeleton.split(";"):
        tokens = statement.split()
        if not tokens:
            continue
        head = tokens[0].upper()
        denied = (
            head in _DENIED_LEADING_KEYWORDS
            or (head == "ROLLBACK" and (len(tokens) < 2 or tokens[1].upper() != "TO"))
            or (head == "PREPARE" and len(tokens) > 1 and tokens[1].upper() == "TRANSACTION")
        )
        if denied:
            raise AuditTransactionViolation(
                f"the audited action body issued transaction-control SQL ({' '.join(tokens[:2])!r}); "
                "only audited() itself may complete the audited transaction — the mutation and its "
                "event row commit together or not at all"
            )


def _screen_sql(text: str) -> None:
    _screen_skeleton(_blank_noise(text, backslash_escapes_literals=False))
    if "\\" in text:
        # The regimes can only diverge where a backslash appears in the text.
        _screen_skeleton(_blank_noise(text, backslash_escapes_literals=True))


def _refuse(operation: str) -> AuditTransactionViolation:
    return AuditTransactionViolation(
        f"the audited action body called {operation} on the audited transaction; only "
        "audited() itself may complete it — the mutation and its event row commit together "
        "or not at all"
    )


class GuardedCursor:
    """A cursor whose road back to the raw connection is closed.

    Forwards the query-and-fetch surface a mutation needs; screens the SQL of
    ``execute``/``executemany`` exactly like the connection guard; and denies
    ``.connection`` — the back-reference through which a raw psycopg cursor
    hands out the unguarded connection. Anything else is an
    :class:`AttributeError`, so new cursor surface is allowed deliberately.
    """

    __slots__ = ("_cursor",)

    def __init__(self, cursor: Any) -> None:
        object.__setattr__(self, "_cursor", cursor)

    def execute(self, query, params=None, **kwargs):
        # Screened first, and *then* executed: what runs is the screened
        # rendering itself, never a second one (see _screened_query).
        screened = _screened_query(query, self._cursor)
        self._cursor.execute(screened, params, **kwargs)
        return self

    def executemany(self, query, params_seq, **kwargs):
        screened = _screened_query(query, self._cursor)
        return self._cursor.executemany(screened, params_seq, **kwargs)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchmany(self, size: int = 0):
        return self._cursor.fetchmany(size)

    def fetchall(self):
        return self._cursor.fetchall()

    def close(self) -> None:
        # Closing a cursor is harmless; the transaction outlives it.
        self._cursor.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._cursor.close()
        return False

    def __iter__(self):
        return iter(self._cursor)

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def rownumber(self):
        return self._cursor.rownumber

    @property
    def description(self):
        return self._cursor.description

    @property
    def statusmessage(self):
        return self._cursor.statusmessage

    @property
    def connection(self):
        raise _refuse(".connection  # the raw connection stays with audited()")

    def __getattr__(self, name: str):
        raise AttributeError(
            f"GuardedCursor does not expose {name!r}: the audited action body gets the "
            "execute/fetch surface only. If a mutation genuinely needs more cursor surface, "
            "allow it in GuardedCursor deliberately."
        )


class GuardedTransaction:
    """A nested (savepoint) transaction block without the ``.connection`` road back."""

    __slots__ = ("_transaction",)

    def __init__(self, transaction: Any) -> None:
        object.__setattr__(self, "_transaction", transaction)

    def __enter__(self):
        self._transaction.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._transaction.__exit__(*exc_info)

    @property
    def savepoint_name(self):
        return self._transaction.savepoint_name

    @property
    def connection(self):
        raise _refuse(".connection  # the raw connection stays with audited()")

    def __getattr__(self, name: str):
        raise AttributeError(f"GuardedTransaction does not expose {name!r}.")


class GuardedConnection:
    """The transaction handle :func:`audited` lends to the action body.

    Forwards exactly what a mutation needs — ``execute`` (screened, returning
    a :class:`GuardedCursor`), ``cursor`` (returning a :class:`GuardedCursor`),
    and ``transaction`` (a *nested* block, i.e. a savepoint, returned as a
    :class:`GuardedTransaction`) — and refuses everything that would end,
    abandon, or reconfigure the transaction: ``commit``, ``rollback``,
    ``close``, ``cancel``/``cancel_safe``, ``set_autocommit``/``autocommit``
    assignment, and **transaction-control SQL text** (see :func:`_screen_sql`)
    all raise :class:`AuditTransactionViolation`. Any other attribute is an
    :class:`AttributeError` naming this guard, so new psycopg surface has to
    be allowed here deliberately rather than leaking through.

    A guard can always be dug past with enough determination (``_conn`` is
    one underscore away); what stands behind it is the explicit
    ``transaction()`` block the body runs inside — psycopg refuses
    Python-level ``commit()``/``rollback()`` within one — and the
    transaction-status backstop :func:`audited` runs before the event insert,
    which turns any escape this guard did not foresee into a loud, named
    failure instead of a silent audit gap.
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: Any) -> None:
        object.__setattr__(self, "_conn", conn)

    def execute(self, query, params=None, **kwargs):
        screened = _screened_query(query, self._conn)
        return GuardedCursor(self._conn.execute(screened, params, **kwargs))

    def cursor(self, *args, **kwargs):
        return GuardedCursor(self._conn.cursor(*args, **kwargs))

    def transaction(self, *args, **kwargs):
        # Nested inside audited()'s own transaction block, psycopg makes this
        # a savepoint: the body may partially roll *itself* back, but cannot
        # complete or discard the outer transaction through it.
        return GuardedTransaction(self._conn.transaction(*args, **kwargs))

    def commit(self) -> None:
        raise _refuse("commit()")

    def rollback(self) -> None:
        raise _refuse("rollback()")

    def close(self) -> None:
        raise _refuse("close()")

    def cancel(self) -> None:
        raise _refuse("cancel()")

    def cancel_safe(self, *args, **kwargs) -> None:
        raise _refuse("cancel_safe()")

    def set_autocommit(self, value) -> None:
        raise _refuse("set_autocommit()")

    @property
    def autocommit(self) -> bool:
        return False

    @autocommit.setter
    def autocommit(self, value) -> None:
        raise _refuse("autocommit = ...  # set_autocommit")

    def __getattr__(self, name: str):
        raise AttributeError(
            f"GuardedConnection does not expose {name!r}: the audited action body gets execute/"
            "cursor/transaction only. If a mutation genuinely needs more psycopg surface, allow "
            "it in GuardedConnection deliberately."
        )


def _decoded_for_screening(raw: bytes) -> str:
    """The query bytes as text, or a refusal.

    Screening reads text, and the bytes must therefore be decodable. UTF-8 is
    both psycopg's default client encoding and Collab Hub's; a query that does not
    decode is refused rather than screened through a lossy reading, because
    the encodings where this bites (SJIS, GBK, Big5 — client-only in
    PostgreSQL) are precisely the ones where an ASCII ``'`` or ``\\`` can be
    the trailing byte of a multi-byte character, which is how a quote gets
    smuggled past a byte-level screen.
    """

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuditTransactionViolation(
            "the query on the audited transaction is not valid UTF-8, so it cannot be screened for "
            "transaction control; pass UTF-8 SQL text on the audited transaction"
        ) from exc


def _connection_encoding(context) -> str:
    """The encoding psycopg would encode a ``str`` query with.

    psycopg reaches it as ``Transformer.encoding``, which is the connection's
    ``client_encoding``; ``ConnectionInfo.encoding`` is the public spelling of
    the same value (a ``Connection`` hands out itself as ``.connection``, a
    ``Cursor`` its connection), and psycopg's own default with no connection
    to ask is UTF-8.
    """

    connection = getattr(context, "connection", None)
    info = getattr(connection, "info", None)
    return getattr(info, "encoding", None) or "utf-8"


def _encoded_for_execution(text: str, context) -> bytes:
    """The screened text as the bytes that will execute, or a refusal.

    Handing psycopg the ``str`` would leave one encoding step outside the
    screen: what executes would be bytes this module never saw. Encoding it
    here — with the encoding psycopg itself would use — closes that. The
    screen reads *characters*, which is also what the server's lexer sees
    (PostgreSQL converts the client encoding to the server encoding before
    parsing), so a non-UTF-8 ``client_encoding`` is fine on this path —
    unlike the bytes and composable paths, which have to reconstruct
    characters from bytes and refuse anything but UTF-8.

    A failed encode is the only thing that can go wrong and the only thing
    checked. (An earlier version also compared ``raw.decode(encoding)`` back
    against the text and presented it as a safeguard; encode-then-decode
    through one codec cannot disagree, so it asserted nothing and is gone.)
    """

    encoding = _connection_encoding(context)
    try:
        return text.encode(encoding)
    except (UnicodeEncodeError, LookupError) as exc:
        raise AuditTransactionViolation(
            f"the query on the audited transaction cannot be encoded as {encoding!r} for the "
            "connection, so what would execute is not what was screened; pass SQL text the "
            "connection's client_encoding can carry"
        ) from exc


def _refuse_foreign_composable(query) -> None:
    """Only psycopg.sql's own composables may be rendered for screening.

    Rendering calls the object's ``as_bytes``, which receives the *raw*
    connection as its adaptation context — a foreign ``Composable`` subclass
    could simply use it, or return different bytes on the second call. Both
    doors are shut here rather than reasoned about; ``Composed`` is walked
    because it renders its members.
    """

    from psycopg import sql

    allowed = (sql.SQL, sql.Composed, sql.Identifier, sql.Literal, sql.Placeholder)
    pending = [query]
    while pending:
        obj = pending.pop()
        if type(obj) not in allowed:
            raise AuditTransactionViolation(
                f"a query built from {type(obj).__name__} is not one of psycopg.sql's own composables; "
                "rendering it for screening would run its code against the audited connection. Pass "
                "plain SQL text or psycopg.sql composables on the audited transaction"
            )
        if type(obj) is sql.Composed:
            pending.extend(obj)


def _screened_query(query, context):
    """Screen *query* and return the exact form to hand to psycopg.

    psycopg turns each accepted query form into the bytes it sends
    (``PostgresQuery._ensure_bytes``: ``str`` is encoded, a ``Composable`` is
    rendered with ``as_bytes``, ``bytes`` is passed through). Screening has to
    see *those* bytes. Rendering a composable with ``as_string`` and executing
    it separately screens a different artifact than the one that runs — a
    hostile composable can return ``SELECT 1`` from ``as_string`` and
    ``COMMIT`` from ``as_bytes`` — and even ``as_bytes`` is only trustworthy
    if it is called once. So a composable is rendered here exactly as psycopg
    would render it, the rendered bytes are screened, and **those bytes** are
    what execute: what was screened is literally what the server receives.

    Every path therefore returns bytes, ``str`` included (see
    :func:`_encoded_for_execution`) — psycopg passes bytes through untouched,
    so no rendering or encoding step is left downstream of the screen.

    Anything psycopg would not accept as a query is refused outright — an
    unscreenable statement on the audited transaction is not worth the risk of
    being COMMIT.
    """

    if isinstance(query, str):
        _screen_sql(query)
        return _encoded_for_execution(query, context)
    if isinstance(query, (bytes, bytearray, memoryview)):
        raw = bytes(query)
        _screen_sql(_decoded_for_screening(raw))
        return raw

    from psycopg import sql

    if not isinstance(query, sql.Composable):
        raise AuditTransactionViolation(
            f"cannot screen a query of type {type(query).__name__} for transaction control; "
            "pass plain SQL text or a psycopg.sql composable on the audited transaction"
        )
    _refuse_foreign_composable(query)
    try:
        rendered = query.as_bytes(context)
    except Exception as exc:
        raise AuditTransactionViolation(
            f"could not render a {type(query).__name__} query for transaction-control "
            "screening; pass plain SQL text on the audited transaction"
        ) from exc
    if not isinstance(rendered, (bytes, bytearray, memoryview)):
        raise AuditTransactionViolation(
            f"a {type(query).__name__} query rendered to {type(rendered).__name__}, not bytes, so what "
            "would execute cannot be screened; pass plain SQL text on the audited transaction"
        )
    rendered = bytes(rendered)
    _screen_sql(_decoded_for_screening(rendered))
    return rendered


@dataclass
class PendingAuditEvent:
    """One privileged action mid-flight: the transaction, and the row to come.

    ``conn`` is the :class:`GuardedConnection` the body's mutation **must**
    use — a mutation issued on any other connection is outside the atomicity
    guarantee. The remaining fields become the ``collab_audit_events`` row
    when the body completes; ``detail``, ``target_id``, and ``target_label``
    are the ones a body typically fills in once the mutation has produced
    them.

    Every field is validated again at insert time — vocabulary, size, and
    control-character bounds — and the fields that identify *who did what,
    to what kind of thing, in what scope* (``action``, ``actor``,
    ``actor_label``, ``target_type``, ``org_id``) additionally may not
    differ from what :func:`audited` stamped: a body that rewrites them
    aborts the action. They are stamped rather than parameters of the body
    because the actor of an audited action is the authenticated caller and
    the action, target type, and scope are declared up front, never values
    the mutation picks. Only ``target_id``, ``target_label``, and ``detail``
    — the facts the mutation itself produces — may be filled in by the body.
    """

    conn: GuardedConnection
    action: str
    actor: str
    actor_label: str | None
    target_type: str | None = None
    target_id: str | None = None
    target_label: str | None = None
    org_id: str | None = None
    detail: dict | None = None
    event_id: int | None = field(default=None, init=False)
    """The inserted row's id, set after the body completes and before commit."""


def _validate_vocabulary(action: str, target_type: str | None) -> None:
    if action not in AUDIT_ACTIONS:
        raise UnknownAuditActionError(
            f"{action!r} is not a recorded audit action; the closed set is {sorted(AUDIT_ACTIONS)}. "
            "New actions are added to AUDIT_ACTIONS (and the table's CHECK constraint, via an "
            "appended migration) by the issue that introduces them."
        )
    if target_type is not None and target_type not in AUDIT_TARGET_TYPES:
        raise UnknownAuditTargetTypeError(
            f"{target_type!r} is not an audit target type; the closed set is {sorted(AUDIT_TARGET_TYPES)}."
        )


def _validate_text(name: str, value: str | None, max_chars: int) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise InvalidAuditFieldError(f"audit field {name} must be a string, not {type(value).__name__}")
    if len(value) > max_chars:
        raise InvalidAuditFieldError(f"audit field {name} exceeds {max_chars} characters ({len(value)})")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        # NUL would be refused by Postgres anyway; the rest would let a value
        # forge line breaks or escape sequences into a log a person reads.
        raise InvalidAuditFieldError(f"audit field {name} contains control characters")


def _serialized_detail(detail: dict | None) -> str | None:
    if detail is None:
        return None
    if not isinstance(detail, dict):
        raise InvalidAuditFieldError(f"audit detail must be a dict, not {type(detail).__name__}")
    payload = json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode()) > AUDIT_DETAIL_MAX_BYTES:
        raise InvalidAuditFieldError(
            f"audit detail exceeds {AUDIT_DETAIL_MAX_BYTES} bytes serialized; detail is a redacted "
            "summary, not a payload archive"
        )
    if "\\u0000" in payload or "\x00" in payload:
        # Postgres jsonb cannot represent NUL; refuse it as invalid input
        # rather than letting the insert fail with a bare DataError.
        raise InvalidAuditFieldError("audit detail contains a NUL character")
    return payload


def _assert_transaction_intact(conn) -> None:
    """The backstop behind the guard and the SQL screen.

    ``audited()`` opened an explicit transaction block, so at the moment the
    event row is about to be inserted the connection must report an active
    transaction (``INTRANS``). Anything else — idle after an escaped COMMIT,
    an error state, unknown — means the body broke the audited invariant in
    a way the guard did not foresee. A committed mutation cannot be undone
    from here; raising :class:`AuditTransactionBrokenError` guarantees the
    escape surfaces as a named failure of the request instead of a
    privileged action that silently succeeded without its audit row.
    """

    from psycopg import pq

    status = conn.info.transaction_status
    if status != pq.TransactionStatus.INTRANS:
        raise AuditTransactionBrokenError(
            f"the audited transaction is no longer active (transaction_status={status!r}); the "
            "action body ended or abandoned the transaction despite the guard. The audit row was "
            "NOT written; if the mutation was committed out of band it is now unrecorded and must "
            "be reconciled by hand (see the operator.manual runbook procedure)."
        )


def _validate_row(event: PendingAuditEvent) -> str | None:
    """Full field validation; returns the serialized detail JSON."""

    _validate_vocabulary(event.action, event.target_type)
    _validate_text("actor", event.actor, AUDIT_ID_MAX_CHARS)
    _validate_text("actor_label", event.actor_label, AUDIT_LABEL_MAX_CHARS)
    _validate_text("target_id", event.target_id, AUDIT_ID_MAX_CHARS)
    _validate_text("target_label", event.target_label, AUDIT_LABEL_MAX_CHARS)
    _validate_text("org_id", event.org_id, AUDIT_ID_MAX_CHARS)
    return _serialized_detail(event.detail)


@contextmanager
def audited(
    db,
    ctx: AuthContext,
    action: str,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    target_label: str | None = None,
    org_id: str | None = None,
    detail: dict | None = None,
) -> Iterator[PendingAuditEvent]:
    """Run an already-authorized mutation and its audit row in one transaction.

    ``db`` is a pooled :class:`~.db.PostgresDatabase`; ``ctx`` the caller's
    resolved :class:`~.auth.AuthContext` (**already authorized** — this
    primitive is deliberately unaware of either authority axis, see the module
    docstring). The body receives a :class:`PendingAuditEvent` and must issue
    its Postgres writes on ``ev.conn``, which cannot complete the transaction.

    ``org_id`` is explicit, never defaulted from the caller's organization: an
    operator's action may be hub-scoped (no organization) or aimed at an
    organization that is not their own, and a silently-defaulted wrong scope
    would be worse than a NULL. NULL means "not attributable to one
    organization". Once declared, the scope is fixed — the body cannot rewrite
    it, and the same holds for ``action`` and ``target_type``: the row always
    records the action that was declared, not one the body substituted.

    Failure semantics, which are the entire point:

    - the body raises → the transaction rolls back → **no mutation, no row**;
    - the body tries to commit/rollback/close/switch the connection →
      :class:`AuditTransactionViolation` → same rollback;
    - the event's fields fail validation (vocabulary, rewrite, size, control
      characters) → same rollback;
    - the event insert fails (constraint, connection loss) → same rollback;
    - the commit itself fails → both are gone together.

    Nothing is caught here. There is no best-effort mode: an audit write that
    may silently fail is not audit, and a log that can diverge from reality is
    worse than no log because it is trusted.
    """

    _validate_vocabulary(action, target_type)
    from psycopg.types.json import Json

    with db.connection() as conn:
        # An explicit transaction block: psycopg itself refuses commit() and
        # rollback() inside it, so even code that reaches past the guard to
        # the raw connection cannot complete the transaction — only this
        # context manager can, by exiting the block.
        with conn.transaction():
            event = PendingAuditEvent(
                conn=GuardedConnection(conn),
                action=action,
                actor=ctx.user,
                actor_label=ctx.display.email or ctx.display.name,
                target_type=target_type,
                target_id=target_id,
                target_label=target_label,
                org_id=org_id,
                detail=detail,
            )
            stamped_action = event.action
            stamped_actor = event.actor
            stamped_actor_label = event.actor_label
            stamped_target_type = event.target_type
            stamped_org_id = event.org_id
            yield event
            # The fields are mutable so the body can fill in what the
            # mutation produced — which also means it could have replaced
            # them. Everything is re-validated, and the fields that say who
            # did what, to what kind of thing, in what scope must be exactly
            # what was stamped before the body ran.
            for name, stamped, current in (
                ("action", stamped_action, event.action),
                ("actor", stamped_actor, event.actor),
                ("actor_label", stamped_actor_label, event.actor_label),
                ("target_type", stamped_target_type, event.target_type),
                ("org_id", stamped_org_id, event.org_id),
            ):
                if current != stamped:
                    raise InvalidAuditFieldError(
                        f"audited() {name} was rewritten by the action body ({stamped!r} -> "
                        f"{current!r}); the action, its target type, the actor, and the "
                        "organization scope are all fixed when the action is declared"
                    )
            detail_payload = _validate_row(event)
            # The backstop (layer 3): the event insert only happens into the
            # transaction the mutation ran in. If the body escaped the guard
            # and the SQL screen by a route not foreseen here, the connection
            # is no longer inside the explicit transaction block — and that
            # must be a loud, named failure, never a silently detached log.
            _assert_transaction_intact(conn)
            row = conn.execute(
                """
                INSERT INTO collab_audit_events
                    (actor, actor_label, action, target_type, target_id, target_label, org_id, detail)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    event.actor,
                    event.actor_label,
                    event.action,
                    event.target_type,
                    event.target_id,
                    event.target_label,
                    event.org_id,
                    Json(event.detail, dumps=lambda _obj: detail_payload) if detail_payload is not None else None,
                ),
            ).fetchone()
            event.event_id = row["id"]
