"""Durable, replayable Track events and status derivation.

A Track is the append-only record of one Op. Callers observe a run by reading
the Track; status is a projection of its events, never a separately maintained
mutable field: the run machine (``states/run.py``) folded over the events.

Every event carries ``schema``, the version of its shape. Version 1 is the
schema ``docs/cog-execution/track.md`` describes, and the engine writes it; an
event with ``schema`` 0 was written before it (or built without saying), and
:func:`upgrade` reads it in the version-1 shape.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4

from .states.run import Run, RunState

SCHEMA_VERSION = 1

# A step's payload above this many bytes of JSON is stored by reference (#5).
PAYLOAD_INLINE_MAX_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class TrackEvent:
    """One immutable fact in a run's Track."""

    run_id: str
    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    event_id: str = field(default_factory=lambda: uuid4().hex)
    sequence: int | None = None
    # The version of the event's shape. An event built without one is read as
    # written before v1, so `upgrade` lifts it: a pre-v1 `paused` rebuilt from an
    # export stays readable instead of being taken for a v1 event it is not. A
    # v1 event through `upgrade` is unchanged either way. The engine writes v1.
    schema: int = 0


class TrackStore(Protocol):
    """Append, replay, and stream the durable history for an Op, and keep its large payloads."""

    def append(self, event: TrackEvent) -> TrackEvent:
        """Append an event and return it with its assigned sequence."""

    def append_if(self, event: TrackEvent, condition: Callable[[tuple[TrackEvent, ...]], bool]) -> TrackEvent | None:
        """Append an event only if ``condition`` holds of the run's Track as it is when the event lands.

        The check and the append are one step: no other append to the run comes
        between them, so what ``condition`` saw is still true of the Track the
        event joins. Returns the stored event, or ``None`` when ``condition``
        refused it. This is how a process that does not advance a run writes to
        its Track without racing the one that does.
        """

    def replay(self, run_id: str, *, after_sequence: int = 0, limit: int | None = None) -> tuple[TrackEvent, ...]:
        """Replay events in sequence order: those after ``after_sequence``, at most ``limit`` of them."""

    def stream(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        timeout_seconds: float = 0,
    ) -> Iterator[TrackEvent]:
        """Yield new events, optionally polling until the timeout expires."""

    def put_payload(self, run_id: str, ref: str, rendered: str) -> None:
        """Keep a step's payload, already rendered as JSON, under ``ref``; writing a ``ref`` again replaces it."""

    def get_payload(self, ref: str) -> Any:
        """The payload kept under ``ref``; ``KeyError`` when there is none."""

    def run_ids(self) -> tuple[str, ...]:
        """Every run submitted to this Track, once each, in the order they were submitted.

        What a host reads when it starts, to find the runs it left unfinished. A run
        submitted before schema v1 is recorded as ``submitted``, and is listed too.
        """


_SUBMITTED = frozenset({"op_submitted", "submitted"})
"""What submits a run: ``op_submitted`` in schema v1, ``submitted`` before it."""


def _once(run_ids: Iterable[str]) -> tuple[str, ...]:
    """Each run once, where it first appears."""
    return tuple(dict.fromkeys(run_ids))


class OneSubmissionPerRun(ValueError):
    """A second ``op_submitted`` for a run: two callers tried to start it."""


# ---------------------------------------------------------------------------
# Reading events written before schema version 1
# ---------------------------------------------------------------------------

# Pre-v1 event types, and the v1 type each one is read as.
_RENAMED = {
    "submitted": "op_submitted",
    "paused": "gate_escalated",
    "signal_received": "gate_decided",
    "rejected": "gate_decided",
    "timed_out": "budget_exceeded",
}

# What a pre-v1 event type implied and its payload did not say.
_IMPLIED = {
    "rejected": {"outcome": "reject"},
    "signal_received": {"outcome": "send_back"},
    "timed_out": {"dimension": "duration"},
}


