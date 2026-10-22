"""Frame change-history storage backends.

This is an **event log, not document versioning**: each row records who changed
what (the API-level event), when — never the Frame body or any past document
content. It complements the fire-and-forget ``audit_event`` logging in
``observability.py`` by being persistent and queryable by entity id.

History is a required relational feature with **no per-feature toggle**: there
is no ``Disabled`` backend. ``InMemory`` exists only for tests/dev; ``Postgres``
is used whenever the shared ``frames.postgres`` URL is set. With no DB
configured the builder returns ``UnavailableFrameHistoryStore``, whose use
raises ``HistoryUnavailableError`` (→ 503) — the only off state.
"""

from __future__ import annotations

import base64
import binascii
import json
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from .db import locked_schema_connection

# Defensive cap on the JSON-encoded ``detail`` payload. History is metadata, not
# content; anything larger is a bug or an attempt to smuggle a body in.
DETAIL_MAX_BYTES = 4096

Cursor = tuple[datetime, str]


class HistoryUnavailableError(RuntimeError):
    """Raised when Frame history is requested but no backend is configured."""

    pass


class InvalidCursorError(ValueError):
    """Raised when a pagination cursor cannot be decoded or validated."""

    pass


@dataclass(frozen=True)
class HistoryEntry:
    """One recorded change event for a Frame or Frame group."""

    id: str
    org_id: str
    workspace_id: str
    entity_type: str
    entity_id: str
    event: str
    actor: str
    detail: dict | None
    created_at: datetime


@dataclass(frozen=True)
class HistoryEventCount:
    """Aggregated count of one event kind by one actor (usage reporting)."""

    event: str
    actor: str
    count: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


def cap_detail(detail: dict | None) -> dict | None:
    """Return ``detail`` if it serializes within the size cap, else a marker.

    Never returns the original ``body`` content: callers are responsible for
    not passing it, and this guard keeps any accidentally-large payload out of
    the store.
    """

    if detail is None:
        return None
    encoded = json.dumps(detail, separators=(",", ":"), default=str)
    if len(encoded.encode("utf-8")) > DETAIL_MAX_BYTES:
        return {"truncated": True}
    return detail


