"""The Cog catalog: where indexed artifacts live (issue #84).

One row per ``(source_id, repository, digest)`` in ``collab_cog_artifacts``,
created by migration version 11 of :mod:`..frames.collab_schema` -- never by
this module, which carries no DDL. Identity is the digest: ``cog_id`` and
``name`` are search keys read from the Cog's own declarations, and the
repository path is not identity (published repository names carry an id
suffix, and one Cog may be published to several repositories).

Three backends, following the house pattern of the other relational stores:

- :class:`PostgresCogCatalogStore` over the shared ``frames.postgres`` pool;
- :class:`InMemoryCogCatalogStore` for tests and single-process development;
- :class:`UnavailableCogCatalogStore` when no database is configured, which
  raises :class:`CogCatalogUnavailableError` (-> 503 at the API) rather than
  answering an empty catalog as if it were the truth.

**Content policy.** ``card`` is the published bundle's own declarations,
stored verbatim (parent-issue acceptance: the reader's output, structure
preserved) -- so whatever a publisher writes there is what the catalog holds.
The guarantee is narrower and absolute: *configured registry credentials*
never reach cards, ``read_errors``, or logs. The store additionally refuses
content ``jsonb`` cannot represent (:class:`CogCatalogDataError`), so one
pathological card can be recorded as failed instead of poisoning writes.

The write surface is exactly what the reconciliation indexer needs -- the
calls of :class:`SweepView` (``known``, ``upsert``, ``update_tags``,
``mark_removed``, and the housekeeping ``collect_unconfigured_blobs``) plus
``mark_removed_one`` for a webhook delete; the read surface is what the
catalog API (#85) builds on. Rows are never deleted: installs and runs may
reference a digest long after its publisher removed it, so removal is
``removed_at = now()`` and the row stays readable. What is deleted is the
pull path's blob descriptors: a removed version's at once, and those of a
source no longer configured after a retention period (issue #193).

**Sessions.** A sweep runs under :meth:`CogCatalogStore.sweep_lock`, which
yields a :class:`SweepView` when the lock was taken. On Postgres that view is
bound to the lock's own connection, so every sweep read and write rides the
lock session and a sweeper that dies mid-write loses the write and the lock
together (issue #128). The store's *own* methods never consult the lock:
they check out a pooled connection of their own every time, so a webhook's
targeted write or a direct call in a test lands concurrently with a sweep and
cannot be pulled onto -- or die with -- its session (issue #148).

Every method is synchronous psycopg over the shared pool, like every other
store here. The indexer is async and bridges with a worker thread.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .deadline import bounded_connection
from .registry import reference

STATUS_INDEXED = "indexed"
"""The reader produced a card. Errors on the card do not change this: a draft
or a Cog with a broken profile is still a Cog, listed with what it declared."""

STATUS_NON_COG = "non_cog"
"""The manifest carries no ``COG.md`` (and no Prog ``pixi.toml``). Recorded,
with a reason, so the digest is skipped on later sweeps instead of re-read."""

STATUS_FAILED = "failed"
"""Fetching or reading the bundle failed. ``read_errors`` says how; the next
sweep retries it."""

STATUSES = frozenset({STATUS_INDEXED, STATUS_NON_COG, STATUS_FAILED})

DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 1000

# Session-level advisory-lock key the indexer's sweep takes so replicas do not
# sweep concurrently. Same `int.from_bytes(<8 ASCII bytes>, "big")` derivation
# as COLLAB_SCHEMA_LOCK_KEY ("collab_1") and FRAMES_SERVER_SCHEMA_LOCK_KEY
# ("fsvrddl1"), so the constant is greppable and distinct from both.
COG_INDEX_LOCK_KEY = int.from_bytes(b"cogidx_1", "big")

SWEEP_STATEMENT_TIMEOUT_SECONDS = 20.0
"""Server-side bound on every statement the sweep runs (lock included).

