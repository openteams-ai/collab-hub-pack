"""Reading ``collab_audit_events``.

The writer lives in :mod:`.audit` and stays the only writer. This module reads,
and does nothing else: there is no update, no delete, and no insert here, which
is what makes "append-only" a property of the code rather than a convention
somebody remembers.

Keyset paging, not offset
-------------------------
The log is append-only and read newest-first, so a page is "the next *n* rows
below this id". That is one index seek, it is stable while the log is being
written to -- an ``OFFSET`` page shifts under you every time a row lands above
it, silently repeating or skipping entries -- and it never has to count the
rows it is skipping over.

What the caller gets back is the id to ask below next, rather than a page
number, because a page number would be a lie the moment the log grew.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "MAX_PAGE_SIZE",
    "AuditEntry",
    "AuditLog",
    "AuditLogUnavailableError",
    "AuditPage",
    "PostgresAuditLog",
    "UnavailableAuditLog",
]

MAX_PAGE_SIZE = 200
"""The most rows one request may take.

A cap rather than a default, and enforced here rather than at the route: the
limit is a property of what this store is willing to materialize, and a second
caller reaching it directly should not be able to ask for the whole table.
"""


class AuditLogUnavailableError(RuntimeError):
    """This deployment has no database, so there is no log to read.

    Its own error rather than an empty page, because "nothing was recorded" and
    "the record cannot be reached" mean opposite things to somebody
    investigating an incident, and a reader that conflated them would be worse
    than no reader at all.
    """


@dataclass(frozen=True)
class AuditEntry:
    """One recorded action, as the panel and the runbook read it."""

    id: int
    at: datetime
    actor: str
    actor_label: str | None
    action: str
    target_type: str | None
    target_id: str | None
    target_label: str | None
    org_id: str | None
    detail: dict | None


@dataclass(frozen=True)
class AuditPage:
    entries: list[AuditEntry]
    next_before_id: int | None
    """Ask for ``before_id`` of this to get the next page; ``None`` at the end."""


class AuditLog(ABC):
    @abstractmethod
    def list_events(
        self,
        *,
        limit: int,
        before_id: int | None = None,
        actor: str | None = None,
        action: str | None = None,
    ) -> AuditPage:
        raise NotImplementedError


class UnavailableAuditLog(AuditLog):
    """What a deployment with no database reads: nothing, loudly."""

    def list_events(self, **_kwargs) -> AuditPage:
        raise AuditLogUnavailableError("this deployment records no audit log: it has no configured database")


class PostgresAuditLog(AuditLog):
    def __init__(self, db) -> None:
        self._db = db

    def list_events(
        self,
        *,
        limit: int,
        before_id: int | None = None,
        actor: str | None = None,
        action: str | None = None,
    ) -> AuditPage:
        size = max(1, min(limit, MAX_PAGE_SIZE))
        clauses: list[str] = []
        params: list[object] = []
        if before_id is not None:
            clauses.append("id < %s")
            params.append(before_id)
        if actor:
            clauses.append("actor = %s")
            params.append(actor)
        if action:
            clauses.append("action = %s")
            params.append(action)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        # One row more than asked for, so "is there a next page" is answered by
        # the same query rather than by a second COUNT over a growing table.
        params.append(size + 1)

        with self._db.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT id, at, actor, actor_label, action,
                       target_type, target_id, target_label, org_id, detail
                FROM collab_audit_events
                {where}
                ORDER BY id DESC
                LIMIT %s
                """,
                tuple(params),
            ).fetchall()

        entries = [_entry(row) for row in rows[:size]]
        has_more = len(rows) > size
        return AuditPage(
            entries=entries,
            next_before_id=entries[-1].id if has_more and entries else None,
        )


def _entry(row) -> AuditEntry:
    return AuditEntry(
        id=row["id"],
        at=row["at"],
        actor=row["actor"],
        actor_label=row["actor_label"],
        action=row["action"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        target_label=row["target_label"],
        org_id=row["org_id"],
        detail=row["detail"],
    )
