"""The keyed claim: one step attempt acts once, however often it is invoked.

A step can be invoked again after it already acted: a durable backend resuming
after a crash, or someone retrying an interrupted ``none`` run. Every
interaction carries an idempotency key, stable for one step attempt
(``{run}:{step}:{attempt}``, each component percent-escaped), and this store
keeps what became of that key. A worker *reserves* the key before it acts and
*commits* its envelope after; a replay of a committed key is answered with the
envelope, and nothing acts again. A replay that finds the key reserved by a
worker that never committed, one lost between its side effect and its result,
must not act either: the claim moves to ``outcome_unknown``, the step fails
``outcome-unknown``, and only a person, or an entry point the Cog declares
idempotent, reconciles it. The claim narrows the crash window to the gap
between reserve and commit and makes it visible; it does not pretend to close
it (ADR-0002 D1).

The states are the step attempt machine's (``states/step.py``): ``reserved``,
``committed``, ``outcome_unknown`` and, once a person reconciled an unknown
outcome, ``recorded``; every move goes through that machine. Three stores
keep them behind one protocol and one conformance suite, the shape the Track
has: in memory, SQLite beside the SQLite Track, and Postgres beside the hub's,
whose table comes from the hub's migrations. The hub serves them to workers as
``/v1/claims``, authorized by the run token and scoped to that run's own keys.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from .states import InvalidTransition
from .states.step import STEP_ATTEMPT, StepAttempt

CLAIM_RETENTION = timedelta(days=30)
"""How long a claim is kept after it last changed. An attempt's claim matters until the attempt's outcome
is on the Track, which a retry or a durable resume reaches long before this; what outlives it is history."""

RESERVED, COMMITTED, OUTCOME_UNKNOWN, RECORDED = "reserved", "committed", "outcome_unknown", "recorded"


def key_component(value: str) -> str:
    """Percent-escape the key delimiter so idempotency keys are injective.

    The key is ``{run_id}:{step}:{attempt}``; without escaping, ``run="a:b"
    step="c"`` and ``run="a" step="b:c"`` collide. Escaping ``:`` (and ``%``, so
    the escaping itself round-trips) keeps distinct (run, step) pairs distinct,
    the contract the claim is built on, while leaving keys readable whenever
    the ids contain no ``:``.
    """
    return value.replace("%", "%25").replace(":", "%3A")


def run_of(key: str) -> str | None:
    """The run an idempotency key belongs to, read from its first component; ``None`` for a key of no run.

    The hub reads it to find the Track the worker's run token is verified
    against, so a token reaches only its own run's keys.
    """
    if not key or ":" not in key:
        return None
    first = key.split(":", 1)[0]
    return first.replace("%3A", ":").replace("%25", "%") or None


class ClaimRefused(ValueError):
    """A commit, or a reconciliation, the claim's state does not allow: never reserved, reserved by another
    worker, or already past the state the move leaves."""


@dataclass(frozen=True, slots=True)
class Claim:
    """What became of one idempotency key."""

    run_id: str
    key: str
    state: str
    """``reserved``, ``committed``, ``outcome_unknown`` or ``recorded``: the step attempt machine's states."""
    worker: str | None
    """The worker holding the reservation, by the sha256 of its run token; what a replay is told apart by."""
    reserved_at: datetime
    updated_at: datetime
    envelope: Mapping[str, Any] | None = None
    """The result envelope the worker committed, returned to every replay of the key."""
    committed_at: datetime | None = None
    actor: str | None = None
    """Who reconciled an unknown outcome, when a person did."""

    def held_by(self, worker: str) -> bool:
        return self.state == RESERVED and self.worker == worker


