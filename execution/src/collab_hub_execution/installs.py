"""The installs: each Cog pinned by digest on this hub, what its install has reached, and its binding.

The store the API and the run controller share, beside the Track. The API
records that an install was asked for (``POST /v1/cogs/installs``) or that
its removal was (``DELETE``); the controller, which alone constructs an
executor, takes each ask and moves the install through its machine
(``states.install``): ``PUBLISHED`` to ``FETCHED`` to ``BOUND`` to
``INVOKABLE``, every move appended to the install's own event log so a Cog
stuck before ``INVOKABLE`` says which step failed and why. Three stores, one
protocol: in memory for tests, SQLite for a hub without Postgres, and
Postgres on the hub, whose tables come from the hub's migrations.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .states import INSTALL, CogInstall, InstallState
from .states._machine import Transition

INSTALL_RUN_PREFIX = "install:"
"""The run id an install's workers are materialized under: ``install:<install id>``."""


class InstallConflict(RuntimeError):
    """A record that expected the install in one state found it in another: another controller moved it (an
    uninstall recorded while this one was checking, say), and this one's move is dropped, never written over it."""


class InstallExists(ValueError):
    """The organization already holds a live install of this digest."""


@dataclass(frozen=True, slots=True)
class InstallRow:
    """One install as the store keeps it."""

    id: str
    reference: str
    digest: str
    state: str = field(default_factory=lambda: InstallState.PUBLISHED.value)
    org_id: str | None = None
    cog_id: str | None = None
    card: Mapping[str, Any] = field(default_factory=dict)
    controller: str | None = None
    lock_digest: str | None = None
    binding_id: str | None = None
    binding: Mapping[str, Any] | None = None
    failure: str | None = None
    failed_step: str | None = None
    requested_by: str | None = None
    uninstall_requested_by: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def live(self) -> bool:
        return self.state != InstallState.UNINSTALLED.value

    @property
    def invokable(self) -> bool:
        return self.state == InstallState.INVOKABLE.value

    @property
    def run_id(self) -> str:
        """The run id this install's workers are materialized under."""
        return f"{INSTALL_RUN_PREFIX}{self.id}"

    def machine(self) -> CogInstall:
        """The install's context for its machine, at the state the row records."""
        return CogInstall(reference=self.reference, state=INSTALL[self.state], binding=self.binding_id,
                          failure=self.failure)


@dataclass(frozen=True, slots=True)
class InstallEvent:
    install_id: str
    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    sequence: int | None = None


def new_install_id() -> str:
    return f"inst-{uuid.uuid4().hex[:12]}"


_COLUMNS = ("id", "org_id", "reference", "digest", "cog_id", "card", "state", "controller", "lock_digest",
            "binding_id", "binding", "failure", "failed_step", "requested_by", "uninstall_requested_by",
            "created_at", "updated_at")
_CHANGEABLE = frozenset({"state", "controller", "lock_digest", "binding_id", "binding", "failure", "failed_step"})


class InstallStore(Protocol):
    """What the API and the controller share about installs."""

    def create(self, row: InstallRow) -> InstallRow:
        """Record an install asked for; ``InstallExists`` when the organization holds a live one of the digest."""

    def get(self, install_id: str) -> InstallRow | None: ...

    def find(self, org_id: str | None, *, reference: str | None = None, digest: str | None = None,
             invokable: bool = False) -> InstallRow | None:
        """The organization's live install of a reference or a digest, or ``None``."""

    def list(self, *, org_id: str | None = None, state: str | None = None, include_uninstalled: bool = False,
             limit: int | None = None, offset: int = 0) -> tuple[InstallRow, ...]: ...

    def claim(self, install_id: str, controller: str) -> bool:
        """Take an install nobody has started on, naming the controller; ``False`` when another did."""

    def record(self, install_id: str, transition: Transition[CogInstall], *, expected_state: str | None = None,
               **changes: Any) -> InstallRow:
        """Write a machine transition's records to the install's log, and its state and ``changes`` to the row.

        With ``expected_state``, the state the transition moves from, the row is written only while it is still
        in that state; ``InstallConflict`` otherwise, and nothing is written.
        """

    def reclaim(self, install_id: str, from_controller: str, controller: str) -> bool:
        """Take an install in progress from a controller that no longer holds its id; ``False`` when it moved."""

    def request_uninstall(self, install_id: str, actor: str) -> InstallRow | None:
        """Record that ``actor`` asked for the install's removal; ``None`` for an install that is gone."""

    def events(self, install_id: str) -> tuple[InstallEvent, ...]: ...

    def pending(self) -> tuple[InstallRow, ...]:
        """What a controller has to act on: installs nobody has started, and removals asked for."""

    def in_progress(self) -> tuple[InstallRow, ...]:
        """Installs a controller has claimed and not finished: short of invokable, with no step recorded as failed
        and no removal asked. What a controller resumes under its own id, or takes from an id no longer held."""