The pool timeout bounds *checkout*; nothing else bounds execution. A
cancelled sweep's release is queued behind the statement it abandoned, so
this is what bounds how long a cancelled sweep can keep its lock while the
transport still works (a dead transport is the process's to end: the session
dies with it). Session-set on the lock connection (which runs in autocommit)
and reset before it returns to the pool; transaction-local on the store's
own sweep-path methods, so their connections go back unaltered.
"""


class CogCatalogUnavailableError(RuntimeError):
    """Raised when the catalog is needed but no backend is configured."""


class CogCatalogDataError(ValueError):
    """This one row's content cannot be stored (e.g. NUL in the card's JSON).

    Deliberately distinct from the store's availability/outage errors: the
    indexer records a row raising this as ``failed`` and continues the sweep,
    while an outage aborts the sweep. Both backends raise it for the same
    content so tests against the in-memory store see the production behavior.
    """


@dataclass(frozen=True)
class CogArtifact:
    """One row of the catalog: one artifact at one location in one source."""

    source_id: str
    host: str
    repository: str
    digest: str
    status: str
    tags: tuple[str, ...] = ()
    pushed_at: datetime | None = None
    indexed_at: datetime | None = None
    manifest_media_type: str | None = None
    card: dict[str, Any] | None = None
    cog_id: str | None = None
    name: str | None = None
    version: str | None = None
    kind: str | None = None
    publisher: str | None = None
    manifest_schema: str | None = None
    read_errors: tuple[str, ...] = ()
    removed_at: datetime | None = None
    published_by: str | None = None
    """The Hub user who published this artifact *through the Hub* (issue #180); ``None`` for an out-of-band push.

    Authenticated, unlike the card's self-declared ``publisher``. Never
    taken from the row a writer passes in: every write keeps one that is
    set, and fills an unset one only from a publication attempt the registry
    is **known to have accepted** (:meth:`CogCatalogStore.accept_publication`)
    -- the earliest, if there are several. With no such record the publisher
    is unknown and stays ``None``.
    """
    published_org: str | None = None

    @property
    def reference(self) -> str:
        """The pinned install reference ``<host>/<repository>@<digest>``."""

        return reference(self.host, self.repository, self.digest)

    @property
    def present(self) -> bool:
        return self.removed_at is None


logger = logging.getLogger("frames_server.cogs.catalog")

MAX_PULL_CANDIDATES = 4
"""Locations of one manifest (or one blob) a pull may be tried against, preferred first.

A work bound, never an authorization window: every lookup behind the Hub's
``/v2/`` surface is exact -- by digest, by stored tag, or by blob digest --
so this only caps how many *sources holding the same content* one request
may fall back across.
"""

DEFAULT_TAGS_PAGE = 1000
MAX_TAGS_PAGE = 1000
"""Tags in one ``tags/list`` page: what a request gets without ``n``, and the most ``n`` may ask for."""

DEFAULT_REMOVED_SOURCE_RETENTION = timedelta(days=7)
"""How long the blob descriptors of a source dropped from the configuration are kept (issue #193).

Counted from the last time the source was seen configured: stamped by every
sweep of an indexer that has it, and by every first recording of a manifest's
descriptors for it, on any replica. Long against any rollout, so a replica
still running the old configuration has long since gone (or has kept the
source alive by writing for it) before anything of the source is collected.
``cogs.index.removed_source_retention_hours`` sets it.
"""

MAX_COLLECTED_MANIFESTS_PER_SWEEP = 500
"""Manifests whose descriptors one sweep may collect for unconfigured sources; the rest wait for the next sweep.

Per manifest, never per descriptor row, so a manifest loses all of its
descriptors or none (what :attr:`PullableArtifact.blobs_recorded` relies on).
"""


@dataclass(frozen=True)
class PullableArtifact:
    """One artifact the Hub may serve a pull of: where it lives, and what names it.

    The slim view of a catalog row the ``/v2/`` surface needs -- no card.
    """

    source_id: str
    repository: str
    digest: str
    tags: tuple[str, ...] = ()
    blobs_recorded: bool = False
    """Whether this manifest's blob descriptors are already recorded (``record_manifest_blobs``)."""


@dataclass(frozen=True)
class BlobDescriptor:
    """One blob a manifest references, as its (verified) descriptor declared it."""

    digest: str
    size: int
    media_type: str = ""


@dataclass(frozen=True)
class PullableBlob:
    """Where a blob may be pulled from: a source holding a pullable manifest that references it."""

    source_id: str
    repository: str
    manifest_digest: str
    digest: str
    size: int


@dataclass(frozen=True)
class KnownArtifact:
    """What the indexer needs to know about a row before deciding to refetch it."""

    repository: str
    digest: str
    tags: tuple[str, ...]
    status: str
    removed: bool


@dataclass(frozen=True)
class CatalogFilter:
    """Filters for :meth:`CogCatalogStore.list_current`.

    ``source_id`` **scopes** the listing: the newest row per ``cog_id`` is
    chosen among that source's rows. Every other filter **tests the chosen
    row** -- a Cog is listed when its *current* card matches, never because
    an older version once did -- so without ``source_id`` a listed entry is
    always the card the Cog's own detail view serves (with it, the entry is
    the newest *in that source*, which may be older).

    ``provides``/``requires``/``accepts``/``produces`` are containment filters
    over the structured card (``card @> ...``), which is what the GIN index
    on ``card`` serves. ``requires`` names a capability
    (``card.requires[*].capability``); ``provides`` an entry of
    ``card.provides``; ``accepts``/``produces`` entries of ``card.io``.
    ``q`` is a case-insensitive substring of the ``name`` column or the
    card's ``description`` when that is a JSON string (a structured
    description never matches); ``%`` and ``_`` in it are literal. Case
    folding is guaranteed identical across backends for ASCII only.
    """

    kind: str | None = None
    publisher: str | None = None
    source_id: str | None = None
    provides: str | None = None
    requires: str | None = None
    accepts: str | None = None
    produces: str | None = None
    q: str | None = None

    def containment(self) -> dict[str, Any] | None:
        """The JSON document the card must contain, or ``None`` when no card filter is set."""

        document: dict[str, Any] = {}
        if self.provides is not None:
            document["provides"] = [self.provides]
        if self.requires is not None:
            document["requires"] = [{"capability": self.requires}]
        io: dict[str, Any] = {}
        if self.accepts is not None:
            io["accepts"] = [self.accepts]
        if self.produces is not None:
            io["produces"] = [self.produces]
        if io:
            document["io"] = io
        return document or None


class SweepView(ABC):
    """The catalog as one sweep sees it: the sweep-path calls and nothing else.

    What :meth:`CogCatalogStore.sweep_lock` hands the sweep that took the
    lock. On Postgres the view is bound to the lock's own connection, so a
    call on it rides the lock session (issue #128) -- a sweeper that dies
    mid-write loses the write and the lock together, and nothing but the
    holder of the view can ride that session (issue #148). The in-memory
    store has no sessions and is its own view.
    """

    @abstractmethod
    def clock(self) -> datetime:
        """The time by the clock that stamps ``indexed_at``: the database's, where there is one."""

        raise NotImplementedError

    @abstractmethod
    def known(self, source_id: str) -> list[KnownArtifact]:
        """Every row this source has ever produced, removed rows included."""

        raise NotImplementedError

    @abstractmethod
    def upsert(self, artifact: CogArtifact) -> None:
        """Insert the row or replace it whole; a replaced row is present again (``removed_at`` cleared)."""

        raise NotImplementedError

    @abstractmethod
    def update_tags(
        self,
        source_id: str,
        repository: str,
        digest: str,
        tags: Iterable[str],
        *,
        pushed_at: datetime | None = None,
    ) -> bool:
        """Change a row's tag set without touching its card, and mark it present again.

        Returns whether a row existed. ``pushed_at`` is updated only when given.
        """

        raise NotImplementedError

    @abstractmethod
    def mark_removed(
        self,
        source_id: str,
        present: Mapping[str, Iterable[str]],
        *,
        excluding: Iterable[str] = (),
        written_before: datetime | None = None,
    ) -> int:
        """Set ``removed_at = now()`` on this source's present rows whose digest is not in ``present``.

        ``present`` maps repository -> digests enumerated this sweep; rows in
        a repository listed in ``excluding`` (one whose enumeration failed
        this sweep) are left untouched, because what was not enumerated
        cannot be declared gone. Returns the number of rows newly marked.
        Never deletes.

        ``written_before`` -- a :meth:`clock` reading taken before the
        registry was enumerated -- limits removal to rows last written
        (``indexed_at``) before it, in the removal statement itself: a row a
        publish wrote, or brought back, after that reading is not declared
        gone by a listing that may predate it (issue #180).
        """

        raise NotImplementedError

    @abstractmethod
    def collect_unconfigured_blobs(
        self,
        configured: Iterable[str],
        *,
        retention: timedelta = DEFAULT_REMOVED_SOURCE_RETENTION,
        limit: int = MAX_COLLECTED_MANIFESTS_PER_SWEEP,
    ) -> int:
        """Delete the blob descriptors of sources that are no longer configured; at most ``limit`` manifests' worth.

        Housekeeping for issue #193, run once at the end of every sweep with
        the ids of the sources the indexer has configured. Three steps:

        1. stamp every ``configured`` source as seen configured now;
        2. give a source that has descriptors but no stamp one (now): its
           retention clock starts, and nothing of it goes this time;
        3. for sources that are not ``configured`` and were last seen
           configured more than ``retention`` ago, delete the descriptors of
           up to ``limit`` manifests, each manifest's whole set at once.

        A source in ``configured`` is never collected, whatever its stamp
        says. Another process that still has the source configured moves
        its stamp only by writing for it: its indexer, if it runs one, every
        sweep, and its first recording of any manifest's descriptors
        (:meth:`record_manifest_blobs`). Reads of manifests already recorded
        write nothing, so a process that only serves them does not keep the
        source alive; the retention is what covers a rollout. Each manifest
        is taken under its artifact's row, like :meth:`mark_removed`, so a
        recording in flight is never split; a row something else holds is
        skipped until the next sweep. Descriptors with no artifact row are
        left alone (only hand edits make them). Only ``collab_cog_manifest_blobs`` is
        deleted from: catalog rows and their history stay. A manifest whose
        descriptors were collected is recorded again by its next manifest
        read once its source is configured again.

        Returns the number of manifests whose descriptors were deleted.
        Raises :class:`ValueError` for an empty ``configured`` (that would
        declare every source gone) or a retention that is not positive.
        """

        raise NotImplementedError


def _checked_collection(configured: Iterable[str], retention: timedelta, limit: int) -> tuple[list[str], int]:
    """The arguments of :meth:`SweepView.collect_unconfigured_blobs`, checked: sorted unique ids and a sane limit."""

    ids = sorted(set(configured))
    if not ids:
        raise ValueError("collect_unconfigured_blobs needs the configured source ids; none would declare all gone")
    if retention <= timedelta(0):
        raise ValueError("the retention of an unconfigured source's descriptors must be positive")
    return ids, max(1, min(int(limit), MAX_COLLECTED_MANIFESTS_PER_SWEEP))


class CogCatalogStore(SweepView):
    """The catalog relation, as the indexer writes it and the API reads it.

    The :class:`SweepView` calls exist on the store itself too, each on a
    pooled connection of its own, for the lock-less targeted entry points (a
    webhook's ``reindex``) and for direct use; a sweep does not call them --
    it calls the view its lock returned.
    """

    # -- what the indexer needs, beyond the sweep view ----------------------

    @abstractmethod
    def mark_removed_one(self, source_id: str, repository: str, digest: str) -> bool:
        """Mark one row removed (a webhook delete). Returns whether a present row was marked."""

        raise NotImplementedError

    @abstractmethod
    def sweep_lock(self) -> AbstractContextManager[SweepView | None]:
        """Try to become the one sweeper; yields the sweep's view of the store, or ``None`` when locked out.

        Non-blocking. Held for the whole sweep and released on exit whether or
        not the sweep succeeded. Single flight is first a property of the
        deployment shape -- the chart runs one indexer replica, recreated,
        never rolled (issue #148) -- and this lock is the belt under it: it
        catches two indexer releases against one database. The Postgres
        store takes a **session-level** advisory lock on one pooled
        connection held outside any transaction (the sweep does network I/O
        for minutes; an open transaction that long would pin vacuum) and
        yields a view bound to that connection, so the sweep's reads and
        writes ride the lock session and a dead sweeper's lock and in-flight
        writes drop together when the server drops its connection (issue
        #128).
        """

        raise NotImplementedError

    # -- what the API needs ---------------------------------------------------

    @abstractmethod
    def get(self, digest: str, *, source_id: str | None = None, repository: str | None = None) -> CogArtifact | None:
        """One row by digest, removed rows included (an install may reference one).

        A digest can sit in several locations; without ``source_id``/``repository``
        the present row indexed most recently wins.
        """

        raise NotImplementedError

    @abstractmethod
    def locations(self, digest: str) -> list[CogArtifact]:
        """Every row carrying this digest, across sources and repositories."""

        raise NotImplementedError

    @abstractmethod
    def list_current(
        self,
        filters: CatalogFilter | None = None,
        *,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> list[CogArtifact]:
        """The newest present, indexed row per ``cog_id``, filtered, ordered by ``cog_id``.

        "Newest" is by ``pushed_at`` (unknown last), then ``indexed_at``.
        Non-Cog and failed rows, rows without a ``cog_id``, and removed rows
        never appear here. The newest row is chosen first (within
        ``filters.source_id`` when given) and the remaining filters test that
        row; see :class:`CatalogFilter`. ``offset`` skips that many entries of
        the ordered result, for paging.
        """

        raise NotImplementedError

    @abstractmethod
    def list_repositories(self) -> list[CogArtifact]:
        """The newest present, indexed row (with a ``cog_id``) per repository path, ordered by path.

        Repositories of the same path in different sources collapse to one
        entry. What the ``catalog.v1.json`` compatibility view is built from.
        """

        raise NotImplementedError

    @abstractmethod
    def list_versions(self, cog_id: str, *, include_removed: bool = False) -> list[CogArtifact]:
        """Every indexed row for one ``cog_id``, newest first."""

        raise NotImplementedError

    # -- what publishing through the Hub needs (issue #180) --------------------

    @abstractmethod
    def note_publication(
        self, attempt_id: str, source_id: str, repository: str, digest: str, *, user_id: str, org_id: str | None
    ) -> None:
        """Record one attempt to publish this digest, **before** its manifest is forwarded.

        One row per attempt, never shared: two users, or one user twice,
        publishing the same digest each have their own. An attempt
        attributes nothing until :meth:`accept_publication`. Attempts that
        never resolved are purged here, by age.
        """

        raise NotImplementedError

    @abstractmethod
    def accept_publication(self, attempt_id: str) -> None:
        """The registry accepted this attempt's manifest: from now on it may attribute the digest.

        If this cannot be recorded, nothing else may stand in for it: the
        digest's publisher stays unknown.
        """

        raise NotImplementedError

    @abstractmethod
    def accepted_digests(self, source_id: str, repository: str, org_id: str | None) -> set[str]:
        """The digests the registry is known to have accepted from this organization for this repository.

        Accepted attempts only. That an organization *tried* to publish a
        digest says nothing about how a digest found in the registry got
        there: the same bytes can be pushed by anyone.
        """

        raise NotImplementedError

    @abstractmethod
    def forget_publication(self, attempt_id: str) -> None:
        """The registry definitely refused this attempt's manifest: drop this attempt, and only this one.

        An accepted attempt is never dropped.
        """

        raise NotImplementedError

    @abstractmethod
    def record_published(self, artifact: CogArtifact, *, tag: str | None) -> None:
        """Write the row of a manifest the registry has just accepted through the Hub. One transaction.

        Like :meth:`upsert`, the row is replaced whole and is present again,
        and its publisher is decided as every write decides it (kept if set,
        else the earliest accepted attempt). The difference is the tags:
        **they are assigned, not replaced.** ``artifact.tags`` is ignored.
        With ``tag`` (the manifest was put under it), that tag ends up on
        this digest and on no other digest of the same source and
        repository: it is added here and taken from wherever else it was,
        atomically, serialized per repository. A put by digest
        (``tag=None``) adds nothing. The row's other tags stay as the
        catalog holds them; a row that was marked removed starts from none,
        so nothing stale comes back with it.

        Raises :class:`CogCatalogDataError` for a row the backend cannot
        represent, as :meth:`upsert` does.
        """

        raise NotImplementedError

    @abstractmethod
    def repository_known(self, repository: str) -> bool:
        """Whether the catalog has ever held a row for this repository path, in any source, in any state.

        What makes a repository "already exists, and was not published
        through the Hub" when it has no ownership record: removed, failed and
        non-Cog rows count, because the path is taken either way.
        """

        raise NotImplementedError

    # -- what pulls through the Hub need (issue #179) -------------------------
    #
    # "Pullable" is one rule, applied by every method below and nowhere else:
    # the row is present (not removed), indexed, and has a ``cog_id`` -- what
    # the read API lists -- and its source is one of ``source_ids`` (the
    # sources this process can reach). Every lookup is exact; none pages.

    @abstractmethod
    def find_pullable(
        self,
        repository: str,
        source_ids: Sequence[str],
        *,
        digest: str | None = None,
        tag: str | None = None,
    ) -> list[PullableArtifact]:
        """The pullable rows of ``repository`` with exactly this digest, or carrying exactly this tag.

        Exactly one of ``digest``/``tag`` is given. Newest first (as
        :meth:`list_versions` orders), so the first row carrying a tag is the
        one the tag resolves to; at most :data:`MAX_PULL_CANDIDATES` rows,
        which for a digest are the sources holding the same content. Each row
        says whether its blob descriptors are already recorded, so a manifest
        read writes them once and never again.
        """

        raise NotImplementedError

    @abstractmethod
    def has_pullable(self, repository: str, source_ids: Sequence[str]) -> bool:
        """Whether ``repository`` holds any pullable row: the difference between an unknown name and an unknown ref."""

        raise NotImplementedError

    @abstractmethod
    def list_pullable_tags(
        self, repository: str, source_ids: Sequence[str], *, after: str | None = None, limit: int = DEFAULT_TAGS_PAGE
    ) -> list[str]:
        """One page of the tags pullable rows of ``repository`` carry: sorted (code-point order), deduplicated.

        ``after`` is applied before ``limit``, so paging reaches every tag
        however many there are. ``limit`` is clamped to
        ``1..MAX_TAGS_PAGE + 1`` (the extra one is how a caller learns that
        another page exists).
        """

        raise NotImplementedError

    @abstractmethod
    def record_manifest_blobs(
        self, source_id: str, repository: str, manifest_digest: str, blobs: Iterable[BlobDescriptor]
    ) -> None:
        """Remember which blobs a manifest references. Idempotent; the content under a digest never changes.

        Written by whoever has just *verified* the manifest's bytes against
        its digest, with **all** of its descriptors in one call. The rows
        grant nothing by themselves: :meth:`find_blob` joins them to the
        pullable rule at read time.

        Recording and removal are serialized on the artifact's row, which is
        what lets :attr:`PullableArtifact.blobs_recorded` mean "all of them":
        nothing is recorded for an artifact that has no present row (a call
        for one is a no-op), and marking an artifact removed
        (:meth:`mark_removed`, :meth:`mark_removed_one`) deletes every row
        recorded for it. A manifest therefore has either all of its
        descriptors or none, and one that comes back is recorded again by
        its next manifest read.

        A call that records anything also stamps ``source_id`` as seen
        configured now (issue #193): only a process that has the source
        configured records for it, so a replica still serving a source keeps
        :meth:`collect_unconfigured_blobs` away from it, and re-adding a
        collected source restarts its clock with its first manifest read.
        """

        raise NotImplementedError

    @abstractmethod
    def find_blob(self, repository: str, blob_digest: str, source_ids: Sequence[str]) -> list[PullableBlob]:
        """Where ``blob_digest`` may be pulled from in ``repository``: via a manifest that is pullable *now*.

        Empty when no recorded manifest of that repository references it, or
        when every manifest that does has since been removed -- removal takes
        a version's blobs with it at once, unless another pullable manifest
        references them too. One location per source (that source's newest
        manifest naming the blob), at most :data:`MAX_PULL_CANDIDATES`
        sources, newest first: the candidates exist to fall back across
        sources, so several manifests in one source must not crowd another
        source out.
        """

        raise NotImplementedError


def new_attempt_id() -> str:
    """What identifies one attempt to publish a manifest through the Hub."""

    return "pub-" + secrets.token_hex(16)


UNACCEPTED_ATTEMPT_MAX_AGE_SECONDS = 24 * 3600
"""How long an attempt that never resolved is kept. It attributes nothing, and settles nothing, either way."""


def _keep_publication(
    new: CogArtifact, old: CogArtifact | None, accepted: tuple[str, str | None] | None
) -> CogArtifact:
    """A write replaces a row whole, except for who published it through the Hub.

    A publisher already on the row stays; one that is not there yet comes
    from the earliest attempt the registry is known to have accepted, if
    there is one. Never from the row passed in.
    """

    if old is not None and old.published_by is not None:
        return replace(new, published_by=old.published_by, published_org=old.published_org)
    user_id, org_id = accepted or (None, None)
    return replace(new, published_by=user_id, published_org=org_id)


def _checked(artifact: CogArtifact) -> None:
    if artifact.status not in STATUSES:
        raise ValueError(f"unknown catalog status {artifact.status!r}")
    require_aware(artifact.pushed_at, "pushed_at")
    require_aware(artifact.indexed_at, "indexed_at")


def _one_of(digest: str | None, tag: str | None) -> None:
    if (digest is None) == (tag is None):
        raise ValueError("give exactly one of digest and tag")


def _tags_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_TAGS_PAGE + 1))


def _bounded_limit(limit: int) -> int:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return min(limit, MAX_LIST_LIMIT)


def _checked_offset(offset: int) -> int:
    if offset < 0:
        raise ValueError("offset must not be negative")
    return offset


def like_pattern(text: str) -> str:
    """``text`` as an ``ILIKE`` substring pattern with its wildcards escaped (backslash escape)."""

    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


LIKE_ESCAPE = "\\"
"""The escape character :func:`like_pattern` uses, bound as a parameter (``ESCAPE %s``) so the
statement does not depend on the server's ``standard_conforming_strings`` setting."""


def matches_query(name: str | None, card: Mapping[str, Any] | None, q: str) -> bool:
    """The in-memory ``q`` test: case-insensitive substring of ``name`` or a *string* card ``description``.

    A description that is not a JSON string never matches, in either backend
    (the SQL checks ``jsonb_typeof``): rendering structured values as text is
    where the two backends would disagree. Case-folding is ``str.lower`` here
    and ``ILIKE`` there -- identical for ASCII, locale-dependent beyond it.
    """

    description = (card or {}).get("description")
    needle = q.lower()
    return any(needle in text.lower() for text in (name, description) if isinstance(text, str))


def _sort_key(artifact: CogArtifact) -> tuple:
    # pushed_at DESC NULLS LAST, indexed_at DESC, then a stable tiebreak.
    floor = datetime.min.replace(tzinfo=UTC)
    return (
        artifact.pushed_at is None,
        -(artifact.pushed_at or floor).timestamp(),
        -(artifact.indexed_at or floor).timestamp(),
        artifact.source_id,
        artifact.repository,
        artifact.digest,
    )


def _get_key(artifact: CogArtifact) -> tuple:
    # get()'s documented order: present rows first, then the most recently
    # INDEXED location -- pushed_at deliberately plays no part here, because
    # every location of one digest shares the artifact's push time while
    # indexed_at says which row this catalog wrote about it last.
    floor = datetime.min.replace(tzinfo=UTC)
    return (
        artifact.removed_at is not None,
        -(artifact.indexed_at or floor).timestamp(),
        artifact.source_id,
        artifact.repository,
    )


def require_aware(value: datetime | None, what: str) -> datetime | None:
    """Refuse a naive datetime at the store boundary.

    ``timestamptz`` interprets a naive value in the session's timezone and the
    in-memory store would compare it against aware ones (a ``TypeError`` at
    sort time, far from the caller) -- both are the wrong place to discover
    the mistake. Adapters already normalize to aware UTC; this guards direct
    store and ``reindex`` callers.
    """

    if value is not None and value.utcoffset() is None:
        # utcoffset() covers both shapes of naivety: tzinfo absent, and a
        # tzinfo present whose utcoffset() returns None (datetime.tzinfo
        # allows that, and such a value is naive in every way that matters).
        raise ValueError(f"{what} must be timezone-aware; a naive datetime would be reinterpreted per session")
    return value


def contains_nul(value: Any) -> bool:
    """Whether any string in this JSON tree contains an actual NUL character.

    Walks the PARSED values (keys included). Never implemented by searching
    the serialized text: ``json.dumps`` escapes a literal backslash, so the
    substring ``\\u0000`` appears both for a real NUL and for the harmless
    six characters backslash-u-0-0-0-0 in a document about NUL -- and only the
    first is something ``jsonb`` refuses.
    """

    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(contains_nul(key) or contains_nul(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_nul(item) for item in value)
    return False


def _scalar_eq(a: Any, b: Any) -> bool:
    # jsonb: true/false and numbers are different types (1 does not contain
    # true), while Python's `1 == True`. Numbers compare numerically across
    # int/float, matching jsonb's numeric comparison.
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a == b


def json_contains(document: Any, needle: Any, *, _top: bool = True) -> bool:
    """Postgres ``jsonb @>`` containment, mirrored exactly for the in-memory store.

    Objects: every key of ``needle`` is in ``document`` with a contained value.
    Arrays: every element of ``needle`` is contained by *some* element of
    ``document``; an array needle never matches a scalar document. Scalars:
    equality, with booleans distinct from numbers. The one asymmetry jsonb
    grants -- an array contains a bare scalar -- applies at the **top level
    only**, exactly as documented for ``@>``.

    Pinned against a live server by a shared case table in
    ``test_cog_catalog.py``, so a divergence fails a test rather than making
    a filter answer differently in dev and production.
    """

    if isinstance(needle, dict):
        return isinstance(document, dict) and all(
            k in document and json_contains(document[k], v, _top=False) for k, v in needle.items()
        )
    if isinstance(needle, list):
        if not isinstance(document, list):
            return False
        return all(any(json_contains(item, wanted, _top=False) for item in document) for wanted in needle)
    if isinstance(document, list):
        return _top and any(_scalar_eq(item, needle) for item in document if not isinstance(item, (dict, list)))
    if isinstance(document, dict):
        return False
    return _scalar_eq(document, needle)


class UnavailableCogCatalogStore(CogCatalogStore):
    """Store used when no shared frames Postgres is configured. Every call raises."""

    def _refuse(self) -> CogCatalogUnavailableError:
        return CogCatalogUnavailableError("Cog catalog storage is not configured")

    def known(self, source_id: str) -> list[KnownArtifact]:
        raise self._refuse()

    def upsert(self, artifact: CogArtifact) -> None:
        raise self._refuse()

    def update_tags(self, source_id, repository, digest, tags, *, pushed_at=None) -> bool:
        raise self._refuse()

    def mark_removed(self, source_id, present, *, excluding=(), written_before=None) -> int:
        raise self._refuse()

    def collect_unconfigured_blobs(
        self, configured, *, retention=DEFAULT_REMOVED_SOURCE_RETENTION, limit=MAX_COLLECTED_MANIFESTS_PER_SWEEP
    ) -> int:
        raise self._refuse()

    def clock(self) -> datetime:
        raise self._refuse()

    def mark_removed_one(self, source_id, repository, digest) -> bool:
        raise self._refuse()

    def sweep_lock(self) -> AbstractContextManager[SweepView | None]:
        raise self._refuse()

    def get(self, digest, *, source_id=None, repository=None) -> CogArtifact | None:
        raise self._refuse()

    def locations(self, digest) -> list[CogArtifact]:
        raise self._refuse()

    def list_current(self, filters=None, *, limit=DEFAULT_LIST_LIMIT, offset=0) -> list[CogArtifact]:
        raise self._refuse()

    def list_repositories(self) -> list[CogArtifact]:
        raise self._refuse()

    def list_versions(self, cog_id, *, include_removed=False) -> list[CogArtifact]:
        raise self._refuse()

    def note_publication(self, attempt_id, source_id, repository, digest, *, user_id, org_id) -> None:
        raise self._refuse()

    def accept_publication(self, attempt_id) -> None:
        raise self._refuse()

    def accepted_digests(self, source_id, repository, org_id) -> set[str]:
        raise self._refuse()

    def forget_publication(self, attempt_id) -> None:
        raise self._refuse()

    def record_published(self, artifact, *, tag) -> None:
        raise self._refuse()

    def repository_known(self, repository) -> bool:
        raise self._refuse()

    def find_pullable(self, repository, source_ids, *, digest=None, tag=None) -> list[PullableArtifact]:
        raise self._refuse()

    def has_pullable(self, repository, source_ids) -> bool:
        raise self._refuse()

    def list_pullable_tags(self, repository, source_ids, *, after=None, limit=0) -> list[str]:
        raise self._refuse()

    def record_manifest_blobs(self, source_id, repository, manifest_digest, blobs) -> None:
        raise self._refuse()

    def find_blob(self, repository, blob_digest, source_ids) -> list[PullableBlob]:
        raise self._refuse()


@dataclass
class InMemoryCogCatalogStore(CogCatalogStore):
    """Process-local catalog for tests and single-process development.

    Same semantics as the Postgres store, including containment filtering
    (:func:`json_contains`) and a process-local, non-blocking sweep lock
    whose view is the store itself (no sessions to bind to).
    """

    _rows: dict[tuple[str, str, str], CogArtifact] = field(default_factory=dict)
    _blobs: dict[tuple[str, str, str], dict[str, BlobDescriptor]] = field(default_factory=dict)
    _activity: dict[str, datetime] = field(default_factory=dict)
    """``collab_cog_source_activity``: when each source was last seen configured."""
    _attempts: dict[str, dict[str, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _sweep: threading.Lock = field(default_factory=threading.Lock)

    def _key(self, artifact: CogArtifact) -> tuple[str, str, str]:
        return (artifact.source_id, artifact.repository, artifact.digest)

    def known(self, source_id: str) -> list[KnownArtifact]:
        with self._lock:
            return [
                KnownArtifact(
                    repository=row.repository,
                    digest=row.digest,
                    tags=row.tags,
                    status=row.status,
                    removed=row.removed_at is not None,
                )
                for row in self._rows.values()
                if row.source_id == source_id
            ]

    def _storable(self, artifact: CogArtifact) -> None:
        _checked(artifact)
        if artifact.card is not None and contains_nul(artifact.card):
            # Parity with Postgres, where jsonb refuses NUL: a card this store
            # silently accepted would be a test passing on content production
            # rejects. contains_nul walks the parsed values, so a literal
            # backslash-u0000 in documentation text is (correctly) accepted.
            raise CogCatalogDataError("card contains NUL (\\u0000), which jsonb cannot store")

    def upsert(self, artifact: CogArtifact) -> None:
        self._storable(artifact)
        stored = replace(
            artifact,
            tags=tuple(sorted(set(artifact.tags))),
            read_errors=tuple(artifact.read_errors),
            indexed_at=artifact.indexed_at or datetime.now(UTC),
            removed_at=None,
        )
        with self._lock:
            # Parity with Postgres: a publisher on the row stays; an unset one comes from an accepted attempt.
            key = self._key(stored)
            self._rows[key] = _keep_publication(stored, self._rows.get(key), self._accepted(key))

    def _accepted(self, key: tuple[str, str, str]) -> tuple[str, str | None] | None:
        accepted = sorted(
            (attempt["accepted_at"], attempt_id, attempt)
            for attempt_id, attempt in self._attempts.items()
            if attempt["key"] == key and attempt["accepted_at"] is not None
        )
        return (accepted[0][2]["user_id"], accepted[0][2]["org_id"]) if accepted else None

    def note_publication(self, attempt_id, source_id, repository, digest, *, user_id, org_id) -> None:
        now = datetime.now(UTC)
        stale = now - timedelta(seconds=UNACCEPTED_ATTEMPT_MAX_AGE_SECONDS)
        with self._lock:
            for old in [k for k, a in self._attempts.items() if a["accepted_at"] is None and a["created_at"] < stale]:
                del self._attempts[old]
            self._attempts[attempt_id] = {
                "key": (source_id, repository, digest),
                "user_id": user_id,
                "org_id": org_id,
                "created_at": now,
                "accepted_at": None,
            }

    def accept_publication(self, attempt_id) -> None:
        with self._lock:
            attempt = self._attempts.get(attempt_id)
            if attempt is not None and attempt["accepted_at"] is None:
                attempt["accepted_at"] = datetime.now(UTC)

    def accepted_digests(self, source_id, repository, org_id) -> set[str]:
        with self._lock:
            return {
                attempt["key"][2]
                for attempt in self._attempts.values()
                if attempt["key"][:2] == (source_id, repository)
                and attempt["org_id"] == org_id
                and attempt["accepted_at"] is not None
            }

    def forget_publication(self, attempt_id) -> None:
        with self._lock:
            attempt = self._attempts.get(attempt_id)
            if attempt is not None and attempt["accepted_at"] is None:
                del self._attempts[attempt_id]

    def record_published(self, artifact, *, tag) -> None:
        self._storable(artifact)
        key = self._key(artifact)
        with self._lock:
            old = self._rows.get(key)
            kept = set(old.tags) if old is not None and old.removed_at is None else set()
            if tag is not None:
                kept.add(tag)
                for other_key, other in list(self._rows.items()):
                    if other_key != key and other_key[:2] == key[:2] and tag in other.tags:
                        self._rows[other_key] = replace(other, tags=tuple(t for t in other.tags if t != tag))
            stored = replace(
                artifact,
                tags=tuple(sorted(kept)),
                read_errors=tuple(artifact.read_errors),
                indexed_at=datetime.now(UTC),
                removed_at=None,
            )
            self._rows[key] = _keep_publication(stored, old, self._accepted(key))

    def repository_known(self, repository) -> bool:
        with self._lock:
            return any(row.repository == repository for row in self._rows.values())

    def update_tags(self, source_id, repository, digest, tags, *, pushed_at=None) -> bool:
        require_aware(pushed_at, "pushed_at")
        with self._lock:
            row = self._rows.get((source_id, repository, digest))
            if row is None:
                return False
            self._rows[(source_id, repository, digest)] = replace(
                row,
                tags=tuple(sorted(set(tags))),
                pushed_at=pushed_at if pushed_at is not None else row.pushed_at,
                removed_at=None,
            )
            return True

    def mark_removed(self, source_id, present, *, excluding=(), written_before=None) -> int:
        wanted = {repo: set(digests) for repo, digests in present.items()}
        shielded = set(excluding)
        now = datetime.now(UTC)
        marked = 0
        with self._lock:
            for key, row in list(self._rows.items()):
                if row.source_id != source_id or row.removed_at is not None:
                    continue
                if row.repository in shielded or row.digest in wanted.get(row.repository, ()):
                    continue
                if written_before is not None and row.indexed_at is not None and row.indexed_at >= written_before:
                    continue
                self._rows[key] = replace(row, removed_at=now)
                self._blobs.pop(key, None)
                marked += 1
        return marked

    def mark_removed_one(self, source_id, repository, digest) -> bool:
        with self._lock:
            row = self._rows.get((source_id, repository, digest))
            if row is None or row.removed_at is not None:
                return False
            self._rows[(source_id, repository, digest)] = replace(row, removed_at=datetime.now(UTC))
            self._blobs.pop((source_id, repository, digest), None)
            return True

    def clock(self) -> datetime:
        return datetime.now(UTC)

    @contextmanager
    def sweep_lock(self):
        acquired = self._sweep.acquire(blocking=False)
        try:
            yield self if acquired else None
        finally:
            if acquired:
                self._sweep.release()

    def get(self, digest, *, source_id=None, repository=None) -> CogArtifact | None:
        candidates = [
            row
            for row in self.locations(digest)
            if (source_id is None or row.source_id == source_id)
            and (repository is None or row.repository == repository)
        ]
        if not candidates:
            return None
        candidates.sort(key=_get_key)
        return candidates[0]

    def locations(self, digest) -> list[CogArtifact]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.digest == digest]
        rows.sort(key=lambda row: (row.source_id, row.repository))
        return rows

    def _present_cogs(self) -> list[CogArtifact]:
        with self._lock:
            return [
                row
                for row in self._rows.values()
                if row.removed_at is None and row.status == STATUS_INDEXED and row.cog_id is not None
            ]

    def list_current(self, filters=None, *, limit=DEFAULT_LIST_LIMIT, offset=0) -> list[CogArtifact]:
        limit = _bounded_limit(limit)
        offset = _checked_offset(offset)
        filters = filters or CatalogFilter()
        needle = filters.containment()
        rows = [row for row in self._present_cogs() if filters.source_id is None or row.source_id == filters.source_id]
        rows.sort(key=_sort_key)
        newest: dict[str, CogArtifact] = {}
        for row in rows:
            newest.setdefault(row.cog_id, row)  # type: ignore[arg-type]
        matching = [
            row
            for row in newest.values()
            if (filters.kind is None or row.kind == filters.kind)
            and (filters.publisher is None or row.publisher == filters.publisher)
            and (needle is None or json_contains(row.card, needle))
            and (filters.q is None or matches_query(row.name, row.card, filters.q))
        ]
        matching.sort(key=lambda row: row.cog_id or "")
        return matching[offset : offset + limit]

    def list_repositories(self) -> list[CogArtifact]:
        rows = self._present_cogs()
        rows.sort(key=lambda row: (row.repository, *_sort_key(row)))
        newest: dict[str, CogArtifact] = {}
        for row in rows:
            newest.setdefault(row.repository, row)
        return [newest[repository] for repository in sorted(newest)]

    def list_versions(self, cog_id, *, include_removed=False) -> list[CogArtifact]:
        with self._lock:
            rows = [
                row
                for row in self._rows.values()
                if row.cog_id == cog_id and row.status == STATUS_INDEXED and (include_removed or row.removed_at is None)
            ]
        rows.sort(key=_sort_key)
        return rows

    def _pullable_rows(self, repository, source_ids) -> list[CogArtifact]:
        allowed = set(source_ids)
        rows = [row for row in self._present_cogs() if row.repository == repository and row.source_id in allowed]
        rows.sort(key=_sort_key)
        return rows

    def find_pullable(self, repository, source_ids, *, digest=None, tag=None) -> list[PullableArtifact]:
        _one_of(digest, tag)
        rows = [
            row
            for row in self._pullable_rows(repository, source_ids)
            if (row.digest == digest if digest is not None else tag in row.tags)
        ]
        with self._lock:
            recorded = set(self._blobs)
        return [
            PullableArtifact(
                source_id=row.source_id,
                repository=row.repository,
                digest=row.digest,
                tags=row.tags,
                blobs_recorded=(row.source_id, row.repository, row.digest) in recorded,
            )
            for row in rows[:MAX_PULL_CANDIDATES]
        ]

    def has_pullable(self, repository, source_ids) -> bool:
        return bool(self._pullable_rows(repository, source_ids))

    def list_pullable_tags(self, repository, source_ids, *, after=None, limit=DEFAULT_TAGS_PAGE) -> list[str]:
        tags = {tag for row in self._pullable_rows(repository, source_ids) for tag in row.tags}
        return sorted(tag for tag in tags if after is None or tag > after)[: _tags_limit(limit)]

    def record_manifest_blobs(self, source_id, repository, manifest_digest, blobs) -> None:
        descriptors = list(blobs)
        key = (source_id, repository, manifest_digest)
        with self._lock:
            row = self._rows.get(key)
            if not descriptors or row is None or row.removed_at is not None:
                return
            recorded = self._blobs.setdefault(key, {})
            wrote = False
            for blob in descriptors:
                if blob.digest not in recorded:
                    recorded[blob.digest] = blob
                    wrote = True
            if wrote:
                self._stamp(source_id, datetime.now(UTC))

    def _stamp(self, source_id: str, now: datetime) -> None:
        """Seen configured at ``now``; never moves a stamp backwards. Caller holds ``_lock``."""

        seen = self._activity.get(source_id)
        self._activity[source_id] = now if seen is None else max(seen, now)

    def collect_unconfigured_blobs(
        self, configured, *, retention=DEFAULT_REMOVED_SOURCE_RETENTION, limit=MAX_COLLECTED_MANIFESTS_PER_SWEEP
    ) -> int:
        ids, limit = _checked_collection(configured, retention, limit)
        now = datetime.now(UTC)
        with self._lock:
            for source_id in ids:
                self._stamp(source_id, now)
            for source_id in {key[0] for key in self._blobs} - set(ids):
                self._activity.setdefault(source_id, now)
            collectable = {
                source_id
                for source_id, seen in self._activity.items()
                if source_id not in ids and seen < now - retention
            }
            # No row locks to skip here: the in-memory store is one lock. Like Postgres,
            # descriptors with no artifact row are left alone.
            batch = sorted(key for key in self._blobs if key[0] in collectable and key in self._rows)[:limit]
            for key in batch:
                del self._blobs[key]
        return len(batch)

    def find_blob(self, repository, blob_digest, source_ids) -> list[PullableBlob]:
        found: list[PullableBlob] = []
        seen: set[str] = set()
        for row in self._pullable_rows(repository, source_ids):
            with self._lock:
                blob = self._blobs.get((row.source_id, repository, row.digest), {}).get(blob_digest)
            # One location per source (its newest manifest naming the blob):
            # the candidates are for falling back across sources.
            if blob is not None and row.source_id not in seen:
                seen.add(row.source_id)
                found.append(
                    PullableBlob(
                        source_id=row.source_id,
                        repository=repository,
                        manifest_digest=row.digest,
                        digest=blob_digest,
                        size=blob.size,
                    )
                )
        return found[:MAX_PULL_CANDIDATES]


_COLUMNS = (
    "source_id, host, repository, digest, tags, pushed_at, indexed_at, manifest_media_type, status, card, "
    "cog_id, name, version, kind, publisher, manifest_schema, read_errors, removed_at, "
    "published_by, published_org"
)


_PULLABLE = f"removed_at IS NULL AND status = '{STATUS_INDEXED}' AND cog_id IS NOT NULL"
"""The one rule for what a pull through the Hub may reach; see :class:`CogCatalogStore`."""
_PULLABLE_A = f"a.removed_at IS NULL AND a.status = '{STATUS_INDEXED}' AND a.cog_id IS NOT NULL"


PUBLISH_LOCK_CLASS = int.from_bytes(b"cpb1", "big")
"""First key of the advisory lock serializing one repository's publishes (second: ``hashtext(source/repository)``)."""

_WRITTEN_COLUMNS = (
    "source_id, host, repository, digest, tags, pushed_at, indexed_at, manifest_media_type, "
    "status, card, cog_id, name, version, kind, publisher, manifest_schema, read_errors"
)
_WRITTEN_VALUES = "%s, %s, %s, %s, %s, %s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s"
_REPLACED_COLUMNS = """host = EXCLUDED.host,
                    pushed_at = EXCLUDED.pushed_at,
                    indexed_at = now(),
                    manifest_media_type = EXCLUDED.manifest_media_type,
                    status = EXCLUDED.status,
                    card = EXCLUDED.card,
                    cog_id = EXCLUDED.cog_id,
                    name = EXCLUDED.name,
                    version = EXCLUDED.version,
                    kind = EXCLUDED.kind,
                    publisher = EXCLUDED.publisher,
                    manifest_schema = EXCLUDED.manifest_schema,
                    read_errors = EXCLUDED.read_errors,
                    published_org = CASE WHEN collab_cog_artifacts.published_by IS NULL
                                         THEN EXCLUDED.published_org
                                         ELSE collab_cog_artifacts.published_org END,
                    published_by = COALESCE(collab_cog_artifacts.published_by, EXCLUDED.published_by)"""
"""What every write of a row replaces, and the one thing it keeps: a publisher that is already set."""

_ACCEPTED_ATTEMPT = """FROM collab_cog_publication_attempts
                         WHERE source_id = %s AND repository = %s AND digest = %s AND accepted_at IS NOT NULL
                         ORDER BY accepted_at, attempt_id LIMIT 1"""
_PUBLISHER = f"""(SELECT published_by {_ACCEPTED_ATTEMPT}),
                        (SELECT published_org {_ACCEPTED_ATTEMPT})"""
"""A new row's publisher: the earliest attempt the registry is known to have accepted, else nobody.

Two scalar subqueries that only read. Neither write of a row modifies an
attempt, so the two paths lock the same thing in the same order.
"""


def _key(artifact: CogArtifact) -> tuple[str, str, str]:
    return (artifact.source_id, artifact.repository, artifact.digest)


def _written_values(artifact: CogArtifact, tags: list[str]) -> tuple:
    from psycopg.types.json import Jsonb

    return (
        artifact.source_id,
        artifact.host,
        artifact.repository,
        artifact.digest,
        tags,
        artifact.pushed_at,
        artifact.manifest_media_type,
        artifact.status,
        Jsonb(artifact.card) if artifact.card is not None else None,
        artifact.cog_id,
        artifact.name,
        artifact.version,
        artifact.kind,
        artifact.publisher,
        artifact.manifest_schema,
        Jsonb(list(artifact.read_errors)),
    )


def _row_to_artifact(row: Mapping[str, Any]) -> CogArtifact:
    card = row["card"]
    errors = row["read_errors"]
    return CogArtifact(
        source_id=row["source_id"],
        host=row["host"],
        repository=row["repository"],
        digest=row["digest"],
        status=row["status"],
        tags=tuple(row["tags"] or ()),
        pushed_at=row["pushed_at"],
        indexed_at=row["indexed_at"],
        manifest_media_type=row["manifest_media_type"],
        card=card if isinstance(card, dict) else None,
        cog_id=row["cog_id"],
        name=row["name"],
        version=row["version"],
        kind=row["kind"],
        publisher=row["publisher"],
        manifest_schema=row["manifest_schema"],
        read_errors=tuple(errors) if isinstance(errors, list) else (),
        removed_at=row["removed_at"],
        # .get: a row mapping built before these columns existed (a test
        # double, a query that selects a subset) has no publication.
        published_by=row.get("published_by"),
        published_org=row.get("published_org"),
    )


class PostgresCogCatalogStore(CogCatalogStore):
    """The catalog over ``collab_cog_artifacts`` on the shared pool.

    No ``_ensure_schema``: the table is created by the versioned, lock-guarded
    runner in :mod:`..frames.collab_schema` (version 11). A store that also
    emitted DDL would reintroduce the unlocked ``CREATE TABLE IF NOT EXISTS``
    race that runner exists to remove.
    """

    def __init__(self, db):
        self._db = db

    @contextmanager
    def _own_connection(self):
        """A pooled connection of this call's own, whose statements the server bounds.

        The pool timeout bounds checkout, and this transaction-local
        ``statement_timeout`` bounds execution to
        :data:`SWEEP_STATEMENT_TIMEOUT_SECONDS` while the transport is alive.
        Transaction-local, so the connection returns to the pool unaltered.
        Every write method of the store itself takes one: the store never
        consults whether a sweep holds the lock, so a targeted write or a
        direct call lands on its own session however many sweeps are running
        (issue #148). The API reads (get/list) keep the ordinary checkout:
        they run on the request path, which has its own semantics.
        """

        with self._db.connection() as conn:
            conn.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(int(SWEEP_STATEMENT_TIMEOUT_SECONDS * 1000)),),
            )
            yield conn

    def known(self, source_id: str) -> list[KnownArtifact]:
        with self._own_connection() as conn:
            return _select_known(conn, source_id)

    def upsert(self, artifact: CogArtifact) -> None:
        _checked(artifact)  # before a connection is checked out
        with self._own_connection() as conn:
            _upsert_row(conn, artifact)

    def note_publication(self, attempt_id, source_id, repository, digest, *, user_id, org_id) -> None:
        with bounded_connection(self._db) as conn:
            conn.execute(
                """
                DELETE FROM collab_cog_publication_attempts
                WHERE accepted_at IS NULL AND created_at < now() - make_interval(secs => %s)
                """,
                (UNACCEPTED_ATTEMPT_MAX_AGE_SECONDS,),
            )
            conn.execute(
                """
                INSERT INTO collab_cog_publication_attempts
                    (attempt_id, source_id, repository, digest, published_by, published_org)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (attempt_id, source_id, repository, digest, user_id, org_id),
            )

    def accept_publication(self, attempt_id) -> None:
        with bounded_connection(self._db) as conn:
            conn.execute(
                """
                UPDATE collab_cog_publication_attempts SET accepted_at = clock_timestamp()
                WHERE attempt_id = %s AND accepted_at IS NULL
                """,
                (attempt_id,),
            )

    def accepted_digests(self, source_id, repository, org_id) -> set[str]:
        with bounded_connection(self._db) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT digest FROM collab_cog_publication_attempts
                WHERE source_id = %s AND repository = %s AND published_org IS NOT DISTINCT FROM %s
                  AND accepted_at IS NOT NULL
                """,
                (source_id, repository, org_id),
            ).fetchall()
        return {row["digest"] for row in rows}

    def forget_publication(self, attempt_id) -> None:
        with bounded_connection(self._db) as conn:
            conn.execute(
                "DELETE FROM collab_cog_publication_attempts WHERE attempt_id = %s AND accepted_at IS NULL",
                (attempt_id,),
            )

    def record_published(self, artifact, *, tag) -> None:
        import psycopg

        _checked(artifact)
        try:
            # The request's own budget, not the sweep's: this runs inside a
            # push, and must give its thread and its connection back when the
            # request's time is up.
            with bounded_connection(self._db) as conn:
                # One repository's tag assignments are serialized: two
                # publishes moving one tag at once must not each strip it
                # from rows the other has not written yet and leave it on
                # both. Released with the transaction.
                conn.execute(
                    "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                    (PUBLISH_LOCK_CLASS, f"{artifact.source_id}/{artifact.repository}"),
                )
                # Every row this transaction will change, locked first and in
                # (repository, digest) order: its own digest, and the rows
                # the tag is taken from. The sweep's removal locks in the
                # same order.
                conn.execute(
                    """
                    SELECT digest FROM collab_cog_artifacts
                    WHERE source_id = %s AND repository = %s AND (digest = %s OR %s::text = ANY(tags))
                    ORDER BY repository, digest
                    FOR UPDATE
                    """,
                    (artifact.source_id, artifact.repository, artifact.digest, tag),
                )
                conn.execute(
                    f"""
                INSERT INTO collab_cog_artifacts (
                    {_WRITTEN_COLUMNS}, removed_at, published_by, published_org
                )
                VALUES ({_WRITTEN_VALUES}, NULL, {_PUBLISHER})
                ON CONFLICT (source_id, repository, digest) DO UPDATE SET
                    {_REPLACED_COLUMNS},
                    tags = CASE WHEN collab_cog_artifacts.removed_at IS NULL THEN ARRAY(
                               SELECT DISTINCT tag FROM unnest(collab_cog_artifacts.tags || EXCLUDED.tags) AS tag
                               ORDER BY tag)
                           ELSE EXCLUDED.tags END,
                    removed_at = NULL
                    """,
                    (
                        *_written_values(artifact, [tag] if tag is not None else []),
                        *_key(artifact),
                        *_key(artifact),
                    ),
                )
                if tag is not None:
                    conn.execute(
                        """
                        UPDATE collab_cog_artifacts SET tags = array_remove(tags, %s)
                        WHERE source_id = %s AND repository = %s AND digest <> %s AND %s = ANY(tags)
                        """,
                        (tag, artifact.source_id, artifact.repository, artifact.digest, tag),
                    )
        except psycopg.DataError as exc:
            raise CogCatalogDataError(type(exc).__name__) from exc

    def update_tags(self, source_id, repository, digest, tags, *, pushed_at=None) -> bool:
        require_aware(pushed_at, "pushed_at")  # before a connection is checked out
        with self._own_connection() as conn:
            return _update_tags_row(conn, source_id, repository, digest, tags, pushed_at=pushed_at)

    def mark_removed(self, source_id, present, *, excluding=(), written_before=None) -> int:
        with self._own_connection() as conn:
            marked = _mark_removed_rows(conn, source_id, present, excluding=excluding, written_before=written_before)
        # A connection of its own, after the removal has committed, so a
        # failed cleanup cannot take the removal down with it.
        with self._own_connection() as conn:
            _forget_removed_blobs(conn, source_id)
        return marked

    def collect_unconfigured_blobs(
        self, configured, *, retention=DEFAULT_REMOVED_SOURCE_RETENTION, limit=MAX_COLLECTED_MANIFESTS_PER_SWEEP
    ) -> int:
        ids, limit = _checked_collection(configured, retention, limit)
        with self._own_connection() as conn:
            return _collect_unconfigured_blobs(conn, ids, retention, limit)

    def clock(self) -> datetime:
        with self._own_connection() as conn:
            return _read_clock(conn)

    def mark_removed_one(self, source_id, repository, digest) -> bool:
        # A targeted entry point (webhook delete), never the sweep's: its own
        # bounded connection, so it works concurrently with a sweep instead
        # of riding -- and dying with -- the lock session (issue #128).
        with self._own_connection() as conn:
            row = conn.execute(
                """
                UPDATE collab_cog_artifacts SET removed_at = now()
                WHERE source_id = %s AND repository = %s AND digest = %s AND removed_at IS NULL
                RETURNING digest
                """,
                (source_id, repository, digest),
            ).fetchone()
            if row is not None:
                # Same transaction as the mark, which holds the row: a
                # recording in flight finished before the mark could start,
                # and none can begin until this commits.
                conn.execute(
                    """
                    DELETE FROM collab_cog_manifest_blobs
                    WHERE source_id = %s AND repository = %s AND manifest_digest = %s
                    """,
                    (source_id, repository, digest),
                )
        return row is not None

    def sweep_lock(self) -> AbstractContextManager[SweepView | None]:
        return _postgres_sweep_lock(self._db)

    def get(self, digest, *, source_id=None, repository=None) -> CogArtifact | None:
        with self._db.connection() as conn:
            row = conn.execute(
                f"""
                SELECT {_COLUMNS} FROM collab_cog_artifacts
                WHERE digest = %s
                  AND (%s::text IS NULL OR source_id = %s)
                  AND (%s::text IS NULL OR repository = %s)
                ORDER BY (removed_at IS NOT NULL), indexed_at DESC, source_id, repository
                LIMIT 1
                """,
                (digest, source_id, source_id, repository, repository),
            ).fetchone()
        return _row_to_artifact(row) if row else None

    def locations(self, digest) -> list[CogArtifact]:
        with self._db.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM collab_cog_artifacts WHERE digest = %s ORDER BY source_id, repository",
                (digest,),
            ).fetchall()
        return [_row_to_artifact(row) for row in rows]

    def list_current(self, filters=None, *, limit=DEFAULT_LIST_LIMIT, offset=0) -> list[CogArtifact]:
        from psycopg.types.json import Jsonb

        limit = _bounded_limit(limit)
        offset = _checked_offset(offset)
        filters = filters or CatalogFilter()
        needle = filters.containment()
        sql, params = self.current_query(filters, needle, limit, offset)
        with self._db.connection() as conn:
            rows = conn.execute(sql, [Jsonb(p) if isinstance(p, dict) else p for p in params]).fetchall()
        return [_row_to_artifact(row) for row in rows]

    @staticmethod
    def current_query(
        filters: CatalogFilter, needle: dict[str, Any] | None, limit: int, offset: int = 0
    ) -> tuple[str, list[Any]]:
        """The ``list_current`` statement, exposed so a live test can EXPLAIN it.

        Two stages, so a filter tests each Cog's *current* row rather than
        selecting an older version that happens to match: the inner
        ``DISTINCT ON (cog_id)`` picks the newest present row per Cog (scoped
        by ``source_id`` when given), the outer ``WHERE`` filters those rows.
        The card filter is ``card @> %s`` -- the operator the GIN index
        (``jsonb_path_ops``) exists for -- applied twice: inside, as a
        candidate prefilter on ``cog_id`` the index answers (a Cog whose
        current card matches has at least one matching row, so it is never
        dropped), and outside, as the test itself. Everything else is an
        equality on an indexed or cheap column, or the ``q`` substring match.
        The page is ordered by ``cog_id`` in the ``"C"`` collation (code-point
        order), the order the in-memory store uses, so paging is identical
        on both and independent of the database's locale.
        """

        inner = ["removed_at IS NULL", f"status = '{STATUS_INDEXED}'", "cog_id IS NOT NULL"]
        params: list[Any] = []
        if filters.source_id is not None:
            inner.append("source_id = %s")
            params.append(filters.source_id)
        if needle is not None:
            inner.append("cog_id IN (SELECT cog_id FROM collab_cog_artifacts WHERE card @> %s)")
            params.append(needle)
        outer: list[str] = []
        for column, value in (("kind", filters.kind), ("publisher", filters.publisher)):
            if value is not None:
                outer.append(f"{column} = %s")
                params.append(value)
        if needle is not None:
            outer.append("card @> %s")
            params.append(needle)
        if filters.q is not None:
            outer.append(
                "(name ILIKE %s ESCAPE %s OR (jsonb_typeof(card->'description') = 'string'"
                " AND card->>'description' ILIKE %s ESCAPE %s))"
            )
            pattern = like_pattern(filters.q)
            params.extend([pattern, LIKE_ESCAPE, pattern, LIKE_ESCAPE])
        params.extend([limit, offset])
        sql = f"""
            SELECT {_COLUMNS} FROM (
                SELECT DISTINCT ON (cog_id) {_COLUMNS}
                FROM collab_cog_artifacts
                WHERE {" AND ".join(inner)}
                ORDER BY cog_id, pushed_at DESC NULLS LAST, indexed_at DESC, source_id, repository, digest
            ) AS current_cogs
            WHERE {" AND ".join(outer) or "TRUE"}
            ORDER BY cog_id COLLATE "C"
            LIMIT %s OFFSET %s
        """
        return sql, params

    def list_repositories(self) -> list[CogArtifact]:
        with self._db.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT ON (repository) {_COLUMNS} FROM collab_cog_artifacts
                WHERE removed_at IS NULL AND status = '{STATUS_INDEXED}' AND cog_id IS NOT NULL
                ORDER BY repository, pushed_at DESC NULLS LAST, indexed_at DESC, source_id, digest
                """
            ).fetchall()
        # Code-point order, as the in-memory store sorts, whatever the locale.
        return sorted((_row_to_artifact(row) for row in rows), key=lambda row: row.repository)

    def list_versions(self, cog_id, *, include_removed=False) -> list[CogArtifact]:
        with self._db.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT {_COLUMNS} FROM collab_cog_artifacts
                WHERE cog_id = %s AND status = '{STATUS_INDEXED}' AND (%s OR removed_at IS NULL)
                ORDER BY pushed_at DESC NULLS LAST, indexed_at DESC, source_id, repository, digest
                """,
                (cog_id, include_removed),
            ).fetchall()
        return [_row_to_artifact(row) for row in rows]

    def repository_known(self, repository) -> bool:
        with bounded_connection(self._db) as conn:
            row = conn.execute(
                "SELECT 1 AS found FROM collab_cog_artifacts WHERE repository = %s LIMIT 1",
                (repository,),
            ).fetchone()
        return row is not None

    def find_pullable(self, repository, source_ids, *, digest=None, tag=None) -> list[PullableArtifact]:
        _one_of(digest, tag)
        match = "a.digest = %s" if digest is not None else "%s = ANY(a.tags)"
        with bounded_connection(self._db) as conn:
            rows = conn.execute(
                f"""
                SELECT a.source_id, a.repository, a.digest, a.tags,
                       EXISTS (
                           SELECT 1 FROM collab_cog_manifest_blobs b
                           WHERE b.source_id = a.source_id AND b.repository = a.repository
                             AND b.manifest_digest = a.digest
                       ) AS blobs_recorded
                FROM collab_cog_artifacts a
                WHERE a.repository = %s AND {match} AND a.source_id = ANY(%s) AND {_PULLABLE_A}
                ORDER BY a.pushed_at DESC NULLS LAST, a.indexed_at DESC, a.source_id, a.repository, a.digest
                LIMIT %s
                """,
                (repository, digest if digest is not None else tag, list(source_ids), MAX_PULL_CANDIDATES),
            ).fetchall()
        return [
            PullableArtifact(
                source_id=row["source_id"],
                repository=row["repository"],
                digest=row["digest"],
                tags=tuple(row["tags"] or ()),
                blobs_recorded=bool(row["blobs_recorded"]),
            )
            for row in rows
        ]

    def has_pullable(self, repository, source_ids) -> bool:
        with bounded_connection(self._db) as conn:
            row = conn.execute(
                f"""
                SELECT 1 AS found FROM collab_cog_artifacts
                WHERE repository = %s AND source_id = ANY(%s) AND {_PULLABLE}
                LIMIT 1
                """,
                (repository, list(source_ids)),
            ).fetchone()
        return row is not None

    def list_pullable_tags(self, repository, source_ids, *, after=None, limit=DEFAULT_TAGS_PAGE) -> list[str]:
        with bounded_connection(self._db) as conn:
            rows = conn.execute(
                f"""
                SELECT tag FROM (
                    SELECT DISTINCT tag FROM collab_cog_artifacts, unnest(tags) AS tag
                    WHERE repository = %s AND source_id = ANY(%s) AND {_PULLABLE}
                ) AS tags
                WHERE %s::text IS NULL OR tag COLLATE "C" > %s
                ORDER BY tag COLLATE "C"
                LIMIT %s
                """,
                (repository, list(source_ids), after, after, _tags_limit(limit)),
            ).fetchall()
        return [row["tag"] for row in rows]

    def record_manifest_blobs(self, source_id, repository, manifest_digest, blobs) -> None:
        descriptors = list(blobs)
        if not descriptors:
            return
        with bounded_connection(self._db) as conn:
            # One statement, and a no-op once the manifest has been recorded:
            # what a digest references cannot change.
            #
            # Only for an artifact whose row is present, and holding that row
            # shared while the descriptors go in. Marking it removed updates
            # the row, so a removal waits for this statement and then deletes
            # everything it wrote, and a recording that arrives behind a
            # removal finds no present row and writes nothing. That is what
            # keeps a manifest's descriptors all-or-none (see the base class).
            #
            # A recording that wrote anything also stamps the source as seen
            # configured (issue #193), in the same statement: whoever records
            # for a source has it configured, so the indexer's collection of
            # unconfigured sources' descriptors leaves this one alone. Only a
            # first recording writes, so the stamp costs no later read a write.
            conn.execute(
                """
                WITH present AS (
                    SELECT 1 FROM collab_cog_artifacts
                    WHERE source_id = %s AND repository = %s AND digest = %s AND removed_at IS NULL
                    FOR SHARE
                ), recorded AS (
                    INSERT INTO collab_cog_manifest_blobs
                        (source_id, repository, manifest_digest, blob_digest, size, media_type)
                    SELECT %s, %s, %s, blob.digest, blob.size, blob.media_type
                    FROM unnest(%s::text[], %s::bigint[], %s::text[]) AS blob(digest, size, media_type)
                    WHERE EXISTS (SELECT 1 FROM present)
                    ON CONFLICT DO NOTHING
                    RETURNING 1
                )
                INSERT INTO collab_cog_source_activity AS seen (source_id, last_configured_at)
                SELECT %s, now() WHERE EXISTS (SELECT 1 FROM recorded)
                ON CONFLICT (source_id) DO UPDATE
                SET last_configured_at = GREATEST(seen.last_configured_at, EXCLUDED.last_configured_at)
                """,
                (
                    source_id,
                    repository,
                    manifest_digest,
                    source_id,
                    repository,
                    manifest_digest,
                    [blob.digest for blob in descriptors],
                    [blob.size for blob in descriptors],
                    [blob.media_type for blob in descriptors],
                    source_id,
                ),
            )

    def find_blob(self, repository, blob_digest, source_ids) -> list[PullableBlob]:
        with bounded_connection(self._db) as conn:
            rows = conn.execute(
                f"""
                SELECT source_id, repository, manifest_digest, blob_digest, size FROM (
                    SELECT DISTINCT ON (b.source_id)
                           b.source_id, b.repository, b.manifest_digest, b.blob_digest, b.size,
                           a.pushed_at, a.indexed_at
                    FROM collab_cog_manifest_blobs b
                    JOIN collab_cog_artifacts a
                      ON a.source_id = b.source_id AND a.repository = b.repository AND a.digest = b.manifest_digest
                    WHERE b.repository = %s AND b.blob_digest = %s AND b.source_id = ANY(%s)
                      AND {_PULLABLE_A}
                    ORDER BY b.source_id, a.pushed_at DESC NULLS LAST, a.indexed_at DESC, a.digest
                ) AS located
                ORDER BY pushed_at DESC NULLS LAST, indexed_at DESC, source_id
                LIMIT %s
                """,
                (repository, blob_digest, list(source_ids), MAX_PULL_CANDIDATES),
            ).fetchall()
        return [
            PullableBlob(
                source_id=row["source_id"],
                repository=row["repository"],
                manifest_digest=row["manifest_digest"],
                digest=row["blob_digest"],
                size=int(row["size"]),
            )
            for row in rows
        ]