def upgrade(event: TrackEvent) -> TrackEvent:
    """The event in the version-1 shape, whatever version it was written in.

    A version-1 event is returned as it is. An older one keeps its identity,
    sequence and time; its type and payload are read as version 1 records them:
    ``paused`` as ``gate_escalated``; ``signal_received`` and ``rejected`` as
    ``gate_decided`` with an ``outcome``; ``timed_out`` as ``budget_exceeded``
    with ``dimension: duration``; ``step_completed``'s ``output`` as ``payload``;
    the older ``submitted`` as ``op_submitted``. Nothing is written back.
    """
    if event.schema >= SCHEMA_VERSION:
        return event
    kind, payload = event.event_type, dict(event.payload)
    for key, value in _IMPLIED.get(kind, {}).items():
        payload.setdefault(key, value)
    if "output" in payload and "payload" not in payload:
        payload["payload"] = payload.pop("output")
    return replace(event, event_type=_RENAMED.get(kind, kind), payload=payload, schema=SCHEMA_VERSION)


def derive_run_status(events: Iterable[TrackEvent]) -> RunState | None:
    """The run's state: its Track folded through the run machine; ``None`` if never submitted."""

    run = Run.replay(upgrade(event) for event in events)
    return None if run is None else run.state


def _column(row: Any, index: int, name: str) -> Any:
    """One column of a row, whether the connection returns tuples or mappings (``dict_row``)."""
    return row[name] if isinstance(row, Mapping) else row[index]


def _run_lock(run_id: str) -> str:
    """The advisory lock that serializes appends to one run's Track."""
    return f"collab_track:{run_id}"


def _copy(event: TrackEvent, sequence: int) -> TrackEvent:
    return TrackEvent(
        run_id=event.run_id,
        event_type=event.event_type,
        payload=dict(event.payload),
        occurred_at=event.occurred_at,
        event_id=event.event_id,
        sequence=sequence,
        schema=event.schema,
    )


def _poll(replay, run_id: str, after_sequence: int, timeout_seconds: float, interval: float) -> Iterator[TrackEvent]:
    deadline = time.monotonic() + timeout_seconds
    cursor = after_sequence
    while True:
        events = replay(run_id, after_sequence=cursor)
        for event in events:
            cursor = event.sequence or cursor
            yield event
        if not timeout_seconds or time.monotonic() >= deadline:
            return
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