IN_PROGRESS_STATES = (InstallState.PUBLISHED.value, InstallState.FETCHED.value, InstallState.BOUND.value)


def _check_changes(changes: Mapping[str, Any]) -> None:
    unknown = set(changes) - _CHANGEABLE
    if unknown:
        raise ValueError(f"an install's {sorted(unknown)} cannot be changed by a record")


# --- in memory ------------------------------------------------------------------------------------


class InMemoryInstallStore:
    def __init__(self) -> None:
        self._rows: dict[str, InstallRow] = {}
        self._events: list[InstallEvent] = []
        self._lock = threading.Lock()

    def create(self, row: InstallRow) -> InstallRow:
        with self._lock:
            if any(r.org_id == row.org_id and r.digest == row.digest and r.live for r in self._rows.values()):
                raise InstallExists(f"{row.digest} is already installed")
            self._rows[row.id] = row
            return row

    def get(self, install_id: str) -> InstallRow | None:
        return self._rows.get(install_id)

    def find(self, org_id, *, reference=None, digest=None, invokable=False) -> InstallRow | None:
        for row in self._rows.values():
            if row.org_id == org_id and row.live and (reference is None or row.reference == reference) \
                    and (digest is None or row.digest == digest) and (not invokable or row.invokable):
                return row
        return None

    def list(self, *, org_id=None, state=None, include_uninstalled=False, limit=None, offset=0):
        rows = [r for r in self._rows.values() if (org_id is None or r.org_id == org_id)
                and (state is None or r.state == state) and (include_uninstalled or r.live)]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return tuple(rows[offset:offset + limit] if limit is not None else rows[offset:])

    def claim(self, install_id: str, controller: str) -> bool:
        with self._lock:
            row = self._rows.get(install_id)
            if row is None or row.controller is not None or row.state != InstallState.PUBLISHED.value:
                return False
            self._rows[install_id] = replace(row, controller=controller, updated_at=datetime.now(UTC))
            return True

    def reclaim(self, install_id: str, from_controller: str, controller: str) -> bool:
        with self._lock:
            row = self._rows.get(install_id)
            if row is None or row.controller != from_controller or not _in_progress(row):
                return False
            self._rows[install_id] = replace(row, controller=controller, updated_at=datetime.now(UTC))
            return True

    def record(self, install_id: str, transition: Transition[CogInstall], *, expected_state: str | None = None,
               **changes: Any) -> InstallRow:
        _check_changes(changes)
        with self._lock:
            row = self._rows[install_id]
            if expected_state is not None and row.state != expected_state:
                raise InstallConflict(f"install {install_id} is {row.state}, not {expected_state}")
            now = datetime.now(UTC)
            for record in transition.records:
                self._events.append(InstallEvent(install_id, record.event_type, dict(record.payload), now,
                                                 sequence=len(self._events) + 1))
            row = replace(row, state=transition.after.state.value, updated_at=now, **changes)
            self._rows[install_id] = row
            return row

    def request_uninstall(self, install_id: str, actor: str) -> InstallRow | None:
        with self._lock:
            row = self._rows.get(install_id)
            if row is None or not row.live:
                return None
            if row.uninstall_requested_by is None:
                row = replace(row, uninstall_requested_by=actor, updated_at=datetime.now(UTC))
                self._rows[install_id] = row
            return row

    def events(self, install_id: str) -> tuple[InstallEvent, ...]:
        return tuple(e for e in self._events if e.install_id == install_id)

    def pending(self) -> tuple[InstallRow, ...]:
        return tuple(r for r in self._rows.values() if _pending(r))

    def in_progress(self) -> tuple[InstallRow, ...]:
        return tuple(r for r in self._rows.values() if _in_progress(r))


def _pending(row: InstallRow) -> bool:
    asked = row.state == InstallState.PUBLISHED.value and row.controller is None
    removal = row.uninstall_requested_by is not None and row.live
    return asked or removal