class _PostgresSweepView(SweepView):
    """The sweep-path calls bound to the connection the sweep lock is held on.

    Yielded by :func:`_postgres_sweep_lock` for exactly as long as the lock is
    acquired; the only way onto that session (issue #148). Safe to share
    with the lock because the indexer runs its store calls one at a time and
    every sweep-path statement is a single statement, fine under the
    autocommit the session uses; ``statement_timeout`` is session-set by the
    lock helper, so no transaction-local set is needed (under autocommit it
    would be a no-op anyway). Calls on a view whose lock has been released
    are a bug: the connection has gone back to the pool.
    """

    def __init__(self, conn) -> None:
        self._conn = conn

    def known(self, source_id: str) -> list[KnownArtifact]:
        return _select_known(self._conn, source_id)

    def clock(self) -> datetime:
        return _read_clock(self._conn)

    def upsert(self, artifact: CogArtifact) -> None:
        _upsert_row(self._conn, artifact)

    def update_tags(self, source_id, repository, digest, tags, *, pushed_at=None) -> bool:
        return _update_tags_row(self._conn, source_id, repository, digest, tags, pushed_at=pushed_at)

    def mark_removed(self, source_id, present, *, excluding=(), written_before=None) -> int:
        marked = _mark_removed_rows(self._conn, source_id, present, excluding=excluding, written_before=written_before)
        _forget_removed_blobs(self._conn, source_id)  # autocommit: the removal already stands
        return marked

    def collect_unconfigured_blobs(
        self, configured, *, retention=DEFAULT_REMOVED_SOURCE_RETENTION, limit=MAX_COLLECTED_MANIFESTS_PER_SWEEP
    ) -> int:
        ids, limit = _checked_collection(configured, retention, limit)
        return _collect_unconfigured_blobs(self._conn, ids, retention, limit)