class ClaimStore(Protocol):
    """Reserve, commit and read keyed claims; every store passes ``test_claims_conformance.py``."""

    def reserve(self, run_id: str, key: str, *, worker: str) -> tuple[Claim, bool]:
        """Reserve ``key`` for ``worker`` before it acts; the claim, and whether the worker is to act.

        A key nobody holds is reserved for the worker, and so is one the same
        worker reserved before (its request was answered and the answer lost):
        both are its to act on. A key reserved by another worker is that
        worker's lost outcome: the claim moves to ``outcome_unknown`` and the
        caller must not act. A committed key, and an unknown or reconciled one,
        are returned as they are, and the caller does not act either.
        """

    def commit(self, run_id: str, key: str, *, worker: str, envelope: Mapping[str, Any]) -> Claim:
        """Commit the envelope of the key ``worker`` holds; :class:`ClaimRefused` for a key it does not.

        Committing a key the same worker committed already is answered with the
        claim as it is, since a worker whose first commit was answered and lost
        commits again.
        """

    def get(self, run_id: str, key: str) -> Claim | None:
        """The claim under ``key`` for ``run_id``, or ``None``."""

    def reconcile(self, run_id: str, key: str, *, actor: str) -> Claim:
        """A person settles an unknown outcome: the claim is ``recorded`` and names them. :class:`ClaimRefused`
        for a claim that is not ``outcome_unknown``."""

    def prune(self, *, now: datetime | None = None) -> int:
        """Delete the claims that last changed longer ago than the store's retention; how many went."""


def _attempt(claim: Claim) -> StepAttempt:
    return StepAttempt(key=claim.key, state=STEP_ATTEMPT[claim.state])


def _reserve(existing: Claim | None, run_id: str, key: str, worker: str, now: datetime) -> tuple[Claim, bool]:
    """The claim after a reserve, and whether the worker acts: the machine decides, the store writes."""
    if existing is None:
        reserved = StepAttempt(key=key).reserve().after
        return Claim(run_id=run_id, key=key, state=reserved.state.value, worker=worker, reserved_at=now,
                     updated_at=now), True
    if existing.held_by(worker):
        return existing, True
    if existing.state == RESERVED:
        # Another worker holds it and never committed: it was lost after it may have acted.
        unknown = _attempt(existing).worker_lost().after
        return replace(existing, state=unknown.state.value, updated_at=now), False
    return existing, False


def _commit(existing: Claim | None, key: str, worker: str, envelope: Mapping[str, Any], now: datetime) -> Claim:
    if existing is None:
        raise ClaimRefused(f"key {key!r} was never reserved: a worker reserves a key before it acts")
    if existing.state == COMMITTED and existing.worker == worker:
        return existing
    if existing.state == RESERVED and existing.worker != worker:
        raise ClaimRefused(f"key {key!r} is reserved by another worker")
    try:
        committed = _attempt(existing).commit().after
    except InvalidTransition as exc:
        raise ClaimRefused(f"key {key!r} cannot be committed: {exc}") from exc
    return replace(existing, state=committed.state.value, envelope=dict(envelope), committed_at=now, updated_at=now)


def _reconcile(existing: Claim | None, key: str, actor: str, now: datetime) -> Claim:
    if existing is None:
        raise ClaimRefused(f"key {key!r} has no claim to reconcile")
    try:
        recorded = _attempt(existing).reconcile(actor=actor).after
    except InvalidTransition as exc:
        raise ClaimRefused(f"key {key!r} cannot be reconciled: {exc}") from exc
    return replace(existing, state=recorded.state.value, actor=actor, updated_at=now)


def _now() -> datetime:
    return datetime.now(UTC)


class InMemoryClaimStore:
    """Thread-safe claims for tests and one-process use."""

    def __init__(self, retention: timedelta = CLAIM_RETENTION) -> None:
        self.retention = retention
        self._lock = RLock()
        self._claims: dict[tuple[str, str], Claim] = {}

    def reserve(self, run_id: str, key: str, *, worker: str) -> tuple[Claim, bool]:
        with self._lock:
            claim, acts = _reserve(self._claims.get((run_id, key)), run_id, key, worker, _now())
            self._claims[(run_id, key)] = claim
            return claim, acts

    def commit(self, run_id: str, key: str, *, worker: str, envelope: Mapping[str, Any]) -> Claim:
        with self._lock:
            claim = _commit(self._claims.get((run_id, key)), key, worker, envelope, _now())
            self._claims[(run_id, key)] = claim
            return claim

    def get(self, run_id: str, key: str) -> Claim | None:
        with self._lock:
            return self._claims.get((run_id, key))

    def reconcile(self, run_id: str, key: str, *, actor: str) -> Claim:
        with self._lock:
            claim = _reconcile(self._claims.get((run_id, key)), key, actor, _now())
            self._claims[(run_id, key)] = claim
            return claim

    def prune(self, *, now: datetime | None = None) -> int:
        cutoff = (now or _now()) - self.retention
        with self._lock:
            old = [which for which, claim in self._claims.items() if claim.updated_at < cutoff]
            for which in old:
                del self._claims[which]
            return len(old)


