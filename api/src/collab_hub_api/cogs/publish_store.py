"""Repository ownership and upload sessions for publishing through the Hub (issue #180).

Two small relations, both created by migration 14 of
:mod:`..frames.collab_schema` and never by this module:

- ``collab_cog_repositories`` -- **who owns a repository published through
  the Hub**. One row per repository path, written *before* the first
  manifest for it is forwarded to the registry, as ``pending`` and already
  belonging to the publisher's organization; it becomes ``committed`` once
  the registry has accepted a manifest. A pending row is deleted only when
  the registry has **definitely refused** every manifest that organization
  had in flight for it (the row names them, by attempt id). An unknown outcome leaves it
  pending and owned: the registry may hold the manifest, so the name is
  never handed to another organization automatically. Only the same
  organization can publish to a pending name; a platform operator can
  release one that is stuck (``docs/cog-registry.md``). Committed rows
  extend the publish source's enumeration, and so do pending rows older than
  a short grace period, so that a sweep can find content whose commit was
  lost -- and commits the row when it does.
- ``collab_cog_upload_sessions`` -- **an upload in progress**. A client
  uploads a blob over several requests that may land on different replicas,
  so the session lives here: the Hub's own session id (the only one a client
  ever sees), who opened it, and the backing registry's session URL, which
  never leaves the Hub. The row is written *before* the registry is asked to
  open its session, and is held by its opener's *lease* until the registry's
  location is attached. Afterwards one request at a time holds the lease
  while it forwards bytes, and gives it back only when the outcome is
  recorded: a lease that lapses means nobody knows what the registry took,
  and the session is dead. A dead or expired session is kept until its
  registry session has been cancelled (see
  :meth:`PublishStore.claim_stale_uploads`).

Three backends, as for the catalog: Postgres over the shared pool, in memory
for tests and single-process development, and one that refuses. Every
Postgres call spends from the request's budget
(:func:`.deadline.bounded_connection`), like the other serving stores.
"""

from __future__ import annotations

import secrets
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from .deadline import bounded_connection

UPLOAD_ID_PREFIX = "up-"
UPLOAD_SESSION_TTL_SECONDS = 3600
"""How long an upload may sit idle: renewed by every chunk recorded, so an active upload never runs out.

A single write is bounded by its lease (``max_blob_seconds`` plus a margin),
not by this: a write that outlasts the session's idle time is still recorded
while its lease is live (:meth:`PublishStore.advance_upload`).
"""

UPLOAD_HARD_AGE_SECONDS = 24 * 3600
"""How long a session whose registry-side cancellation keeps failing is kept before it is dropped regardless."""

MAX_UPLOAD_SESSIONS_PER_USER = 64
"""Open uploads one user may hold; opening past it retires their oldest.

A client pushing a bundle opens one session per layer, a few at a time. The
cap bounds what a looping or hostile client can make the table (and the
backing registry's own session store) hold.
"""

MAX_UPLOAD_ROWS_PER_USER = 2 * MAX_UPLOAD_SESSIONS_PER_USER
"""Live sessions plus retired ones still awaiting cancellation at the registry; past it, opening is refused."""

PENDING_ENUMERATION_GRACE_SECONDS = 120.0
"""How long a repository stays pending before sweeps are sent to it.

Longer than any request that can be forwarding its first manifest, so a
sweep does not go looking while the publish that will commit the row is
still on its way.
"""


class PublishStoreUnavailableError(RuntimeError):
    """Raised when publishing state is needed but no backend is configured."""


class PendingLimitError(RuntimeError):
    """This organization already has as many unsettled repositories as it may; no new name was taken."""


class UploadLimitError(RuntimeError):
    """This user has too many sessions the registry has not let go of yet; nothing was opened."""


def new_upload_id() -> str:
    """An upload session id: one URL-safe path segment, unguessable."""

    return UPLOAD_ID_PREFIX + secrets.token_hex(16)


@dataclass(frozen=True)
class RepositoryRecord:
    """A repository's row: whose it is, and whether the registry has accepted a manifest for it yet."""

    repository: str
    source_id: str
    owner_org_id: str | None
    created_by: str
    committed: bool = True


@dataclass(frozen=True)
class UploadSession:
    """One upload in progress. ``upstream_location`` is the backing registry's session URL: server-side only."""

    id: str
    user_id: str
    repository: str
    source_id: str
    upstream_location: str | None
    received: int = 0
    lease: datetime | None = None
    """The lease this holder took, as stored: what proves a later write or release is the holder's."""