def _select_known(conn, source_id: str) -> list[KnownArtifact]:
    rows = conn.execute(
        "SELECT repository, digest, tags, status, removed_at IS NOT NULL AS removed"
        " FROM collab_cog_artifacts WHERE source_id = %s",
        (source_id,),
    ).fetchall()
    return [
        KnownArtifact(
            repository=row["repository"],
            digest=row["digest"],
            tags=tuple(row["tags"] or ()),
            status=row["status"],
            removed=row["removed"],
        )
        for row in rows
    ]


def _upsert_row(conn, artifact: CogArtifact) -> None:
    import psycopg

    _checked(artifact)
    # `indexed_at` is the server's clock: rows compare across replicas.
    # The card goes in as jsonb verbatim -- the reader's dict, structure
    # preserved -- and `removed_at` is cleared because a row being
    # (re)written was just seen in the registry.
    #
    # Who published it through the Hub is not the reader's to say: see
    # _PUBLISHER. Attempts are only read here, never written, so this
    # statement locks the artifact row and nothing else -- the same, and
    # in the same order, as the publish write below.
    try:
        conn.execute(
            f"""
            INSERT INTO collab_cog_artifacts (
                {_WRITTEN_COLUMNS}, removed_at, published_by, published_org
            )
            VALUES ({_WRITTEN_VALUES}, NULL, {_PUBLISHER})
            ON CONFLICT (source_id, repository, digest) DO UPDATE SET
                {_REPLACED_COLUMNS},
                tags = EXCLUDED.tags,
                removed_at = NULL
            """,
            (*_written_values(artifact, sorted(set(artifact.tags))), *_key(artifact), *_key(artifact)),
        )
    except psycopg.DataError as exc:
        # This row's *content* is what the server refused (NUL in a jsonb
        # string is the known case) -- an availability problem it is not, and
        # the two must fail differently: the indexer records a data error
        # against the artifact and continues, while an outage aborts its
        # sweep. Class name only; the server's message quotes the offending
        # value.
        raise CogCatalogDataError(type(exc).__name__) from exc