def _in_progress(row: InstallRow) -> bool:
    return (row.controller is not None and row.state in IN_PROGRESS_STATES and row.failed_step is None
            and row.uninstall_requested_by is None)


_IN_PROGRESS_SQL = ("controller IS NOT NULL AND state IN ('published', 'fetched', 'bound') "
                    "AND failed_step IS NULL AND uninstall_requested_by IS NULL")


# --- SQLite ---------------------------------------------------------------------------------------


def _dump(value: Any) -> str:
    return json.dumps(value if value is not None else None)


def _row_from(values: Mapping[str, Any]) -> InstallRow:
    def when(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        return datetime.fromisoformat(str(value))

    card, binding = values["card"], values["binding"]
    return InstallRow(
        id=values["id"], org_id=values["org_id"], reference=values["reference"], digest=values["digest"],
        cog_id=values["cog_id"], card=json.loads(card) if isinstance(card, str) else (card or {}),
        state=values["state"], controller=values["controller"], lock_digest=values["lock_digest"],
        binding_id=values["binding_id"],
        binding=json.loads(binding) if isinstance(binding, str) else binding,
        failure=values["failure"], failed_step=values["failed_step"], requested_by=values["requested_by"],
        uninstall_requested_by=values["uninstall_requested_by"],
        created_at=when(values["created_at"]), updated_at=when(values["updated_at"]),
    )


class SqliteInstallStore:
    """The install store on one SQLite file: the same file as the SQLite Track, at dev level 1."""

    _SCHEMA = (
        """
        CREATE TABLE IF NOT EXISTS collab_cog_installs (
            id                     TEXT PRIMARY KEY,
            org_id                 TEXT,
            reference              TEXT NOT NULL,
            digest                 TEXT NOT NULL,
            cog_id                 TEXT,
            card                   TEXT NOT NULL DEFAULT '{}',
            state                  TEXT NOT NULL,
            controller             TEXT,
            lock_digest            TEXT,
            binding_id             TEXT,
            binding                TEXT,
            failure                TEXT,
            failed_step            TEXT,
            requested_by           TEXT,
            uninstall_requested_by TEXT,
            created_at             TEXT NOT NULL,
            updated_at             TEXT NOT NULL
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS collab_cog_installs_one_live "
        "ON collab_cog_installs (org_id, digest) WHERE state <> 'uninstalled'",
        "CREATE INDEX IF NOT EXISTS collab_cog_installs_state ON collab_cog_installs (state)",
        """
        CREATE TABLE IF NOT EXISTS collab_cog_install_events (
            sequence    INTEGER PRIMARY KEY AUTOINCREMENT,
            install_id  TEXT NOT NULL,
            event_type  TEXT NOT NULL,
            payload     TEXT NOT NULL DEFAULT '{}',
            occurred_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS collab_cog_install_events_install "
        "ON collab_cog_install_events (install_id, sequence)",
    )

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    @classmethod
    def ensure_schema(cls, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(path))) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            for statement in cls._SCHEMA:
                connection.execute(statement)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            connection.row_factory = sqlite3.Row
            yield connection

    def create(self, row: InstallRow) -> InstallRow:
        with self._connect() as connection:
            try:
                connection.execute(
                    f"INSERT INTO collab_cog_installs ({', '.join(_COLUMNS)}) "
                    f"VALUES ({', '.join('?' * len(_COLUMNS))})",
                    (row.id, row.org_id, row.reference, row.digest, row.cog_id, _dump(dict(row.card)), row.state,
                     row.controller, row.lock_digest, row.binding_id,
                     _dump(dict(row.binding)) if row.binding is not None else None, row.failure, row.failed_step,
                     row.requested_by, row.uninstall_requested_by, row.created_at.isoformat(),
                     row.updated_at.isoformat()))
            except sqlite3.IntegrityError as exc:
                if "collab_cog_installs.org_id, collab_cog_installs.digest" in str(exc):
                    raise InstallExists(f"{row.digest} is already installed") from exc
                raise
        return row

    def _select(self, connection: sqlite3.Connection, where: str, params: tuple) -> list[InstallRow]:
        rows = connection.execute(f"SELECT {', '.join(_COLUMNS)} FROM collab_cog_installs {where}", params)
        return [_row_from(dict(r)) for r in rows]

    def get(self, install_id: str) -> InstallRow | None:
        with self._connect() as connection:
            rows = self._select(connection, "WHERE id = ?", (install_id,))
        return rows[0] if rows else None

    def find(self, org_id, *, reference=None, digest=None, invokable=False) -> InstallRow | None:
        clauses, params = ["state <> 'uninstalled'", "org_id IS ?"], [org_id]
        if reference is not None:
            clauses.append("reference = ?")
            params.append(reference)
        if digest is not None:
            clauses.append("digest = ?")
            params.append(digest)
        if invokable:
            clauses.append("state = 'invokable'")
        with self._connect() as connection:
            rows = self._select(connection, "WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC",
                                tuple(params))
        return rows[0] if rows else None

    def list(self, *, org_id=None, state=None, include_uninstalled=False, limit=None, offset=0):
        clauses, params = [], []
        if org_id is not None:
            clauses.append("org_id = ?")
            params.append(org_id)
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if not include_uninstalled:
            clauses.append("state <> 'uninstalled'")
        where = ("WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created_at DESC, id DESC"
        if limit is not None:
            where += f" LIMIT {int(limit)} OFFSET {int(offset)}"
        elif offset:
            where += f" LIMIT -1 OFFSET {int(offset)}"
        with self._connect() as connection:
            return tuple(self._select(connection, where, tuple(params)))

    def claim(self, install_id: str, controller: str) -> bool:
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE collab_cog_installs SET controller = ?, updated_at = ? "
                "WHERE id = ? AND controller IS NULL AND state = 'published'",
                (controller, datetime.now(UTC).isoformat(), install_id)).rowcount
        return changed == 1

    def reclaim(self, install_id: str, from_controller: str, controller: str) -> bool:
        with self._connect() as connection:
            changed = connection.execute(
                f"UPDATE collab_cog_installs SET controller = ?, updated_at = ? "
                f"WHERE id = ? AND controller = ? AND {_IN_PROGRESS_SQL}",
                (controller, datetime.now(UTC).isoformat(), install_id, from_controller)).rowcount
        return changed == 1

    def record(self, install_id: str, transition: Transition[CogInstall], *, expected_state: str | None = None,
               **changes: Any) -> InstallRow:
        _check_changes(changes)
        now = datetime.now(UTC)
        values = dict(changes)
        if "binding" in values and values["binding"] is not None:
            values["binding"] = _dump(dict(values["binding"]))
        values["state"] = transition.after.state.value
        values["updated_at"] = now.isoformat()
        guard, params = ("", ()) if expected_state is None else (" AND state = ?", (expected_state,))
        with self._connect() as connection:
            # The row first, under the guard, so a move another controller made is never written over.
            assignments = ", ".join(f"{column} = ?" for column in values)
            changed = connection.execute(f"UPDATE collab_cog_installs SET {assignments} WHERE id = ?{guard}",
                                         (*values.values(), install_id, *params)).rowcount
            if changed != 1:
                connection.rollback()
                raise InstallConflict(f"install {install_id} is not {expected_state}")
            for record in transition.records:
                connection.execute(
                    "INSERT INTO collab_cog_install_events (install_id, event_type, payload, occurred_at) "
                    "VALUES (?, ?, ?, ?)", (install_id, record.event_type, _dump(dict(record.payload)),
                                            now.isoformat()))
            [row] = self._select(connection, "WHERE id = ?", (install_id,))
        return row

    def request_uninstall(self, install_id: str, actor: str) -> InstallRow | None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE collab_cog_installs SET uninstall_requested_by = ?, updated_at = ? "
                "WHERE id = ? AND uninstall_requested_by IS NULL AND state <> 'uninstalled'",
                (actor, datetime.now(UTC).isoformat(), install_id))
            rows = self._select(connection, "WHERE id = ? AND state <> 'uninstalled'", (install_id,))
        return rows[0] if rows else None

    def events(self, install_id: str) -> tuple[InstallEvent, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence, install_id, event_type, payload, occurred_at FROM collab_cog_install_events "
                "WHERE install_id = ? ORDER BY sequence", (install_id,)).fetchall()
        return tuple(InstallEvent(r["install_id"], r["event_type"], json.loads(r["payload"]),
                                  datetime.fromisoformat(r["occurred_at"]), r["sequence"]) for r in rows)

    def pending(self) -> tuple[InstallRow, ...]:
        with self._connect() as connection:
            return tuple(self._select(
                connection, "WHERE (state = 'published' AND controller IS NULL) "
                            "OR (uninstall_requested_by IS NOT NULL AND state <> 'uninstalled') ORDER BY created_at",
                ()))

    def in_progress(self) -> tuple[InstallRow, ...]:
        with self._connect() as connection:
            return tuple(self._select(connection, f"WHERE {_IN_PROGRESS_SQL} ORDER BY created_at", ()))