class PublishStore(ABC):
    """Repository ownership and upload sessions."""

    # -- repositories ----------------------------------------------------------

    @abstractmethod
    def get_repository(self, repository: str) -> RepositoryRecord | None:
        """The repository's row, pending or committed: either way it says whose the name is."""

        raise NotImplementedError

    @abstractmethod
    def reserve_repository(
        self,
        repository: str,
        *,
        source_id: str,
        owner_org_id: str | None,
        created_by: str,
        max_pending: int,
        attempt_id: str,
    ) -> RepositoryRecord:
        """Record, before a manifest is forwarded, that this organization is publishing here.

        Atomic, and durable before anything reaches the registry. With no
        row, a pending one is written for this organization. A pending row
        of this organization's gets ``attempt_id`` among the attempts it
        holds for. A committed row, and a pending row of **another**
        organization's, are returned untouched -- the caller checks whose
        the record is, and holds an attempt only when it is pending and its
        own. Idempotent per attempt.

        The hold is named, not counted, because a caller cut off during this
        call cannot tell whether it landed: :meth:`release_repository` with
        the same ``attempt_id`` takes back exactly this attempt's hold if it
        is there, and nothing otherwise -- never another attempt's.

        A **new** pending row is written only while the organization has
        fewer than ``max_pending`` of them; otherwise
        :class:`PendingLimitError`, and nothing changed. Counted and written
        under one lock per organization. Another attempt at a name the
        organization already holds pending is always allowed: the bound is
        on names, not on retries.
        """

        raise NotImplementedError

    @abstractmethod
    def commit_repository(
        self, repository: str, *, source_id: str, owner_org_id: str | None, created_by: str
    ) -> RepositoryRecord:
        """The registry accepted this organization's manifest: its pending row is ownership now.

        Only this organization's own pending row is changed (or a row
        written, if an operator released it meanwhile). Any other row is
        returned as it stands, and the caller checks it.
        """

        raise NotImplementedError

    @abstractmethod
    def release_repository(self, repository: str, *, owner_org_id: str | None, attempt_id: str) -> None:
        """This attempt forwarded nothing, or the registry **definitely refused** it: its hold is taken back.

        The pending row is deleted when it no longer holds for any attempt.
        An attempt whose outcome is unknown is never released, so the row
        stays, pending and owned. A committed row is not touched, and
        neither is an attempt that was never recorded. Idempotent.
        """

        raise NotImplementedError

    @abstractmethod
    def published_repositories(self, source_id: str) -> list[str]:
        """The repositories a sweep of this source should also enumerate, sorted.

        Committed ones, and pending ones older than
        :data:`PENDING_ENUMERATION_GRACE_SECONDS`: content the registry
        accepted but whose commit was lost is found there.
        """

        raise NotImplementedError

    @abstractmethod
    def pending_repositories(self, source_id: str) -> list[RepositoryRecord]:
        """This source's repositories that are still pending, for a sweep to settle."""

        raise NotImplementedError

    @abstractmethod
    def commit_found(self, source_id: str, repository: str, *, owner_org_id: str | None) -> bool:
        """A sweep found, in this repository, a digest this organization published: commit its pending row.

        Bound to the organization the caller checked the digest against.
        Returns whether a pending row was committed.
        """

        raise NotImplementedError

    # -- upload sessions ---------------------------------------------------------

    @abstractmethod
    def open_upload(
        self, *, upload_id: str, user_id: str, repository: str, source_id: str, lease_seconds: float
    ) -> UploadSession:
        """Reserve a session slot, **before** anything is opened at the registry, held by its opener's lease.

        Serialized per user. Live sessions past the cap are retired, oldest
        first: retired, not deleted -- they stay until
        :meth:`claim_stale_uploads` has had them cancelled at the registry --
        and never a slot that is still opening or a session being written
        to (both hold a lease). The rows bound,
        :data:`MAX_UPLOAD_ROWS_PER_USER`, still holds regardless. Raises
        :class:`UploadLimitError` when the user's rows (live and retired) are
        already at :data:`MAX_UPLOAD_ROWS_PER_USER`. The row has no registry
        location until :meth:`attach_upload`; until then it is unknown to
        :meth:`get_upload`, and neither the cap nor the cleanup touches it
        while its lease lasts.
        """

        raise NotImplementedError

    @abstractmethod
    def attach_upload(self, upload_id: str, *, lease: datetime, upstream_location: str) -> bool:
        """Record where the registry opened the session and give the opener's lease back.

        ``False`` if the slot is no longer this opener's (its lease lapsed
        and it was cleaned up).
        """

        raise NotImplementedError

    @abstractmethod
    def record_orphan(
        self, *, upload_id: str, user_id: str, repository: str, source_id: str, upstream_location: str
    ) -> None:
        """Remember a registry session that has no slot and could not be cancelled, for the cleanup to retry."""

        raise NotImplementedError

    @abstractmethod
    def get_upload(self, upload_id: str, *, user_id: str, repository: str) -> UploadSession | None:
        """The session, if it is this user's, for this repository, attached, unexpired and not dead."""

        raise NotImplementedError

    @abstractmethod
    def lease_upload(
        self, upload_id: str, *, user_id: str, repository: str, lease_seconds: float
    ) -> UploadSession | None:
        """Take the session's mutation lease. ``None`` if it is held (or the session is not there).

        The lease is what lets exactly one request, on any replica, forward
        bytes to -- or close, or cancel -- a registry session at a time. It
        is never taken over: a lease that runs out without having been given
        back means its holder forwarded something and never recorded what,
        so the session is dead from then on.
        """

        raise NotImplementedError

    @abstractmethod
    def advance_upload(
        self, upload_id: str, *, lease: datetime, expected_received: int, received: int, upstream_location: str
    ) -> bool:
        """Move a leased session forward after a chunk was forwarded, give the lease back, renew its idle time.

        Decided by the lease, not by the session's expiry: a write may
        legitimately run longer than :data:`UPLOAD_SESSION_TTL_SECONDS`, and
        while its lease is live nothing else can have touched the session --
        the cleanup claims only sessions whose lease is gone or lapsed, and
        the per-user cap never retires one that is being written to.
        ``False`` -- and nothing changed -- if the lease is no longer this
        holder's or has lapsed, or the session had moved.
        """

        raise NotImplementedError

    @abstractmethod
    def release_upload(self, upload_id: str, *, lease: datetime) -> None:
        """Give the lease back **unchanged**: only for a holder that knows the registry took nothing. Idempotent."""

        raise NotImplementedError

    @abstractmethod
    def retire_upload(self, upload_id: str) -> None:
        """End a session whose registry session may still exist: unusable from now, kept for cleanup.

        The row expires at once and its lease is dropped, so
        :meth:`claim_stale_uploads` picks it up and the cancellation is tried
        again. Idempotent.
        """

        raise NotImplementedError

    @abstractmethod
    def close_upload(self, upload_id: str) -> None:
        """Forget a session whose registry session is finished, cancelled, or was never opened. Idempotent."""

        raise NotImplementedError

    @abstractmethod
    def claim_stale_uploads(
        self, *, limit: int, lease_seconds: float, user_id: str | None = None
    ) -> list[UploadSession]:
        """Claim up to ``limit`` dead sessions (of one user, if given) for cancellation at the registry.

        Dead: past its expiry, or holding a lease that has run out. The
        caller cancels each one upstream and only then calls
        :meth:`close_upload`; one whose cancellation fails is left, and is
        claimable again when the claim runs out. Sessions older than
        :data:`UPLOAD_HARD_AGE_SECONDS` are dropped here outright.
        """

        raise NotImplementedError