def _update_tags_row(conn, source_id, repository, digest, tags, *, pushed_at=None) -> bool:
    require_aware(pushed_at, "pushed_at")
    row = conn.execute(
        """
        UPDATE collab_cog_artifacts
        SET tags = %s,
            pushed_at = COALESCE(%s, pushed_at),
            removed_at = NULL
        WHERE source_id = %s AND repository = %s AND digest = %s
        RETURNING digest
        """,
        (sorted(set(tags)), pushed_at, source_id, repository, digest),
    ).fetchone()
    return row is not None


def _mark_removed_rows(conn, source_id, present, *, excluding=(), written_before=None) -> int:
    from psycopg.types.json import Jsonb

    # The present set travels as one jsonb document ({repo: [digest, ...]})
    # and is unnested server-side, so a source with thousands of artifacts
    # is one statement rather than one per repository, and the whole
    # decision is one snapshot. Repositories whose listing failed travel
    # as an array and are excluded from the UPDATE outright. So are rows
    # written at or after `written_before`, when it is given: the
    # condition is part of this statement, so no write can slip between a
    # check and the removal.
    #
    # The rows are locked in (repository, digest) order before they are
    # updated -- the conditions are re-checked on each row as its lock is
    # taken -- which is the order the publish write locks its rows in
    # (record_published). Two writers that take several row locks in one
    # order cannot deadlock.
    document = {repo: sorted(set(digests)) for repo, digests in present.items()}
    shielded = sorted(set(excluding))
    row = conn.execute(
        """
        WITH present AS (
            SELECT repo.key AS repository, digest.value #>> '{}' AS digest
            FROM jsonb_each(%s) AS repo,
                 jsonb_array_elements(repo.value) AS digest
        ),
        gone AS (
            SELECT a.repository, a.digest FROM collab_cog_artifacts a
            WHERE a.source_id = %s
              AND a.removed_at IS NULL
              AND NOT (a.repository = ANY(%s))
              AND (%s::timestamptz IS NULL OR a.indexed_at < %s)
              AND NOT EXISTS (
                  SELECT 1 FROM present p
                  WHERE p.repository = a.repository AND p.digest = a.digest
              )
            ORDER BY a.repository, a.digest
            FOR UPDATE OF a
        ),
        marked AS (
            UPDATE collab_cog_artifacts a
            SET removed_at = now()
            FROM gone
            WHERE a.source_id = %s AND a.repository = gone.repository AND a.digest = gone.digest
            RETURNING 1
        )
        SELECT count(*) AS n FROM marked
        """,
        (Jsonb(document), source_id, shielded, written_before, written_before, source_id),
    ).fetchone()
    return int(row["n"]) if row else 0