def encode_cursor(created_at: datetime, entry_id: str) -> str:
    """Encode ``(created_at, id)`` as an opaque, URL-safe cursor token."""

    raw = json.dumps([created_at.isoformat(), entry_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(token: str) -> Cursor:
    """Decode an opaque cursor token into a ``(created_at, id)`` tuple.

    Raises ``InvalidCursorError`` on any malformed input so the router can map
    it to a 400 ``invalid_cursor`` response.
    """

    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        decoded = json.loads(raw)
        created_at_raw, entry_id = decoded
        created_at = datetime.fromisoformat(created_at_raw)
    except (ValueError, binascii.Error, TypeError, json.JSONDecodeError) as exc:
        raise InvalidCursorError("Malformed pagination cursor") from exc
    if not isinstance(entry_id, str):
        raise InvalidCursorError("Malformed pagination cursor")
    return created_at, entry_id


class FrameHistoryStore(ABC):
    """Queryable, append-only audit trail of Frame and group changes."""

    @abstractmethod
    def record(
        self,
        org_id: str,
        workspace_id: str,
        entity_type: str,
        entity_id: str,
        event: str,
        actor: str,
        detail: dict | None = None,
    ) -> None:
        """Append one change event. ``actor`` is the Hub user, never a document author."""

        raise NotImplementedError

    @abstractmethod
    def query(
        self,
        org_id: str,
        workspace_id: str,
        entity_type: str,
        entity_id: str,
        limit: int,
        before: Cursor | None = None,
    ) -> list[HistoryEntry]:
        """Return change events newest-first, optionally older than ``before``."""

        raise NotImplementedError

    @abstractmethod
    def count_events(
        self,
        org_id: str,
        workspace_id: str,
        entity_type: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[HistoryEventCount]:
        """Return per-actor counts of each event kind within a time window."""

        raise NotImplementedError


class UnavailableFrameHistoryStore(FrameHistoryStore):
    """Store returned when no shared frames Postgres is configured.

    Recording is a no-op (best-effort relative to the mutation), while reads
    raise ``HistoryUnavailableError`` so the history endpoint returns 503. This
    is the only off state — there is no per-feature ``disabled`` toggle.
    """

    def record(
        self,
        org_id: str,
        workspace_id: str,
        entity_type: str,
        entity_id: str,
        event: str,
        actor: str,
        detail: dict | None = None,
    ) -> None:
        """No-op: recording is best-effort and no DB is configured."""

        return None

    def query(
        self,
        org_id: str,
        workspace_id: str,
        entity_type: str,
        entity_id: str,
        limit: int,
        before: Cursor | None = None,
    ) -> list[HistoryEntry]:
        """Reject reads because no frames Postgres is configured (→ 503)."""

        raise HistoryUnavailableError("Frame history is not configured")

    def count_events(
        self,
        org_id: str,
        workspace_id: str,
        entity_type: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[HistoryEventCount]:
        """Reject reads because no frames Postgres is configured (→ 503)."""

        raise HistoryUnavailableError("Frame history is not configured")


class InMemoryFrameHistoryStore(FrameHistoryStore):
    """Process-local history store for tests and narrow dev scenarios."""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: list[HistoryEntry] = []

    def record(
        self,
        org_id: str,
        workspace_id: str,
        entity_type: str,
        entity_id: str,
        event: str,
        actor: str,
        detail: dict | None = None,
    ) -> None:
        """Append one change event to process-local memory."""

        entry = HistoryEntry(
            id=uuid4().hex,
            org_id=org_id,
            workspace_id=workspace_id,
            entity_type=entity_type,
            entity_id=entity_id,
            event=event,
            actor=actor,
            detail=cap_detail(detail),
            created_at=_now(),
        )
        with self._lock:
            self._entries.append(entry)

    def query(
        self,
        org_id: str,
        workspace_id: str,
        entity_type: str,
        entity_id: str,
        limit: int,
        before: Cursor | None = None,
    ) -> list[HistoryEntry]:
        """Return matching events newest-first from process-local memory."""

        with self._lock:
            matches = [
                entry
                for entry in self._entries
                if entry.org_id == org_id
                and entry.workspace_id == workspace_id
                and entry.entity_type == entity_type
                and entry.entity_id == entity_id
            ]
        matches.sort(key=lambda e: (e.created_at, e.id), reverse=True)
        if before is not None:
            matches = [e for e in matches if (e.created_at, e.id) < before]
        return matches[:limit]

    def count_events(
        self,
        org_id: str,
        workspace_id: str,
        entity_type: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[HistoryEventCount]:
        """Aggregate matching events from process-local memory."""

        counts: dict[tuple[str, str], int] = {}
        with self._lock:
            for entry in self._entries:
                if (
                    entry.org_id != org_id
                    or entry.workspace_id != workspace_id
                    or entry.entity_type != entity_type
                ):
                    continue
                if since is not None and entry.created_at < since:
                    continue
                if until is not None and entry.created_at >= until:
                    continue
                key = (entry.event, entry.actor)
                counts[key] = counts.get(key, 0) + 1
        return sorted(
            (HistoryEventCount(event=event, actor=actor, count=count) for (event, actor), count in counts.items()),
            key=lambda c: (c.event, -c.count, c.actor),
        )


class PostgresFrameHistoryStore(FrameHistoryStore):
    """Postgres-backed history for production-style deployments.

    Mirrors ``PostgresActiveFrameStore``: the database lives outside the pack
    (for example in RDS); the pack only needs a pooled database handle
    (``db.PostgresDatabase``) and creates its table if it is missing.
    """

    def __init__(self, db, auto_migrate: bool = False):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("PostgresFrameHistoryStore requires psycopg") from exc

        self.db = db
        self.database_url = db.database_url
        self.psycopg = psycopg
        if auto_migrate:
            self._ensure_schema()

    def record(
        self,
        org_id: str,
        workspace_id: str,
        entity_type: str,
        entity_id: str,
        event: str,
        actor: str,
        detail: dict | None = None,
    ) -> None:
        """Insert one change event into Postgres."""

        capped = cap_detail(detail)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO frames_server_history (
                    id, org_id, workspace_id, entity_type, entity_id, event, actor, detail
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid4().hex,
                    org_id,
                    workspace_id,
                    entity_type,
                    entity_id,
                    event,
                    actor,
                    self.psycopg.types.json.Jsonb(capped) if capped is not None else None,
                ),
            )

    def query(
        self,
        org_id: str,
        workspace_id: str,
        entity_type: str,
        entity_id: str,
        limit: int,
        before: Cursor | None = None,
    ) -> list[HistoryEntry]:
        """Return matching events newest-first from Postgres, after ``before``."""

        params: list[object] = [org_id, workspace_id, entity_type, entity_id]
        cursor_clause = ""
        if before is not None:
            cursor_clause = "AND (created_at, id) < (%s, %s)"
            params.extend([before[0], before[1]])
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, org_id, workspace_id, entity_type, entity_id, event, actor, detail, created_at
                FROM frames_server_history
                WHERE org_id = %s AND workspace_id = %s AND entity_type = %s AND entity_id = %s
                {cursor_clause}
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                tuple(params),
            ).fetchall()
        return [
            HistoryEntry(
                id=row["id"],
                org_id=row["org_id"],
                workspace_id=row["workspace_id"],
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                event=row["event"],
                actor=row["actor"],
                detail=row["detail"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def count_events(
        self,
        org_id: str,
        workspace_id: str,
        entity_type: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[HistoryEventCount]:
        """Aggregate matching events in Postgres."""

        params: list[object] = [org_id, workspace_id, entity_type]
        window_clause = ""
        if since is not None:
            window_clause += " AND created_at >= %s"
            params.append(since)
        if until is not None:
            window_clause += " AND created_at < %s"
            params.append(until)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT event, actor, count(*) AS count
                FROM frames_server_history
                WHERE org_id = %s AND workspace_id = %s AND entity_type = %s{window_clause}
                GROUP BY event, actor
                ORDER BY event, count DESC, actor
                """,
                tuple(params),
            ).fetchall()
        return [
            HistoryEventCount(event=row["event"], actor=row["actor"], count=row["count"])
            for row in rows
        ]

    def _ensure_schema(self) -> None:
        # Advisory-locked (issue #42): bare CREATE TABLE IF NOT EXISTS races
        # under concurrent replica startup.
        with locked_schema_connection(self.db) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS frames_server_history (
                    id           text PRIMARY KEY,
                    org_id       text NOT NULL,
                    workspace_id text NOT NULL,
                    entity_type  text NOT NULL,
                    entity_id    text NOT NULL,
                    event        text NOT NULL,
                    actor        text NOT NULL,
                    detail       jsonb,
                    created_at   timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS frames_server_history_lookup
                ON frames_server_history (
                    org_id, workspace_id, entity_type, entity_id, created_at DESC, id DESC
                )
                """
            )

    def _connect(self):
        # A transaction-scoped checkout from the shared pool — never a fresh
        # per-request psycopg.connect.
        return self.db.connection()