_COLUMNS = "run_id, key, state, worker, envelope, reserved_at, committed_at, updated_at, actor"


def _column(row: Any, index: int, name: str) -> Any:
    """One column of a row, whether the connection returns tuples or mappings (``dict_row``)."""
    return row[name] if isinstance(row, Mapping) else row[index]


def _when(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _claim_row(row: Any) -> Claim:
    envelope = _column(row, 4, "envelope")
    if isinstance(envelope, str):
        envelope = json.loads(envelope)
    return Claim(
        run_id=_column(row, 0, "run_id"), key=_column(row, 1, "key"), state=_column(row, 2, "state"),
        worker=_column(row, 3, "worker"), envelope=envelope, reserved_at=_when(_column(row, 5, "reserved_at")),
        committed_at=_when(_column(row, 6, "committed_at")), updated_at=_when(_column(row, 7, "updated_at")),
        actor=_column(row, 8, "actor"),
    )


class SqliteClaimStore:
    """Claims in a SQLite file: the Track's own at dev level 1 and on the desktop, beside its tables.

    Each call is one transaction on its own connection, taken with the write
    lock before it reads (``BEGIN IMMEDIATE``), so a reserve and a commit for
    one key never interleave across the processes sharing the file.
    """

    _SCHEMA = (
        """
        CREATE TABLE IF NOT EXISTS collab_claims (
            run_id       TEXT NOT NULL,
            key          TEXT NOT NULL,
            state        TEXT NOT NULL,
            worker       TEXT,
            envelope     TEXT,
            reserved_at  TEXT NOT NULL,
            committed_at TEXT,
            updated_at   TEXT NOT NULL,
            actor        TEXT,
            PRIMARY KEY (run_id, key)
        )
        """,
        "CREATE INDEX IF NOT EXISTS collab_claims_updated ON collab_claims (updated_at)",
    )

    def __init__(self, path: str | Path, retention: timedelta = CLAIM_RETENTION) -> None:
        self.path = str(path)
        self.retention = retention

    @classmethod
    def ensure_schema(cls, path: str | Path) -> None:
        """Create the claims table in the file if absent (idempotent), and the file itself."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(path))) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            for statement in cls._SCHEMA:
                connection.execute(statement)

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    @staticmethod
    def _select(connection: sqlite3.Connection, run_id: str, key: str) -> Claim | None:
        row = connection.execute(f"SELECT {_COLUMNS} FROM collab_claims WHERE run_id = ? AND key = ?",
                                 (run_id, key)).fetchone()
        return None if row is None else _claim_row(row)

    @staticmethod
    def _upsert(connection: sqlite3.Connection, claim: Claim) -> None:
        connection.execute(
            f"INSERT OR REPLACE INTO collab_claims ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (claim.run_id, claim.key, claim.state, claim.worker,
             None if claim.envelope is None else json.dumps(claim.envelope), claim.reserved_at.isoformat(),
             None if claim.committed_at is None else claim.committed_at.isoformat(), claim.updated_at.isoformat(),
             claim.actor))

    def reserve(self, run_id: str, key: str, *, worker: str) -> tuple[Claim, bool]:
        with self._write() as connection:
            claim, acts = _reserve(self._select(connection, run_id, key), run_id, key, worker, _now())
            self._upsert(connection, claim)
            return claim, acts

    def commit(self, run_id: str, key: str, *, worker: str, envelope: Mapping[str, Any]) -> Claim:
        with self._write() as connection:
            claim = _commit(self._select(connection, run_id, key), key, worker, envelope, _now())
            self._upsert(connection, claim)
            return claim

    def get(self, run_id: str, key: str) -> Claim | None:
        with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            return self._select(connection, run_id, key)

    def reconcile(self, run_id: str, key: str, *, actor: str) -> Claim:
        with self._write() as connection:
            claim = _reconcile(self._select(connection, run_id, key), key, actor, _now())
            self._upsert(connection, claim)
            return claim

    def prune(self, *, now: datetime | None = None) -> int:
        cutoff = ((now or _now()) - self.retention).isoformat()
        with self._write() as connection:
            return connection.execute("DELETE FROM collab_claims WHERE updated_at < ?", (cutoff,)).rowcount


class PostgresClaimStore:
    """Claims on the hub's Postgres, beside the Track, on the pool the application supplies.

    On the hub the table comes from the ``collab_`` migration registry
    (``COLLAB_SCHEMA_MIGRATIONS`` in the API), never from :meth:`ensure_schema`,
    which exists for the standalone package and local use; ``_SCHEMA`` is the
    registry's statements in order, and a test holds the two equal. A reserve
    and a commit for one key are serialized by locking the key's row
    (``FOR UPDATE``) for the transaction, and a first reserve by an advisory
    lock on the key, so two workers reserving at once leave one reservation.
    """

    _SCHEMA = (
        """
        CREATE TABLE IF NOT EXISTS collab_claims (
            run_id       text NOT NULL,
            key          text NOT NULL,
            state        text NOT NULL,
            worker       text,
            envelope     jsonb,
            reserved_at  timestamptz NOT NULL,
            committed_at timestamptz,
            updated_at   timestamptz NOT NULL,
            actor        text,
            PRIMARY KEY (run_id, key)
        )
        """,
        "CREATE INDEX IF NOT EXISTS collab_claims_updated ON collab_claims (updated_at)",
    )

    def __init__(self, pool: Any, retention: timedelta = CLAIM_RETENTION) -> None:
        self.pool = pool
        self.retention = retention

    @staticmethod
    def ensure_schema(connection: Any) -> None:
        """Create the claims table and its indexes if absent (idempotent); for standalone and local use."""
        for statement in PostgresClaimStore._SCHEMA:
            connection.execute(statement)

    @staticmethod
    def _lock(connection: Any, run_id: str, key: str) -> Claim | None:
        """The key's claim, locked for this transaction; the key itself is locked when there is no row yet."""
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                           (f"collab_claim:{run_id}:{key}",))
        row = connection.execute(f"SELECT {_COLUMNS} FROM collab_claims WHERE run_id = %s AND key = %s FOR UPDATE",
                                 (run_id, key)).fetchone()
        return None if row is None else _claim_row(row)

    @staticmethod
    def _upsert(connection: Any, claim: Claim) -> None:
        connection.execute(
            f"""
            INSERT INTO collab_claims ({_COLUMNS}) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
            ON CONFLICT (run_id, key) DO UPDATE SET state = EXCLUDED.state, worker = EXCLUDED.worker,
                envelope = EXCLUDED.envelope, committed_at = EXCLUDED.committed_at,
                updated_at = EXCLUDED.updated_at, actor = EXCLUDED.actor
            """,
            (claim.run_id, claim.key, claim.state, claim.worker,
             None if claim.envelope is None else json.dumps(claim.envelope), claim.reserved_at, claim.committed_at,
             claim.updated_at, claim.actor))

    def reserve(self, run_id: str, key: str, *, worker: str) -> tuple[Claim, bool]:
        with self.pool.connection() as connection:
            claim, acts = _reserve(self._lock(connection, run_id, key), run_id, key, worker, _now())
            self._upsert(connection, claim)
            return claim, acts

    def commit(self, run_id: str, key: str, *, worker: str, envelope: Mapping[str, Any]) -> Claim:
        with self.pool.connection() as connection:
            claim = _commit(self._lock(connection, run_id, key), key, worker, envelope, _now())
            self._upsert(connection, claim)
            return claim

    def get(self, run_id: str, key: str) -> Claim | None:
        with self.pool.connection() as connection:
            row = connection.execute(f"SELECT {_COLUMNS} FROM collab_claims WHERE run_id = %s AND key = %s",
                                     (run_id, key)).fetchone()
        return None if row is None else _claim_row(row)

    def reconcile(self, run_id: str, key: str, *, actor: str) -> Claim:
        with self.pool.connection() as connection:
            claim = _reconcile(self._lock(connection, run_id, key), key, actor, _now())
            self._upsert(connection, claim)
            return claim

    def prune(self, *, now: datetime | None = None) -> int:
        cutoff = (now or _now()) - self.retention
        with self.pool.connection() as connection:
            return connection.execute("DELETE FROM collab_claims WHERE updated_at < %s", (cutoff,)).rowcount


__all__ = [
    "CLAIM_RETENTION", "COMMITTED", "OUTCOME_UNKNOWN", "RECORDED", "RESERVED", "Claim", "ClaimRefused",
    "ClaimStore", "InMemoryClaimStore", "PostgresClaimStore", "SqliteClaimStore", "key_component", "run_of",
]
