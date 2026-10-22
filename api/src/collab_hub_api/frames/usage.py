"""Usage-statistics storage backends.

Usage capture has two halves:

- **Seen users** — every authenticated request upserts the caller into a
  per-tenant user roster (identity, email, first/last seen). Recording is
  best-effort and throttled so the auth path never gains a per-request write.
- **Usage events** — client-reported activity the hub cannot observe itself
  (today: ``chat_created`` from the desktop app). Events are append-only rows,
  aggregated by the usage endpoints alongside the existing frame history and
  active-state tables.

Backend semantics mirror ``history.py``: usage is a relational feature that
rides the shared ``frames.postgres`` URL. ``InMemory`` exists only for
tests/dev; with no DB configured the builder returns ``UnavailableUsageStore``.
On the unavailable store, best-effort writes (``record_user_seen``) are no-ops
while event recording and all reads raise ``UsageUnavailableError`` (→ 503).
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from .best_effort import BestEffortWriter
from .db import locked_schema_connection
from .history import cap_detail

usage_logger = logging.getLogger("frames_server.usage")

# Minimum seconds between persisted ``last_seen`` refreshes for one caller.
# Auth runs on every request; without a throttle each request would cost an
# extra Postgres write. First sight (or a changed email) always writes.
SEEN_WRITE_INTERVAL_SECONDS = 300.0

# Backoff after a failed seen-user write. Retrying on the very next request
# would turn a database outage or a saturated pool into a per-request retry
# storm on the auth path, so failures back off exponentially from
# ``SEEN_RETRY_BACKOFF_SECONDS`` up to the ordinary throttle window.
SEEN_RETRY_BACKOFF_SECONDS = 1.0
SEEN_RETRY_BACKOFF_MAX_SECONDS = SEEN_WRITE_INTERVAL_SECONDS


class UsageUnavailableError(RuntimeError):
    """Raised when usage statistics are requested but no backend is configured."""

    pass


@dataclass(frozen=True)
class UsageUser:
    """One authenticated user seen by the API within a tenant."""

    user: str
    email: str | None
    first_seen: datetime
    last_seen: datetime


@dataclass(frozen=True)
class HubOrganizationUsage:
    """One organization's share of the hub, for the operator roll-up."""

    org_id: str
    users: int
    events: int


@dataclass(frozen=True)
class HubUsage:
    """Activity across the whole deployment.

    ``users_total`` counts **people**, not memberships: somebody who belongs to
    two organizations is one user of this hub. The per-organization numbers do
    count them in each, because "how big is org-a" and "how big is this hub"
    are different questions and only one of them is a sum.
    """

    users_total: int
    events_total: int
    events: list[tuple[str, int]]
    organizations: list[HubOrganizationUsage]


@dataclass(frozen=True)
class UsageEventCount:
    """Aggregated count of one client-reported event kind for one user."""

    event: str
    user: str
    count: int


def _within(at: datetime, since: datetime | None, until: datetime | None) -> bool:
    """Whether *at* falls inside ``[since, until)``, either end optional.

    Half-open, matching the Postgres queries (``>= since``, ``< until``).
    """

    if since is not None and at < since:
        return False
    if until is not None and at >= until:
        return False
    return True


def _organizations(users: dict[str, int], events: dict[str, int]) -> list[HubOrganizationUsage]:
    """One row per organization that has people *or* events in the window.

    From both sides, because an organization whose events are counted in the
    hub total but which has no roster row would otherwise be missing from the
    breakdown the total is supposed to add up from.
    """

    return [
        HubOrganizationUsage(org_id=org_id, users=users.get(org_id, 0), events=events.get(org_id, 0))
        for org_id in sorted(users.keys() | events.keys())
    ]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seen_retry_delay(failures: int) -> float:
    """Exponential backoff for a failed seen-user write, capped at the throttle window."""

    delay = SEEN_RETRY_BACKOFF_SECONDS * (2 ** (failures - 1))
    return min(delay, SEEN_RETRY_BACKOFF_MAX_SECONDS)