# --- Postgres --------------------------------------------------------------------------------------


class PostgresInstallStore:
    """The install store on the hub's database; its tables come from the hub's migrations (migration 18).

    ``_SCHEMA`` is the registry's install statements, in order, and a test
    holds the two equal; ``ensure_schema`` is for the standalone package and
    tests, never called on the hub.
    """

    _SCHEMA = (
        """
        CREATE TABLE IF NOT EXISTS collab_cog_installs (
            id                     text PRIMARY KEY,
            org_id                 text,
            reference              text NOT NULL,
            digest                 text NOT NULL,
            cog_id                 text,
            card                   jsonb NOT NULL DEFAULT '{}'::jsonb,
            state                  text NOT NULL,
            controller             text,
            lock_digest            text,
            binding_id             text,
            binding                jsonb,
            failure                text,
            failed_step            text,
            requested_by           text,
            uninstall_requested_by text,
            created_at             timestamptz NOT NULL DEFAULT now(),
            updated_at             timestamptz NOT NULL DEFAULT now()
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS collab_cog_installs_one_live "
        "ON collab_cog_installs (org_id, digest) WHERE state <> 'uninstalled'",
        "CREATE INDEX IF NOT EXISTS collab_cog_installs_state ON collab_cog_installs (state)",
        "CREATE SEQUENCE IF NOT EXISTS collab_cog_install_event_sequence",
        """
        CREATE TABLE IF NOT EXISTS collab_cog_install_events (
            sequence    bigint PRIMARY KEY DEFAULT nextval('collab_cog_install_event_sequence'),
            install_id  text NOT NULL,
            event_type  text NOT NULL,
            payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
            occurred_at timestamptz NOT NULL DEFAULT now()
        )
        """,
        "CREATE INDEX IF NOT EXISTS collab_cog_install_events_install "
        "ON collab_cog_install_events (install_id, sequence)",
    )

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    @staticmethod
    def ensure_schema(connection: Any) -> None:
        for statement in PostgresInstallStore._SCHEMA:
            connection.execute(statement)

    def create(self, row: InstallRow) -> InstallRow:
        with self.pool.connection() as connection:
            try:
                connection.execute(
                    f"INSERT INTO collab_cog_installs ({', '.join(_COLUMNS)}) VALUES "
                    f"(%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)",
                    (row.id, row.org_id, row.reference, row.digest, row.cog_id, _dump(dict(row.card)), row.state,
                     row.controller, row.lock_digest, row.binding_id,
                     _dump(dict(row.binding)) if row.binding is not None else None, row.failure, row.failed_step,
                     row.requested_by, row.uninstall_requested_by, row.created_at, row.updated_at))
            except Exception as exc:  # noqa: BLE001 - the driver's error class is not imported here
                if "collab_cog_installs_one_live" in str(exc):
                    raise InstallExists(f"{row.digest} is already installed") from exc
                raise
        return row

    @staticmethod
    def _select(connection: Any, where: str, params: tuple) -> list[InstallRow]:
        rows = connection.execute(f"SELECT {', '.join(_COLUMNS)} FROM collab_cog_installs {where}", params).fetchall()
        return [_row_from(dict(zip(_COLUMNS, r, strict=True))) for r in rows]

    def get(self, install_id: str) -> InstallRow | None:
        with self.pool.connection() as connection:
            rows = self._select(connection, "WHERE id = %s", (install_id,))
        return rows[0] if rows else None

    def find(self, org_id, *, reference=None, digest=None, invokable=False) -> InstallRow | None:
        clauses, params = ["state <> 'uninstalled'", "org_id IS NOT DISTINCT FROM %s"], [org_id]
        if reference is not None:
            clauses.append("reference = %s")
            params.append(reference)
        if digest is not None:
            clauses.append("digest = %s")
            params.append(digest)
        if invokable:
            clauses.append("state = 'invokable'")
        with self.pool.connection() as connection:
            rows = self._select(connection, "WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC",
                                tuple(params))
        return rows[0] if rows else None

    def list(self, *, org_id=None, state=None, include_uninstalled=False, limit=None, offset=0):
        clauses, params = [], []
        if org_id is not None:
            clauses.append("org_id = %s")
            params.append(org_id)
        if state is not None:
            clauses.append("state = %s")
            params.append(state)
        if not include_uninstalled:
            clauses.append("state <> 'uninstalled'")
        where = ("WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created_at DESC, id DESC"
        if limit is not None:
            where += f" LIMIT {int(limit)}"
        if offset:
            where += f" OFFSET {int(offset)}"
        with self.pool.connection() as connection:
            return tuple(self._select(connection, where, tuple(params)))

    def claim(self, install_id: str, controller: str) -> bool:
        with self.pool.connection() as connection:
            changed = connection.execute(
                "UPDATE collab_cog_installs SET controller = %s, updated_at = now() "
                "WHERE id = %s AND controller IS NULL AND state = 'published'", (controller, install_id)).rowcount
        return changed == 1

    def reclaim(self, install_id: str, from_controller: str, controller: str) -> bool:
        with self.pool.connection() as connection:
            changed = connection.execute(
                f"UPDATE collab_cog_installs SET controller = %s, updated_at = now() "
                f"WHERE id = %s AND controller = %s AND {_IN_PROGRESS_SQL}",
                (controller, install_id, from_controller)).rowcount
        return changed == 1

    def record(self, install_id: str, transition: Transition[CogInstall], *, expected_state: str | None = None,
               **changes: Any) -> InstallRow:
        _check_changes(changes)
        values = dict(changes)
        casts = {}
        if "binding" in values and values["binding"] is not None:
            values["binding"] = _dump(dict(values["binding"]))
            casts["binding"] = "::jsonb"
        values["state"] = transition.after.state.value
        guard, params = ("", ()) if expected_state is None else (" AND state = %s", (expected_state,))
        with self.pool.connection() as connection:
            # The row first, under the guard, so a move another controller made is never written over.
            assignments = ", ".join(f"{column} = %s{casts.get(column, '')}" for column in values)
            changed = connection.execute(
                f"UPDATE collab_cog_installs SET {assignments}, updated_at = now() WHERE id = %s{guard}",
                (*values.values(), install_id, *params)).rowcount
            if changed != 1:
                connection.rollback()
                raise InstallConflict(f"install {install_id} is not {expected_state}")
            for record in transition.records:
                connection.execute(
                    "INSERT INTO collab_cog_install_events (install_id, event_type, payload) "
                    "VALUES (%s, %s, %s::jsonb)",
                    (install_id, record.event_type, _dump(dict(record.payload))))
            [row] = self._select(connection, "WHERE id = %s", (install_id,))
        return row

    def request_uninstall(self, install_id: str, actor: str) -> InstallRow | None:
        with self.pool.connection() as connection:
            connection.execute(
                "UPDATE collab_cog_installs SET uninstall_requested_by = %s, updated_at = now() "
                "WHERE id = %s AND uninstall_requested_by IS NULL AND state <> 'uninstalled'", (actor, install_id))
            rows = self._select(connection, "WHERE id = %s AND state <> 'uninstalled'", (install_id,))
        return rows[0] if rows else None

    def events(self, install_id: str) -> tuple[InstallEvent, ...]:
        with self.pool.connection() as connection:
            rows = connection.execute(
                "SELECT sequence, install_id, event_type, payload, occurred_at FROM collab_cog_install_events "
                "WHERE install_id = %s ORDER BY sequence", (install_id,)).fetchall()
        return tuple(InstallEvent(r[1], r[2], r[3] if isinstance(r[3], dict) else json.loads(r[3]), r[4], r[0])
                     for r in rows)

    def pending(self) -> tuple[InstallRow, ...]:
        with self.pool.connection() as connection:
            return tuple(self._select(
                connection, "WHERE (state = 'published' AND controller IS NULL) "
                            "OR (uninstall_requested_by IS NOT NULL AND state <> 'uninstalled') ORDER BY created_at",
                ()))

    def in_progress(self) -> tuple[InstallRow, ...]:
        with self.pool.connection() as connection:
            return tuple(self._select(connection, f"WHERE {_IN_PROGRESS_SQL} ORDER BY created_at", ()))


__all__ = [
    "INSTALL_RUN_PREFIX", "InMemoryInstallStore", "InstallConflict", "InstallEvent", "InstallExists", "InstallRow",
    "InstallStore",
    "PostgresInstallStore", "SqliteInstallStore", "new_install_id",
]