class UnavailablePublishStore(PublishStore):
    """No backend: every call refuses, which the router answers as 503."""

    def _refuse(self) -> PublishStoreUnavailableError:
        return PublishStoreUnavailableError("publishing storage is not configured")

    def get_repository(self, repository):
        raise self._refuse()

    def reserve_repository(self, repository, *, source_id, owner_org_id, created_by, max_pending, attempt_id):
        raise self._refuse()

    def commit_repository(self, repository, *, source_id, owner_org_id, created_by):
        raise self._refuse()

    def release_repository(self, repository, *, owner_org_id, attempt_id):
        raise self._refuse()

    def published_repositories(self, source_id):
        raise self._refuse()

    def pending_repositories(self, source_id):
        raise self._refuse()

    def commit_found(self, source_id, repository, *, owner_org_id):
        raise self._refuse()

    def open_upload(self, **_kwargs):
        raise self._refuse()

    def attach_upload(self, upload_id, *, lease, upstream_location):
        raise self._refuse()

    def record_orphan(self, **_kwargs):
        raise self._refuse()

    def get_upload(self, upload_id, *, user_id, repository):
        raise self._refuse()

    def lease_upload(self, upload_id, *, user_id, repository, lease_seconds):
        raise self._refuse()

    def advance_upload(self, upload_id, *, lease, expected_received, received, upstream_location):
        raise self._refuse()

    def release_upload(self, upload_id, *, lease):
        raise self._refuse()

    def retire_upload(self, upload_id):
        raise self._refuse()

    def close_upload(self, upload_id):
        raise self._refuse()

    def claim_stale_uploads(self, *, limit, lease_seconds, user_id=None):
        raise self._refuse()