def _read_clock(conn) -> datetime:
    return conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]


def _forget_removed_blobs(conn, source_id: str) -> None:
    """Delete the blob descriptors recorded for the removed artifacts of ``source_id``.

    A removed manifest's descriptors serve nothing (:meth:`find_blob`
    joins them to a present row), so they go where rows stop being
    present instead of accumulating. Once per source per sweep, never on
    a request.

    Each removed row is locked before its descriptors are deleted, and
    the lock re-reads it: a row a reindex has just made present again is
    left alone, and a reindex that arrives meanwhile waits and then finds
    the descriptors gone and the row saying so. A row something else
    holds is skipped and taken by the next sweep. Nothing can be recorded
    for a removed row (:meth:`record_manifest_blobs`), so what is deleted
    here is always a manifest's whole set.

    Housekeeping, and a statement of its own after the removal has been
    recorded: if it fails, the removal stands, the sweep goes on, and
    the next sweep deletes the same rows.
    """

    import psycopg

    try:
        conn.execute(
            """
            WITH gone AS (
                SELECT a.repository, a.digest FROM collab_cog_artifacts a
                WHERE a.source_id = %s AND a.removed_at IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM collab_cog_manifest_blobs b
                      WHERE b.source_id = a.source_id AND b.repository = a.repository
                        AND b.manifest_digest = a.digest
                  )
                FOR UPDATE OF a SKIP LOCKED
            )
            DELETE FROM collab_cog_manifest_blobs b USING gone
            WHERE b.source_id = %s AND b.repository = gone.repository AND b.manifest_digest = gone.digest
            """,
            (source_id, source_id),
        )
    except psycopg.Error as exc:
        logger.warning(
            "cog_catalog_blob_cleanup_failed", extra={"source_id": source_id, "error": type(exc).__name__}
        )