class UsageStore(ABC):
    """Tenant-scoped roster of authenticated users plus client usage events."""

    @abstractmethod
    def record_user_seen(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        email: str | None,
    ) -> None:
        """Upsert one authenticated caller into the seen-user roster.

        Called from the auth path, so implementations must stay cheap, may
        throttle repeat writes for the same caller, and must not make the
        caller wait on a database.
        """

        raise NotImplementedError

    def close(self) -> None:
        """Release anything the store holds. Safe to call on any backend."""

        return None

    @abstractmethod
    def record_event(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        event: str,
        detail: dict | None = None,
    ) -> None:
        """Append one client-reported usage event."""

        raise NotImplementedError

    @abstractmethod
    def list_users(self, org_id: str, workspace_id: str) -> list[UsageUser]:
        """Return every seen user in the tenant, most recently seen first."""

        raise NotImplementedError

    @abstractmethod
    def count_events(
        self,
        org_id: str,
        workspace_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[UsageEventCount]:
        """Return per-user counts of each event kind within a time window."""

        raise NotImplementedError

    @abstractmethod
    def hub_summary(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> HubUsage:
        """Return activity across every organization on this deployment.

        Takes no tenant arguments, which is the whole point: every other read
        here is scoped to one, and this is the operator's question rather than
        a member's. It is a single method rather than hub-wide variants of the
        three reads above because the roll-up is one answer -- returning it in
        pieces would let a caller mix a users figure from one moment with an
        events figure from another and present the pair as a summary.

        The window bounds events only. The roster is current state: "how many
        people does this hub have" does not become a smaller number because you
        asked about last week.
        """

        raise NotImplementedError


class UnavailableUsageStore(UsageStore):
    """Store returned when no shared frames Postgres is configured.

    Seen-user recording is a no-op (best-effort relative to auth), while event
    recording and reads raise ``UsageUnavailableError`` so the usage endpoints
    return 503. This is the only off state — there is no per-feature toggle.
    """

    def record_user_seen(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        email: str | None,
    ) -> None:
        """No-op: recording is best-effort and no DB is configured."""

        return None

    def record_event(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        event: str,
        detail: dict | None = None,
    ) -> None:
        """Reject event writes because no frames Postgres is configured (→ 503)."""

        raise UsageUnavailableError("Usage statistics are not configured")

    def list_users(self, org_id: str, workspace_id: str) -> list[UsageUser]:
        """Reject reads because no frames Postgres is configured (→ 503)."""

        raise UsageUnavailableError("Usage statistics are not configured")

    def count_events(
        self,
        org_id: str,
        workspace_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[UsageEventCount]:
        """Reject reads because no frames Postgres is configured (→ 503)."""

        raise UsageUnavailableError("Usage statistics are not configured")

    def hub_summary(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> HubUsage:
        """Refused for the same reason every other read here is.

        An operator looking at a hub-wide page must never be shown zeros that
        mean "no database" while reading as "no activity".
        """

        raise UsageUnavailableError("Usage statistics are not configured")


@dataclass
class _SeenUser:
    email: str | None
    first_seen: datetime
    last_seen: datetime


@dataclass(frozen=True)
class _Event:
    org_id: str
    workspace_id: str
    user: str
    event: str
    detail: dict | None
    created_at: datetime


class InMemoryUsageStore(UsageStore):
    """Process-local usage store for tests and narrow dev scenarios.

    Unthrottled: every ``record_user_seen`` refreshes ``last_seen`` so tests
    stay deterministic.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._users: dict[tuple[str, str, str], _SeenUser] = {}
        self._events: list[_Event] = []

    def record_user_seen(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        email: str | None,
    ) -> None:
        """Upsert one caller into process-local memory."""

        now = _now()
        with self._lock:
            existing = self._users.get((org_id, workspace_id, user))
            if existing is None:
                self._users[(org_id, workspace_id, user)] = _SeenUser(
                    email=email,
                    first_seen=now,
                    last_seen=now,
                )
            else:
                existing.last_seen = now
                if email:
                    existing.email = email

    def record_event(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        event: str,
        detail: dict | None = None,
    ) -> None:
        """Append one usage event to process-local memory."""

        entry = _Event(
            org_id=org_id,
            workspace_id=workspace_id,
            user=user,
            event=event,
            detail=cap_detail(detail),
            created_at=_now(),
        )
        with self._lock:
            self._events.append(entry)

    def list_users(self, org_id: str, workspace_id: str) -> list[UsageUser]:
        """Return the tenant's seen users from process-local memory."""

        with self._lock:
            users = [
                UsageUser(
                    user=user,
                    email=record.email,
                    first_seen=record.first_seen,
                    last_seen=record.last_seen,
                )
                for (item_org, item_workspace, user), record in self._users.items()
                if item_org == org_id and item_workspace == workspace_id
            ]
        users.sort(key=lambda u: u.last_seen, reverse=True)
        return users

    def hub_summary(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> HubUsage:
        """Aggregate every organization from process-local memory."""

        with self._lock:
            users = list(self._users.items())
            events = [entry for entry in self._events if _within(entry.created_at, since, until)]

        people: set[str] = set()
        org_users: dict[str, set[str]] = {}
        for (org_id, _workspace_id, user), _seen in users:
            people.add(user)
            org_users.setdefault(org_id, set()).add(user)

        org_events: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for entry in events:
            org_events[entry.org_id] = org_events.get(entry.org_id, 0) + 1
            by_kind[entry.event] = by_kind.get(entry.event, 0) + 1

        return HubUsage(
            users_total=len(people),
            events_total=len(events),
            events=sorted(by_kind.items()),
            organizations=_organizations(
                {org_id: len(members) for org_id, members in org_users.items()}, org_events
            ),
        )

    def count_events(
        self,
        org_id: str,
        workspace_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[UsageEventCount]:
        """Aggregate matching events from process-local memory."""

        counts: dict[tuple[str, str], int] = {}
        with self._lock:
            for entry in self._events:
                if entry.org_id != org_id or entry.workspace_id != workspace_id:
                    continue
                if not _within(entry.created_at, since, until):
                    continue
                key = (entry.event, entry.user)
                counts[key] = counts.get(key, 0) + 1
        return sorted(
            (UsageEventCount(event=event, user=user, count=count) for (event, user), count in counts.items()),
            key=lambda c: (c.event, -c.count, c.user),
        )


class PostgresUsageStore(UsageStore):
    """Postgres-backed usage store for production-style deployments.

    Mirrors ``PostgresFrameHistoryStore``: the database lives outside the pack;
    the pack only needs a pooled database handle (``db.PostgresDatabase``) and
    creates its tables if missing. ``record_user_seen`` runs inside auth, so it
    never touches the database on the calling thread: it throttles per caller,
    hands the write to a bounded background writer, and backs off after
    failures instead of retrying on the next request.
    """

    def __init__(self, db, auto_migrate: bool = False, writer: BestEffortWriter | None = None):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("PostgresUsageStore requires psycopg") from exc

        self.db = db
        self.database_url = db.database_url
        self.psycopg = psycopg
        self._writer = writer if writer is not None else BestEffortWriter(name="usage-seen-write")
        self._seen_lock = threading.Lock()
        # (org, workspace, user, email) -> monotonic deadline before which the
        # next write is skipped. Keying on the email means a changed claim
        # writes through immediately.
        self._seen_next_write: dict[tuple[str, str, str, str | None], float] = {}
        # Consecutive failures per caller, driving the retry backoff.
        self._seen_failures: dict[tuple[str, str, str, str | None], int] = {}
        if auto_migrate:
            self._ensure_schema()

    def record_user_seen(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        email: str | None,
    ) -> None:
        """Queue one caller's roster upsert, at most once per throttle window.

        Returns without waiting on the database. Auth runs this for every
        authenticated HTTP request and, through the synchronous authenticator
        the async MCP middleware calls, for every MCP one — so the only work it
        may do on the caller's thread is the in-memory throttle bookkeeping.
        """

        key = (org_id, workspace_id, user, email)
        now = time.monotonic()
        with self._seen_lock:
            next_write = self._seen_next_write.get(key)
            if next_write is not None and now < next_write:
                return
            # Claim the window before leaving the lock so concurrent callers
            # for the same identity do not stampede the pool.
            self._seen_next_write[key] = now + SEEN_WRITE_INTERVAL_SECONDS
        if not self._writer.submit(lambda: self._write_user_seen(key)):
            # The writer is saturated or shutting down. Dropping is the point of
            # a best-effort write; the caller is simply retried after a backoff.
            self._record_seen_failure(key, reason="dropped")

    def _write_user_seen(self, key: tuple[str, str, str, str | None]) -> None:
        """Perform one roster upsert. Runs on a background writer thread."""

        org_id, workspace_id, user, email = key
        try:
            with self._connect_best_effort() as conn:
                conn.execute(
                    """
                    INSERT INTO frames_server_usage_users (
                        org_id, workspace_id, user_id, email, first_seen, last_seen
                    )
                    VALUES (%s, %s, %s, %s, now(), now())
                    ON CONFLICT (org_id, workspace_id, user_id)
                    DO UPDATE SET
                        last_seen = now(),
                        email = COALESCE(EXCLUDED.email, frames_server_usage_users.email)
                    """,
                    (org_id, workspace_id, user, email),
                )
        except Exception:
            self._record_seen_failure(key, reason="failed")
        else:
            with self._seen_lock:
                self._seen_failures.pop(key, None)

    def _record_seen_failure(self, key: tuple[str, str, str, str | None], reason: str) -> None:
        """Back the caller off after a write that did not land.

        Retried sooner than the ordinary throttle window, but never on the very
        next request: an outage or a saturated pool would otherwise have every
        authenticated request queue the same doomed write.
        """

        from .observability import USAGE_WRITE_FAILURES

        with self._seen_lock:
            failures = self._seen_failures.get(key, 0) + 1
            self._seen_failures[key] = failures
            self._seen_next_write[key] = time.monotonic() + _seen_retry_delay(failures)
        USAGE_WRITE_FAILURES.labels(kind=f"user_seen_{reason}").inc()
        usage_logger.warning(
            "usage_user_seen_write_%s",
            reason,
            extra={"user": key[2], "consecutive_failures": failures},
        )

    def close(self) -> None:
        """Stop the background writer, bounded; queued accounting is dropped."""

        self._writer.close()

    def record_event(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        event: str,
        detail: dict | None = None,
    ) -> None:
        """Insert one usage event into Postgres."""

        capped = cap_detail(detail)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO frames_server_usage_events (
                    id, org_id, workspace_id, user_id, event, detail
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid4().hex,
                    org_id,
                    workspace_id,
                    user,
                    event,
                    self.psycopg.types.json.Jsonb(capped) if capped is not None else None,
                ),
            )

    def list_users(self, org_id: str, workspace_id: str) -> list[UsageUser]:
        """Return the tenant's seen users from Postgres, most recent first."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, email, first_seen, last_seen
                FROM frames_server_usage_users
                WHERE org_id = %s AND workspace_id = %s
                ORDER BY last_seen DESC, user_id
                """,
                (org_id, workspace_id),
            ).fetchall()
        return [
            UsageUser(
                user=row["user_id"],
                email=row["email"],
                first_seen=row["first_seen"],
                last_seen=row["last_seen"],
            )
            for row in rows
        ]

    def hub_summary(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> HubUsage:
        """Aggregate every organization, in three grouped reads.

        Three statements on one connection rather than one statement with two
        joins: the roster and the events have different shapes (current state
        against a windowed count) and joining them would multiply rows before
        aggregating, which is the classic way to report a user count that grows
        with how chatty people were.

        The distinct-user total is asked of the database rather than assembled
        in Python, so a hub with a large roster does not stream the whole table
        into this process to count it.
        """

        window = ""
        window_params: list[object] = []
        if since is not None:
            window += " AND created_at >= %s"
            window_params.append(since)
        if until is not None:
            window += " AND created_at < %s"
            window_params.append(until)

        with self._connect() as conn:
            org_rows = conn.execute(
                """
                SELECT org_id, count(DISTINCT user_id) AS users
                FROM frames_server_usage_users
                GROUP BY org_id
                ORDER BY org_id
                """
            ).fetchall()
            people = conn.execute(
                "SELECT count(DISTINCT user_id) AS users FROM frames_server_usage_users"
            ).fetchone()
            event_rows = conn.execute(
                f"""
                SELECT org_id, event, count(*) AS count
                FROM frames_server_usage_events
                WHERE true{window}
                GROUP BY org_id, event
                ORDER BY org_id, event
                """,
                tuple(window_params),
            ).fetchall()

        org_events: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for row in event_rows:
            org_events[row["org_id"]] = org_events.get(row["org_id"], 0) + row["count"]
            by_kind[row["event"]] = by_kind.get(row["event"], 0) + row["count"]

        return HubUsage(
            users_total=(people or {}).get("users", 0),
            events_total=sum(by_kind.values()),
            events=sorted(by_kind.items()),
            organizations=_organizations({row["org_id"]: row["users"] for row in org_rows}, org_events),
        )

    def count_events(
        self,
        org_id: str,
        workspace_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[UsageEventCount]:
        """Aggregate matching events in Postgres."""

        params: list[object] = [org_id, workspace_id]
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
                SELECT event, user_id, count(*) AS count
                FROM frames_server_usage_events
                WHERE org_id = %s AND workspace_id = %s{window_clause}
                GROUP BY event, user_id
                ORDER BY event, count DESC, user_id
                """,
                tuple(params),
            ).fetchall()
        return [
            UsageEventCount(event=row["event"], user=row["user_id"], count=row["count"])
            for row in rows
        ]

    def _ensure_schema(self) -> None:
        # Advisory-locked (issue #42): bare CREATE TABLE IF NOT EXISTS races
        # under concurrent replica startup.
        with locked_schema_connection(self.db) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS frames_server_usage_users (
                    org_id       text NOT NULL,
                    workspace_id text NOT NULL,
                    user_id      text NOT NULL,
                    email        text,
                    first_seen   timestamptz NOT NULL DEFAULT now(),
                    last_seen    timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (org_id, workspace_id, user_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS frames_server_usage_events (
                    id           text PRIMARY KEY,
                    org_id       text NOT NULL,
                    workspace_id text NOT NULL,
                    user_id      text NOT NULL,
                    event        text NOT NULL,
                    detail       jsonb,
                    created_at   timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS frames_server_usage_events_lookup
                ON frames_server_usage_events (
                    org_id, workspace_id, created_at DESC
                )
                """
            )

    def _connect(self):
        # A transaction-scoped checkout from the shared pool — never a fresh
        # per-request psycopg.connect.
        return self.db.connection()

    def _connect_best_effort(self):
        # Seen-user capture runs inside auth, on both the HTTP and the MCP
        # request paths, so it waits milliseconds for a connection rather than
        # the full pool timeout: losing the write is cheaper than holding the
        # request (or the MCP event loop) behind a saturated pool.
        return self.db.best_effort_connection()