@dataclass
class _StoredRepository:
    record: RepositoryRecord
    since: datetime
    holders: set[str] = field(default_factory=set)


@dataclass
class _StoredUpload:
    session: UploadSession
    created: datetime
    expires: datetime
    leased_until: datetime | None = None

    def dead(self, now: datetime) -> bool:
        return self.expires <= now or (self.leased_until is not None and self.leased_until <= now)


@dataclass
class InMemoryPublishStore(PublishStore):
    """Process-local store for tests and single-process development; same semantics as Postgres."""

    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    _repositories: dict[str, _StoredRepository] = field(default_factory=dict)
    _uploads: dict[str, _StoredUpload] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- repositories ----------------------------------------------------------

    def get_repository(self, repository):
        with self._lock:
            stored = self._repositories.get(repository)
            return stored.record if stored is not None else None

    def reserve_repository(self, repository, *, source_id, owner_org_id, created_by, max_pending, attempt_id):
        with self._lock:
            stored = self._repositories.get(repository)
            if stored is None:
                pending = sum(
                    not other.record.committed and other.record.owner_org_id == owner_org_id
                    for other in self._repositories.values()
                )
                if pending >= max_pending:
                    raise PendingLimitError(f"{pending} repositories of this organization are already pending")
                record = RepositoryRecord(repository, source_id, owner_org_id, created_by, committed=False)
                stored = self._repositories[repository] = _StoredRepository(record, self.clock())
            if not stored.record.committed and stored.record.owner_org_id == owner_org_id:
                stored.holders.add(attempt_id)
            return stored.record

    def commit_repository(self, repository, *, source_id, owner_org_id, created_by):
        with self._lock:
            stored = self._repositories.get(repository)
            if stored is None:
                record = RepositoryRecord(repository, source_id, owner_org_id, created_by, committed=True)
                stored = self._repositories[repository] = _StoredRepository(record, self.clock())
            elif not stored.record.committed and stored.record.owner_org_id == owner_org_id:
                stored.record = replace(stored.record, committed=True)
            return stored.record

    def release_repository(self, repository, *, owner_org_id, attempt_id):
        with self._lock:
            stored = self._repositories.get(repository)
            if stored is None or stored.record.committed or stored.record.owner_org_id != owner_org_id:
                return
            stored.holders.discard(attempt_id)
            if not stored.holders:
                del self._repositories[repository]

    def published_repositories(self, source_id):
        settled = self.clock() - timedelta(seconds=PENDING_ENUMERATION_GRACE_SECONDS)
        with self._lock:
            return sorted(
                name
                for name, stored in self._repositories.items()
                if stored.record.source_id == source_id and (stored.record.committed or stored.since <= settled)
            )

    def pending_repositories(self, source_id):
        with self._lock:
            return sorted(
                (
                    stored.record
                    for stored in self._repositories.values()
                    if stored.record.source_id == source_id and not stored.record.committed
                ),
                key=lambda record: record.repository,
            )

    def commit_found(self, source_id, repository, *, owner_org_id):
        with self._lock:
            stored = self._repositories.get(repository)
            if (
                stored is None
                or stored.record.committed
                or stored.record.source_id != source_id
                or stored.record.owner_org_id != owner_org_id
            ):
                return False
            stored.record = replace(stored.record, committed=True)
            return True

    # -- upload sessions ---------------------------------------------------------

    def open_upload(self, *, upload_id, user_id, repository, source_id, lease_seconds):
        now = self.clock()
        lease = now + timedelta(seconds=lease_seconds)
        session = UploadSession(
            id=upload_id, user_id=user_id, repository=repository, source_id=source_id, upstream_location=None
        )
        with self._lock:
            mine = [stored for stored in self._uploads.values() if stored.session.user_id == user_id]
            if len(mine) >= MAX_UPLOAD_ROWS_PER_USER:
                raise UploadLimitError("too many upload sessions are still being cleaned up")
            live = [stored for stored in mine if stored.expires > now]
            # Never a slot that is still opening (its registry session is on
            # its way) or one being written to: both hold a lease.
            evictable = sorted(
                (
                    stored
                    for stored in live
                    if stored.session.upstream_location is not None and stored.leased_until is None
                ),
                key=lambda stored: (stored.created, stored.session.id),
            )
            for stored in evictable[: max(len(live) - (MAX_UPLOAD_SESSIONS_PER_USER - 1), 0)]:
                stored.expires = now
            self._uploads[upload_id] = _StoredUpload(
                session=session,
                created=now,
                expires=now + timedelta(seconds=UPLOAD_SESSION_TTL_SECONDS),
                leased_until=lease,
            )
        return replace(session, lease=lease)

    def attach_upload(self, upload_id, *, lease, upstream_location):
        with self._lock:
            stored = self._uploads.get(upload_id)
            if stored is None or stored.leased_until != lease:
                return False
            stored.session = replace(stored.session, upstream_location=upstream_location)
            stored.leased_until = None
            return True

    def record_orphan(self, *, upload_id, user_id, repository, source_id, upstream_location):
        now = self.clock()
        session = UploadSession(
            id=upload_id,
            user_id=user_id,
            repository=repository,
            source_id=source_id,
            upstream_location=upstream_location,
        )
        with self._lock:
            self._uploads[upload_id] = _StoredUpload(session=session, created=now, expires=now)

    def _live(self, upload_id, user_id, repository, now) -> _StoredUpload | None:
        stored = self._uploads.get(upload_id)
        if (
            stored is None
            or stored.dead(now)
            or stored.session.upstream_location is None
            or stored.session.user_id != user_id
            or stored.session.repository != repository
        ):
            return None
        return stored

    def get_upload(self, upload_id, *, user_id, repository):
        with self._lock:
            stored = self._live(upload_id, user_id, repository, self.clock())
            return stored.session if stored is not None else None

    def lease_upload(self, upload_id, *, user_id, repository, lease_seconds):
        now = self.clock()
        with self._lock:
            stored = self._live(upload_id, user_id, repository, now)
            if stored is None or stored.leased_until is not None:
                return None
            stored.leased_until = now + timedelta(seconds=lease_seconds)
            return replace(stored.session, lease=stored.leased_until)

    def advance_upload(self, upload_id, *, lease, expected_received, received, upstream_location):
        with self._lock:
            stored = self._uploads.get(upload_id)
            now = self.clock()
            if (
                stored is None
                or stored.leased_until != lease
                or lease <= now
                or stored.session.received != expected_received
            ):
                return False
            stored.session = replace(stored.session, received=received, upstream_location=upstream_location)
            stored.leased_until = None
            stored.expires = max(stored.expires, now + timedelta(seconds=UPLOAD_SESSION_TTL_SECONDS))
            return True

    def release_upload(self, upload_id, *, lease):
        with self._lock:
            stored = self._uploads.get(upload_id)
            if stored is not None and stored.leased_until == lease:
                stored.leased_until = None

    def retire_upload(self, upload_id):
        now = self.clock()
        with self._lock:
            stored = self._uploads.get(upload_id)
            if stored is not None:
                stored.expires = min(stored.expires, now)
                stored.leased_until = None

    def close_upload(self, upload_id):
        with self._lock:
            self._uploads.pop(upload_id, None)

    def claim_stale_uploads(self, *, limit, lease_seconds, user_id=None):
        now = self.clock()
        hard = now - timedelta(seconds=UPLOAD_HARD_AGE_SECONDS)
        claimed = []
        with self._lock:
            for key in [key for key, stored in self._uploads.items() if stored.created <= hard]:
                del self._uploads[key]
            stale = sorted(
                (
                    stored
                    for stored in self._uploads.values()
                    if (stored.leased_until is None and stored.expires <= now)
                    or (stored.leased_until is not None and stored.leased_until <= now)
                    if user_id is None or stored.session.user_id == user_id
                ),
                key=lambda stored: (stored.expires, stored.session.id),
            )
            for stored in stale[: max(limit, 0)]:
                stored.leased_until = now + timedelta(seconds=lease_seconds)
                stored.expires = min(stored.expires, now)
                claimed.append(replace(stored.session, lease=stored.leased_until))
        return claimed