def _collect_unconfigured_blobs(conn, configured: list[str], retention: timedelta, limit: int) -> int:
    """Stamp the configured sources, start the clock of unstamped ones, and collect a batch (issue #193).

    See :meth:`SweepView.collect_unconfigured_blobs`. Three statements, each
    atomic on its own and each safe in any order against the others and
    against a concurrent sweep, recording or removal:

    - The stamp only moves forward (``GREATEST``), and only for configured
      ids, which step three never deletes for.
    - Starting a clock inserts ``now()`` for a source with descriptors and
      no stamp and never updates an existing stamp, so it can only delay a
      collection. It finds the sources with a loose index scan over the
      primary key (one probe per distinct ``source_id``), not a table scan.
    - The deletion locks the artifact rows of up to ``limit`` manifests of
      collectable sources that have descriptors, ``FOR UPDATE SKIP LOCKED``
      (the lock a recording takes ``FOR SHARE``, so a recording in flight is
      skipped rather than split, and one that arrives later waits and then
      writes a whole set; a skipped row does not use up the batch), and
      deletes every descriptor of each manifest it holds. Only
      ``collab_cog_manifest_blobs`` is deleted from. Descriptors with no
      artifact row at all are left alone: without a row there is nothing to
      lock against a recording of an artifact recreated mid-statement, and
      the catalog never deletes artifact rows, so only hand edits make them
      (the manual cleanup in docs/frames-operations.md covers them).

    Housekeeping, like :func:`_forget_removed_blobs`: a database error is
    logged by class and answers ``0``; the next sweep tries again. The sweep
    is not failed for it.
    """

    import psycopg

    try:
        conn.execute(
            """
            INSERT INTO collab_cog_source_activity AS seen (source_id, last_configured_at)
            SELECT source_id, now() FROM unnest(%s::text[]) AS configured(source_id)
            ON CONFLICT (source_id) DO UPDATE
            SET last_configured_at = GREATEST(seen.last_configured_at, EXCLUDED.last_configured_at)
            """,
            (configured,),
        )
        conn.execute(
            """
            WITH RECURSIVE sources AS (
                (SELECT source_id FROM collab_cog_manifest_blobs ORDER BY source_id LIMIT 1)
                UNION ALL
                SELECT (
                    SELECT b.source_id FROM collab_cog_manifest_blobs b
                    WHERE b.source_id > sources.source_id ORDER BY b.source_id LIMIT 1
                )
                FROM sources WHERE sources.source_id IS NOT NULL
            )
            INSERT INTO collab_cog_source_activity (source_id, last_configured_at)
            SELECT source_id, now() FROM sources
            WHERE source_id IS NOT NULL AND NOT (source_id = ANY(%s::text[]))
            ON CONFLICT (source_id) DO NOTHING
            """,
            (configured,),
        )
        row = conn.execute(
            """
            WITH collectable AS MATERIALIZED (
                SELECT s.source_id FROM collab_cog_source_activity s
                WHERE NOT (s.source_id = ANY(%s::text[])) AND s.last_configured_at < now() - %s::interval
                  AND EXISTS (SELECT 1 FROM collab_cog_manifest_blobs b WHERE b.source_id = s.source_id)
            ), held AS MATERIALIZED (
                SELECT a.source_id, a.repository, a.digest
                FROM collab_cog_artifacts a
                WHERE a.source_id IN (SELECT source_id FROM collectable)
                  AND EXISTS (
                      SELECT 1 FROM collab_cog_manifest_blobs b
                      WHERE b.source_id = a.source_id AND b.repository = a.repository
                        AND b.manifest_digest = a.digest
                  )
                ORDER BY a.source_id, a.repository, a.digest
                LIMIT %s
                FOR UPDATE OF a SKIP LOCKED
            ), deleted AS (
                DELETE FROM collab_cog_manifest_blobs b USING held h
                WHERE b.source_id = h.source_id AND b.repository = h.repository
                  AND b.manifest_digest = h.digest
                RETURNING b.source_id, b.repository, b.manifest_digest
            )
            SELECT count(DISTINCT (source_id, repository, manifest_digest)) AS manifests FROM deleted
            """,
            (configured, retention, limit),
        ).fetchone()
    except psycopg.Error as exc:
        logger.warning("cog_catalog_source_blob_collection_failed", extra={"error": type(exc).__name__})
        return 0
    return int(row["manifests"]) if row else 0