class InMemoryTrackStore:
    """Thread-safe TrackStore used by tests and local development."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._next_sequence = 1
        self._events: dict[str, list[TrackEvent]] = defaultdict(list)
        self._ids: set[str] = set()
        self._payloads: dict[str, Any] = {}

    def append(self, event: TrackEvent) -> TrackEvent:
        with self._lock:
            if event.sequence is not None:
                raise ValueError("TrackStore assigns event sequences")
            if event.event_id in self._ids:
                raise ValueError(f"event {event.event_id!r} is already on the Track")
            if event.event_type == "op_submitted" and any(
                e.event_type == "op_submitted" for e in self._events.get(event.run_id, ())
            ):
                raise OneSubmissionPerRun(f"run {event.run_id!r} was already submitted")
            stored = _copy(event, self._next_sequence)
            self._next_sequence += 1
            self._events[event.run_id].append(stored)
            self._ids.add(event.event_id)
            return stored

    def append_if(self, event: TrackEvent, condition: Callable[[tuple[TrackEvent, ...]], bool]) -> TrackEvent | None:
        with self._lock:  # reentrant: the check and the append are under one hold
            # The store's own events, as SQLite and Postgres read theirs inside the transaction.
            return self.append(event) if condition(tuple(self._events.get(event.run_id, ()))) else None

    def replay(self, run_id: str, *, after_sequence: int = 0, limit: int | None = None) -> tuple[TrackEvent, ...]:
        with self._lock:
            events = tuple(event for event in self._events.get(run_id, ()) if (event.sequence or 0) > after_sequence)
        return events if limit is None else events[:limit]

    def stream(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        timeout_seconds: float = 0,
    ) -> Iterator[TrackEvent]:
        return _poll(self.replay, run_id, after_sequence, timeout_seconds, 0.05)

    def put_payload(self, run_id: str, ref: str, rendered: str) -> None:
        with self._lock:
            self._payloads[ref] = json.loads(rendered)

    def get_payload(self, ref: str) -> Any:
        with self._lock:
            return self._payloads[ref]

    def run_ids(self) -> tuple[str, ...]:
        with self._lock:
            submitted = [
                (event.sequence or 0, run_id)
                for run_id, events in self._events.items()
                for event in events if event.event_type in _SUBMITTED
            ]
        return _once(run_id for _, run_id in sorted(submitted))


class SqliteTrackStore:
    """TrackStore on one SQLite file, shared by the processes of one host.

    Level 1 runs the API and the run controller as two processes over one Track
    file, and the desktop's local run host keeps its Track the same way. The
    file is opened in WAL mode so a reader never blocks the writer. Each call
    opens its own connection, so the store is safe to share between threads.
    """

    _SCHEMA = (
        """
        CREATE TABLE IF NOT EXISTS collab_track_events (
            sequence    INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id    TEXT NOT NULL UNIQUE,
            run_id      TEXT NOT NULL,
            event_type  TEXT NOT NULL,
            payload     TEXT NOT NULL DEFAULT '{}',
            occurred_at TEXT NOT NULL,
            schema      INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX IF NOT EXISTS collab_track_events_run_sequence ON collab_track_events (run_id, sequence)",
        "CREATE UNIQUE INDEX IF NOT EXISTS collab_track_one_submission "
        "ON collab_track_events (run_id) WHERE event_type = 'op_submitted'",
        """
        CREATE TABLE IF NOT EXISTS collab_track_payloads (
            ref        TEXT PRIMARY KEY,
            run_id     TEXT NOT NULL,
            payload    TEXT NOT NULL,
            stored_at  TEXT NOT NULL
        )
        """,
    )

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    @classmethod
    def ensure_schema(cls, path: str | Path) -> None:
        """Create the Track tables in the file if absent (idempotent), and the file itself."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(path))) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            for statement in cls._SCHEMA:
                connection.execute(statement)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """One connection for one call, in one transaction, and closed after it.

        ``with sqlite3.connect(...)`` only commits: the connection, its file
        handles and the WAL's stay open until collected, so a stream polling every
        50 ms would pile them up. ``timeout`` is how long a write waits for the lock.
        """
        with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            yield connection

    def append(self, event: TrackEvent) -> TrackEvent:
        if event.sequence is not None:
            raise ValueError("TrackStore assigns event sequences")
        with self._connect() as connection:
            return self._insert(connection, event)

    @staticmethod
    def _insert(connection: sqlite3.Connection, event: TrackEvent) -> TrackEvent:
        try:
            cursor = connection.execute(
                "INSERT INTO collab_track_events (event_id, run_id, event_type, payload, occurred_at, schema) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (event.event_id, event.run_id, event.event_type, json.dumps(event.payload),
                 event.occurred_at.isoformat(), event.schema),
            )
        except sqlite3.IntegrityError as exc:
            # SQLite names the column, not the index, when a partial unique index refuses a row.
            if event.event_type == "op_submitted" and "collab_track_events.run_id" in str(exc):
                raise OneSubmissionPerRun(f"run {event.run_id!r} was already submitted") from exc
            raise
        return replace(event, sequence=cursor.lastrowid)

    def append_if(self, event: TrackEvent, condition: Callable[[tuple[TrackEvent, ...]], bool]) -> TrackEvent | None:
        if event.sequence is not None:
            raise ValueError("TrackStore assigns event sequences")
        with self._connect() as connection:
            # The write lock is taken before the read: every other writer to the file waits until
            # this transaction ends, so nothing is appended between the check and the event.
            connection.execute("BEGIN IMMEDIATE")
            if not condition(self._select(connection, event.run_id, 0)):
                return None
            return self._insert(connection, event)

    def replay(self, run_id: str, *, after_sequence: int = 0, limit: int | None = None) -> tuple[TrackEvent, ...]:
        with self._connect() as connection:
            return self._select(connection, run_id, after_sequence, limit)

    @staticmethod
    def _select(connection: sqlite3.Connection, run_id: str, after_sequence: int,
                limit: int | None = None) -> tuple[TrackEvent, ...]:
        rows = connection.execute(
            "SELECT sequence, event_id, run_id, event_type, payload, occurred_at, schema "
            "FROM collab_track_events WHERE run_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
            (run_id, after_sequence, -1 if limit is None else limit),
        ).fetchall()
        return tuple(
            TrackEvent(
                sequence=row[0], event_id=row[1], run_id=row[2], event_type=row[3],
                payload=json.loads(row[4]), occurred_at=datetime.fromisoformat(row[5]), schema=row[6],
            )
            for row in rows
        )

    def stream(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        timeout_seconds: float = 0,
    ) -> Iterator[TrackEvent]:
        return _poll(self.replay, run_id, after_sequence, timeout_seconds, 0.05)

    def put_payload(self, run_id: str, ref: str, rendered: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO collab_track_payloads (ref, run_id, payload, stored_at) VALUES (?, ?, ?, ?)",
                (ref, run_id, rendered, datetime.now(UTC).isoformat()),
            )

    def get_payload(self, ref: str) -> Any:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM collab_track_payloads WHERE ref = ?", (ref,)).fetchone()
        if row is None:
            raise KeyError(ref)
        return json.loads(row[0])

    def run_ids(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id FROM collab_track_events WHERE event_type IN ('op_submitted', 'submitted') "
                "ORDER BY sequence"
            ).fetchall()
        return _once(row[0] for row in rows)


class PostgresTrackStore:
    """TrackStore backed by a PostgreSQL connection pool.

    The pool is supplied by the application, so this adapter does not create
    connections or decide deployment topology. The table's global sequence
    gives each event a stable replay order, including events written by
    multiple API replicas.

    A sequence is drawn when a row is inserted, but rows become visible when
    they commit, and two transactions can commit in the other order. A reader
    that had seen the later sequence would move its cursor past the earlier one
    and never return it. So appends to one run are serialized: each takes a
    transaction-scoped advisory lock on the run before its insert draws a
    sequence, and holds it until it commits. Within a run, sequence order is
    then commit order, and a cursor over one run's events never skips one.
    Appends to different runs do not wait for each other.

    On the hub the tables come from the ``collab_`` migration registry
    (``COLLAB_SCHEMA_MIGRATIONS`` in the API), never from :meth:`ensure_schema`,
    which exists for the standalone package and local use. ``_SCHEMA`` is the
    registry's Track statements, in order, and a test holds the two equal. A
    released migration is frozen (its checksum is verified at startup), so a
    change is a *new* migration, and the same statements appended here.
    """

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    # One statement per execute: psycopg3's extended protocol rejects multiple
    # commands in a single execute().
    _SCHEMA = (
        "CREATE SEQUENCE IF NOT EXISTS collab_track_event_sequence",
        """
        CREATE TABLE IF NOT EXISTS collab_track_events (
            sequence    bigint PRIMARY KEY DEFAULT nextval('collab_track_event_sequence'),
            event_id    text NOT NULL UNIQUE,
            run_id      text NOT NULL,
            event_type  text NOT NULL,
            payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
            occurred_at timestamptz NOT NULL,
            schema      integer NOT NULL DEFAULT 0
        )
        """,
        # A table created before events carried a schema version: its rows are
        # version 0, which is what the default says, and new rows write theirs.
        "ALTER TABLE collab_track_events ADD COLUMN IF NOT EXISTS schema integer NOT NULL DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS collab_track_events_run_sequence ON collab_track_events (run_id, sequence)",
        # At most one submission per run: two API replicas cannot both start the
        # same run (the losing append raises). Single-owner lease / graceful
        # handling of the conflict lands with the crash-safe engine backing (#1).
        "CREATE UNIQUE INDEX IF NOT EXISTS collab_track_one_submission "
        "ON collab_track_events (run_id) WHERE event_type = 'op_submitted'",
        # A step's payload above the inline threshold, kept beside the Track and
        # named by the `payload_ref` of its `step_completed` event.
        """
        CREATE TABLE IF NOT EXISTS collab_track_payloads (
            ref        text PRIMARY KEY,
            run_id     text NOT NULL,
            payload    jsonb NOT NULL,
            stored_at  timestamptz NOT NULL DEFAULT now()
        )
        """,
        "CREATE INDEX IF NOT EXISTS collab_track_payloads_run ON collab_track_payloads (run_id)",
    )

    @staticmethod
    def ensure_schema(connection: Any) -> None:
        """Create the Track tables and indexes if absent (idempotent).

        The adapter never calls this itself: append()/replay() assume the tables
        exist. On the hub they come from the migration registry; this is for the
        standalone package and local use.
        """
        for statement in PostgresTrackStore._SCHEMA:
            connection.execute(statement)

    def append(self, event: TrackEvent) -> TrackEvent:
        if event.sequence is not None:
            raise ValueError("TrackStore assigns event sequences")
        with self.pool.connection() as connection:
            return self._insert(connection, event)

    @staticmethod
    def _insert(connection: Any, event: TrackEvent) -> TrackEvent:
        # Held until this transaction commits, so no other append to the run
        # draws a sequence before this row is visible (see the class docstring).
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (_run_lock(event.run_id),))
        try:
            row = connection.execute(
                """
                INSERT INTO collab_track_events
                    (event_id, run_id, event_type, payload, occurred_at, schema)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s)
                RETURNING sequence
                """,
                (event.event_id, event.run_id, event.event_type, json.dumps(event.payload),
                 event.occurred_at, event.schema),
            ).fetchone()
        except Exception as exc:  # noqa: BLE001 - the driver's error class is not imported here
            if "collab_track_one_submission" in str(exc):
                raise OneSubmissionPerRun(f"run {event.run_id!r} was already submitted") from exc
            raise
        return replace(event, sequence=_column(row, 0, "sequence"))

    def append_if(self, event: TrackEvent, condition: Callable[[tuple[TrackEvent, ...]], bool]) -> TrackEvent | None:
        if event.sequence is not None:
            raise ValueError("TrackStore assigns event sequences")
        with self.pool.connection() as connection:
            # The run's lock, the one every append to it takes, is held from before the read until
            # this transaction commits: nothing is appended between the check and the event.
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (_run_lock(event.run_id),))
            if not condition(self._select(connection, event.run_id, 0)):
                return None
            return self._insert(connection, event)

    def replay(self, run_id: str, *, after_sequence: int = 0, limit: int | None = None) -> tuple[TrackEvent, ...]:
        with self.pool.connection() as connection:
            return self._select(connection, run_id, after_sequence, limit)

    @staticmethod
    def _select(connection: Any, run_id: str, after_sequence: int, limit: int | None = None) -> tuple[TrackEvent, ...]:
        rows = connection.execute(
            """
            SELECT sequence, event_id, run_id, event_type, payload, occurred_at, schema
            FROM collab_track_events
            WHERE run_id = %s AND sequence > %s
            ORDER BY sequence
            LIMIT %s
            """,
            (run_id, after_sequence, limit),
        ).fetchall()
        # By name or by position: the hub's shared pool returns mappings (dict_row).
        return tuple(
            TrackEvent(
                sequence=_column(row, 0, "sequence"), event_id=_column(row, 1, "event_id"),
                run_id=_column(row, 2, "run_id"), event_type=_column(row, 3, "event_type"),
                payload=_column(row, 4, "payload"), occurred_at=_column(row, 5, "occurred_at"),
                schema=_column(row, 6, "schema"),
            )
            for row in rows
        )

    def stream(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        timeout_seconds: float = 0,
    ) -> Iterator[TrackEvent]:
        return _poll(self.replay, run_id, after_sequence, timeout_seconds, 0.1)

    def put_payload(self, run_id: str, ref: str, rendered: str) -> None:
        with self.pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO collab_track_payloads (ref, run_id, payload)
                VALUES (%s, %s, %s::jsonb)
                ON CONFLICT (ref) DO UPDATE SET payload = EXCLUDED.payload, stored_at = now()
                """,
                (ref, run_id, rendered),
            )

    def get_payload(self, ref: str) -> Any:
        with self.pool.connection() as connection:
            row = connection.execute("SELECT payload FROM collab_track_payloads WHERE ref = %s", (ref,)).fetchone()
        if row is None:
            raise KeyError(ref)
        return _column(row, 0, "payload")

    def run_ids(self) -> tuple[str, ...]:
        # One op_submitted per run, which the partial unique index on run_id enforces; a Track
        # written before v1 records `submitted` instead.
        with self.pool.connection() as connection:
            rows = connection.execute(
                "SELECT run_id FROM collab_track_events WHERE event_type IN ('op_submitted', 'submitted') "
                "ORDER BY sequence"
            ).fetchall()
        return _once(_column(row, 0, "run_id") for row in rows)