def _repository_from_row(row) -> RepositoryRecord:
    return RepositoryRecord(
        repository=row["repository"],
        source_id=row["source_id"],
        owner_org_id=row["owner_org_id"],
        created_by=row["created_by"],
        committed=bool(row["committed"]),
    )


def _upload_from_row(row) -> UploadSession:
    return UploadSession(
        id=row["id"],
        user_id=row["user_id"],
        repository=row["repository"],
        source_id=row["source_id"],
        upstream_location=row["upstream_location"],
        received=int(row["received"]),
        lease=row["leased_until"],
    )


UPLOAD_OPEN_LOCK_CLASS = int.from_bytes(b"cup1", "big")
"""First key of the advisory lock serializing one user's upload opening and pruning (second: ``hashtext(user)``)."""

PENDING_LOCK_CLASS = int.from_bytes(b"cpr1", "big")
"""First key of the advisory lock serializing one organization's new pending names (second: ``hashtext(org)``)."""

_REPOSITORY_COLUMNS = "repository, source_id, owner_org_id, created_by, committed"
_UPLOAD_COLUMNS = "id, user_id, repository, source_id, upstream_location, received, leased_until"
_USABLE = "expires_at > now() AND upstream_location IS NOT NULL"
"""A session a client can still use, apart from its lease: unexpired and attached."""