@contextmanager
def _postgres_sweep_lock(db):
    """``pg_try_advisory_lock(COG_INDEX_LOCK_KEY)`` on one pooled connection, held for the block.

    Yields a :class:`_PostgresSweepView` bound to that connection when the
    lock was taken, else ``None``. The lock is **session**-level, not
    transaction-level, and the connection is switched to autocommit for the
    duration so that no transaction (and no snapshot) stays open while the
    sweep talks to registries for minutes. The connection is one pool slot
    the sweep occupies; autocommit is restored before it goes back to the
    pool so the next borrower sees the transaction semantics every other
    store relies on. If the process dies mid-sweep the server releases the
    lock with the connection -- and with it every sweep write still in
    flight, because the view is the sweep's only way to the database and it
    is bound to this connection (issue #128). The view is the block's alone:
    a losing acquisition gets ``None`` and touches no session but its own.

    A cancelled sweep's release is queued on the indexer's sweep thread
    behind the view call it abandoned (issue #148), so the unlock runs after
    that call has returned -- never while its statement is in flight, and
    never before a call that had started but not yet reached the server.
    """

    with db.connection() as conn:
        original_autocommit = conn.autocommit
        acquired = False
        # The connection goes back to the pool only if every step of taking
        # the lock and giving it back is known to have succeeded. Anything
        # else -- the acquire statement failing (did the server take the lock
        # before the error?), the unlock failing (the session still holds
        # it), the RESET or the autocommit restore failing (the next borrower
        # would inherit a 20 s statement timeout, or autocommit) -- leaves
        # the session's state uncertain, and an uncertain session must not
        # be handed to the next borrower. Closing it makes the pool discard
        # it and open a fresh one; the server releases any lock with it.
        clean = False
        try:
            conn.autocommit = True
            # Session-set (autocommit makes a transaction-local set a no-op)
            # and RESET below before the connection returns. It bounds every
            # statement the view runs on this session, and a cancelled
            # sweep's release is queued behind whichever of them is in
            # flight -- so this is what bounds how long a cancelled sweep can
            # keep its lock while the server still answers.
            conn.execute(f"SET statement_timeout = '{int(SWEEP_STATEMENT_TIMEOUT_SECONDS * 1000)}ms'")
            row = conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (COG_INDEX_LOCK_KEY,)).fetchone()
            acquired = bool(row and row["locked"])
            try:
                yield _PostgresSweepView(conn) if acquired else None
            finally:
                if not conn.closed:
                    if acquired:
                        conn.execute("SELECT pg_advisory_unlock(%s)", (COG_INDEX_LOCK_KEY,))
                    conn.execute("RESET statement_timeout")
                    conn.autocommit = original_autocommit
                    clean = True
        finally:
            if not clean and not conn.closed:
                logger.error("cog_index_lock_connection_discarded", extra={"acquired": acquired})
                with suppress(Exception):
                    conn.close()


def card_search_fields(card: Mapping[str, Any]) -> dict[str, str | None]:
    """The search-key columns derived from a card.

    ``cog_id``, ``name``, ``version``, ``kind``, ``publisher``, ``manifest_schema``.

    Kept next to the store so the indexer and any backfill agree on which
    card keys the columns mirror. ``version`` is stringified because the
    profile may declare it as a number and the column is text.
    """

    def text(value: Any) -> str | None:
        if value is None or isinstance(value, (dict, list)):
            return None
        return value if isinstance(value, str) else json.dumps(value)

    return {
        "cog_id": text(card.get("id")),
        "name": text(card.get("name")),
        "version": text(card.get("version")),
        "kind": text(card.get("kind")),
        "publisher": text(card.get("publisher")),
        "manifest_schema": text(card.get("manifest_schema")),
    }