class PostgresPublishStore(PublishStore):
    """Ownership and upload sessions over the shared pool."""

    def __init__(self, db):
        self._db = db

    # -- repositories ----------------------------------------------------------

    def _read_repository(self, conn, repository):
        row = conn.execute(
            f"SELECT {_REPOSITORY_COLUMNS} FROM collab_cog_repositories WHERE repository = %s",
            (repository,),
        ).fetchone()
        return _repository_from_row(row) if row else None

    def get_repository(self, repository):
        with bounded_connection(self._db) as conn:
            return self._read_repository(conn, repository)

    def reserve_repository(self, repository, *, source_id, owner_org_id, created_by, max_pending, attempt_id):
        with bounded_connection(self._db) as conn:
            # One organization's new names are counted and written one at a
            # time, so two requests cannot both see room for the last one.
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                (PENDING_LOCK_CLASS, owner_org_id or ""),
            )
            # Another attempt at a name this organization already holds
            # pending: always allowed, and only recorded. A committed row, and
            # a pending row of another organization's, match nothing here.
            joined = conn.execute(
                """
                UPDATE collab_cog_repositories
                SET holders = CASE WHEN %s = ANY(holders) THEN holders ELSE array_append(holders, %s) END
                WHERE repository = %s AND NOT committed AND owner_org_id IS NOT DISTINCT FROM %s
                RETURNING repository
                """,
                (attempt_id, attempt_id, repository, owner_org_id),
            ).fetchone()
            if joined is None:
                # A new name, if the organization has room for one more
                # pending; a row somebody else holds is left exactly as it is.
                conn.execute(
                    """
                    INSERT INTO collab_cog_repositories
                        (repository, source_id, owner_org_id, created_by, committed, holders)
                    SELECT %s, %s, %s, %s, false, ARRAY[%s]
                    WHERE (
                        SELECT count(*) FROM collab_cog_repositories
                        WHERE NOT committed AND owner_org_id IS NOT DISTINCT FROM %s
                    ) < %s
                    ON CONFLICT (repository) DO NOTHING
                    """,
                    (repository, source_id, owner_org_id, created_by, attempt_id, owner_org_id, max_pending),
                )
            # Read back in the same transaction: whoever holds the name,
            # this is the record that stands, and the caller checks it.
            record = self._read_repository(conn, repository)
        if record is None:
            raise PendingLimitError("this organization has no room for another pending repository")
        return record

    def commit_repository(self, repository, *, source_id, owner_org_id, created_by):
        with bounded_connection(self._db) as conn:
            conn.execute(
                """
                INSERT INTO collab_cog_repositories
                    (repository, source_id, owner_org_id, created_by, committed, holders)
                VALUES (%s, %s, %s, %s, true, '{}')
                ON CONFLICT (repository) DO UPDATE SET committed = true
                WHERE NOT collab_cog_repositories.committed
                  AND collab_cog_repositories.owner_org_id IS NOT DISTINCT FROM EXCLUDED.owner_org_id
                """,
                (repository, source_id, owner_org_id, created_by),
            )
            return self._read_repository(conn, repository)

    def release_repository(self, repository, *, owner_org_id, attempt_id):
        with bounded_connection(self._db) as conn:
            # This attempt's hold first, under the row's lock, and the row
            # deleted only once it holds for nobody: two releases at once
            # cannot both see "one attempt left", and a release of an attempt
            # that was never recorded takes nothing from anyone.
            conn.execute(
                """
                UPDATE collab_cog_repositories SET holders = array_remove(holders, %s)
                WHERE repository = %s AND NOT committed AND owner_org_id IS NOT DISTINCT FROM %s
                """,
                (attempt_id, repository, owner_org_id),
            )
            conn.execute(
                """
                DELETE FROM collab_cog_repositories
                WHERE repository = %s AND NOT committed AND owner_org_id IS NOT DISTINCT FROM %s
                  AND cardinality(holders) = 0
                """,
                (repository, owner_org_id),
            )

    def published_repositories(self, source_id):
        with bounded_connection(self._db) as conn:
            rows = conn.execute(
                """
                SELECT repository FROM collab_cog_repositories
                WHERE source_id = %s AND (committed OR created_at <= now() - make_interval(secs => %s))
                """,
                (source_id, PENDING_ENUMERATION_GRACE_SECONDS),
            ).fetchall()
        return sorted(row["repository"] for row in rows)

    def pending_repositories(self, source_id):
        with bounded_connection(self._db) as conn:
            rows = conn.execute(
                f"""
                SELECT {_REPOSITORY_COLUMNS} FROM collab_cog_repositories
                WHERE source_id = %s AND NOT committed ORDER BY repository
                """,
                (source_id,),
            ).fetchall()
        return [_repository_from_row(row) for row in rows]

    def commit_found(self, source_id, repository, *, owner_org_id):
        with bounded_connection(self._db) as conn:
            row = conn.execute(
                """
                UPDATE collab_cog_repositories SET committed = true
                WHERE source_id = %s AND repository = %s AND NOT committed
                  AND owner_org_id IS NOT DISTINCT FROM %s
                RETURNING repository
                """,
                (source_id, repository, owner_org_id),
            ).fetchone()
        return row is not None

    # -- upload sessions ---------------------------------------------------------

    def open_upload(self, *, upload_id, user_id, repository, source_id, lease_seconds):
        with bounded_connection(self._db) as conn:
            # One user's opening and retiring are serialized, so concurrent
            # opens cannot each count before the other commits and leave the
            # user over the cap. Released with the transaction.
            conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (UPLOAD_OPEN_LOCK_CLASS, user_id))
            held = conn.execute(
                "SELECT count(*) AS n FROM collab_cog_upload_sessions WHERE user_id = %s",
                (user_id,),
            ).fetchone()
            if int(held["n"]) >= MAX_UPLOAD_ROWS_PER_USER:
                raise UploadLimitError("too many upload sessions are still being cleaned up")
            # Retired, not deleted: the registry still holds a session for
            # each, and the row is what remembers where to cancel it. Never
            # a slot that is still opening (no location yet): its registry
            # session is on its way, and nobody else knows where it will be.
            # Nor one being written to: its writer records the outcome by
            # its lease, and an upload is never retired under it.
            conn.execute(
                """
                UPDATE collab_cog_upload_sessions SET expires_at = now()
                WHERE id IN (
                    SELECT id FROM collab_cog_upload_sessions
                    WHERE user_id = %s AND expires_at > now() AND upstream_location IS NOT NULL
                      AND leased_until IS NULL
                    ORDER BY created_at, id
                    LIMIT GREATEST((
                        SELECT count(*) FROM collab_cog_upload_sessions
                        WHERE user_id = %s AND expires_at > now()
                    ) - %s, 0)
                )
                """,
                (user_id, user_id, MAX_UPLOAD_SESSIONS_PER_USER - 1),
            )
            row = conn.execute(
                f"""
                INSERT INTO collab_cog_upload_sessions (id, user_id, repository, source_id, leased_until, expires_at)
                VALUES (%s, %s, %s, %s, clock_timestamp() + make_interval(secs => %s),
                        now() + make_interval(secs => %s))
                RETURNING {_UPLOAD_COLUMNS}
                """,
                (upload_id, user_id, repository, source_id, lease_seconds, UPLOAD_SESSION_TTL_SECONDS),
            ).fetchone()
        return _upload_from_row(row)

    def attach_upload(self, upload_id, *, lease, upstream_location):
        with bounded_connection(self._db) as conn:
            row = conn.execute(
                """
                UPDATE collab_cog_upload_sessions SET upstream_location = %s, leased_until = NULL
                WHERE id = %s AND leased_until = %s
                RETURNING id
                """,
                (upstream_location, upload_id, lease),
            ).fetchone()
        return row is not None

    def record_orphan(self, *, upload_id, user_id, repository, source_id, upstream_location):
        with bounded_connection(self._db) as conn:
            conn.execute(
                """
                INSERT INTO collab_cog_upload_sessions
                    (id, user_id, repository, source_id, upstream_location, expires_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (id) DO NOTHING
                """,
                (upload_id, user_id, repository, source_id, upstream_location),
            )

    def get_upload(self, upload_id, *, user_id, repository):
        with bounded_connection(self._db) as conn:
            row = conn.execute(
                f"""
                SELECT {_UPLOAD_COLUMNS} FROM collab_cog_upload_sessions
                WHERE id = %s AND user_id = %s AND repository = %s AND {_USABLE}
                  AND (leased_until IS NULL OR leased_until > now())
                """,
                (upload_id, user_id, repository),
            ).fetchone()
        return _upload_from_row(row) if row else None

    def lease_upload(self, upload_id, *, user_id, repository, lease_seconds):
        with bounded_connection(self._db) as conn:
            # Compare-and-set on the row: of two requests for one session, on
            # any two replicas, exactly one gets it. A lease that has run out
            # is not free: its holder never said what the registry took.
            row = conn.execute(
                f"""
                UPDATE collab_cog_upload_sessions
                SET leased_until = clock_timestamp() + make_interval(secs => %s)
                WHERE id = %s AND user_id = %s AND repository = %s AND {_USABLE}
                  AND leased_until IS NULL
                RETURNING {_UPLOAD_COLUMNS}
                """,
                (lease_seconds, upload_id, user_id, repository),
            ).fetchone()
        return _upload_from_row(row) if row else None

    def advance_upload(self, upload_id, *, lease, expected_received, received, upstream_location):
        with bounded_connection(self._db) as conn:
            row = conn.execute(
                """
                UPDATE collab_cog_upload_sessions
                SET received = %s, upstream_location = %s, leased_until = NULL,
                    expires_at = GREATEST(expires_at, now() + make_interval(secs => %s))
                WHERE id = %s AND leased_until = %s AND leased_until > clock_timestamp() AND received = %s
                RETURNING id
                """,
                (received, upstream_location, UPLOAD_SESSION_TTL_SECONDS, upload_id, lease, expected_received),
            ).fetchone()
        return row is not None

    def release_upload(self, upload_id, *, lease):
        with bounded_connection(self._db) as conn:
            conn.execute(
                "UPDATE collab_cog_upload_sessions SET leased_until = NULL WHERE id = %s AND leased_until = %s",
                (upload_id, lease),
            )

    def retire_upload(self, upload_id):
        with bounded_connection(self._db) as conn:
            conn.execute(
                """
                UPDATE collab_cog_upload_sessions SET expires_at = LEAST(expires_at, now()), leased_until = NULL
                WHERE id = %s
                """,
                (upload_id,),
            )

    def close_upload(self, upload_id):
        with bounded_connection(self._db) as conn:
            conn.execute("DELETE FROM collab_cog_upload_sessions WHERE id = %s", (upload_id,))

    def claim_stale_uploads(self, *, limit, lease_seconds, user_id=None):
        with bounded_connection(self._db) as conn:
            conn.execute(
                "DELETE FROM collab_cog_upload_sessions WHERE created_at <= now() - make_interval(secs => %s)",
                (UPLOAD_HARD_AGE_SECONDS,),
            )
            rows = conn.execute(
                f"""
                UPDATE collab_cog_upload_sessions
                SET leased_until = clock_timestamp() + make_interval(secs => %s),
                    expires_at = LEAST(expires_at, now())
                WHERE id IN (
                    SELECT id FROM collab_cog_upload_sessions
                    WHERE ((leased_until IS NULL AND expires_at <= now()) OR leased_until <= now())
                      AND (%s::text IS NULL OR user_id = %s)
                    ORDER BY expires_at, id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING {_UPLOAD_COLUMNS}
                """,
                (lease_seconds, user_id, user_id, max(limit, 0)),
            ).fetchall()
        return [_upload_from_row(row) for row in rows]
