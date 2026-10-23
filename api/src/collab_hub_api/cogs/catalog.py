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
four calls of :class:`SweepView` (``known``, ``upsert``, ``update_tags``,
``mark_removed``) plus ``mark_removed_one`` for a webhook delete; the read
surface is what the catalog API (#85) builds on. Rows are never deleted:
installs and runs may reference a digest long after its publisher removed
it, so removal is ``removed_at = now()`` and the row stays readable.

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
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

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

    @property
    def reference(self) -> str:
        """The pinned install reference ``<host>/<repository>@<digest>``."""

        return reference(self.host, self.repository, self.digest)

    @property
    def present(self) -> bool:
        return self.removed_at is None


logger = logging.getLogger("frames_server.cogs.catalog")


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
    """The catalog as one sweep sees it: the four sweep-path calls and nothing else.

    What :meth:`CogCatalogStore.sweep_lock` hands the sweep that took the
    lock. On Postgres the view is bound to the lock's own connection, so a
    call on it rides the lock session (issue #128) -- a sweeper that dies
    mid-write loses the write and the lock together, and nothing but the
    holder of the view can ride that session (issue #148). The in-memory
    store has no sessions and is its own view.
    """

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
        self, source_id: str, present: Mapping[str, Iterable[str]], *, excluding: Iterable[str] = ()
    ) -> int:
        """Set ``removed_at = now()`` on this source's present rows whose digest is not in ``present``.

        ``present`` maps repository -> digests enumerated this sweep; rows in
        a repository listed in ``excluding`` (one whose enumeration failed
        this sweep) are left untouched, because what was not enumerated
        cannot be declared gone. Returns the number of rows newly marked.
        Never deletes.
        """

        raise NotImplementedError


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

    def mark_removed(self, source_id, present, *, excluding=()) -> int:
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


@dataclass
class InMemoryCogCatalogStore(CogCatalogStore):
    """Process-local catalog for tests and single-process development.

    Same semantics as the Postgres store, including containment filtering
    (:func:`json_contains`) and a process-local, non-blocking sweep lock
    whose view is the store itself (no sessions to bind to).
    """

    _rows: dict[tuple[str, str, str], CogArtifact] = field(default_factory=dict)
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

    def upsert(self, artifact: CogArtifact) -> None:
        if artifact.status not in STATUSES:
            raise ValueError(f"unknown catalog status {artifact.status!r}")
        require_aware(artifact.pushed_at, "pushed_at")
        require_aware(artifact.indexed_at, "indexed_at")
        if artifact.card is not None and contains_nul(artifact.card):
            # Parity with Postgres, where jsonb refuses NUL: a card this store
            # silently accepted would be a test passing on content production
            # rejects. contains_nul walks the parsed values, so a literal
            # backslash-u0000 in documentation text is (correctly) accepted.
            raise CogCatalogDataError("card contains NUL (\\u0000), which jsonb cannot store")
        stored = replace(
            artifact,
            tags=tuple(sorted(set(artifact.tags))),
            read_errors=tuple(artifact.read_errors),
            indexed_at=artifact.indexed_at or datetime.now(UTC),
            removed_at=None,
        )
        with self._lock:
            self._rows[self._key(stored)] = stored

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

    def mark_removed(self, source_id, present, *, excluding=()) -> int:
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
                self._rows[key] = replace(row, removed_at=now)
                marked += 1
        return marked

    def mark_removed_one(self, source_id, repository, digest) -> bool:
        with self._lock:
            row = self._rows.get((source_id, repository, digest))
            if row is None or row.removed_at is not None:
                return False
            self._rows[(source_id, repository, digest)] = replace(row, removed_at=datetime.now(UTC))
            return True

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


_COLUMNS = (
    "source_id, host, repository, digest, tags, pushed_at, indexed_at, manifest_media_type, status, card, "
    "cog_id, name, version, kind, publisher, manifest_schema, read_errors, removed_at"
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
        _check_upsertable(artifact)  # before a connection is checked out
        with self._own_connection() as conn:
            _upsert_row(conn, artifact)

    def update_tags(self, source_id, repository, digest, tags, *, pushed_at=None) -> bool:
        require_aware(pushed_at, "pushed_at")  # before a connection is checked out
        with self._own_connection() as conn:
            return _update_tags_row(conn, source_id, repository, digest, tags, pushed_at=pushed_at)

    def mark_removed(self, source_id, present, *, excluding=()) -> int:
        with self._own_connection() as conn:
            return _mark_removed_rows(conn, source_id, present, excluding=excluding)

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

    def upsert(self, artifact: CogArtifact) -> None:
        _upsert_row(self._conn, artifact)

    def update_tags(self, source_id, repository, digest, tags, *, pushed_at=None) -> bool:
        return _update_tags_row(self._conn, source_id, repository, digest, tags, pushed_at=pushed_at)

    def mark_removed(self, source_id, present, *, excluding=()) -> int:
        return _mark_removed_rows(self._conn, source_id, present, excluding=excluding)


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


def _check_upsertable(artifact: CogArtifact) -> None:
    if artifact.status not in STATUSES:
        raise ValueError(f"unknown catalog status {artifact.status!r}")
    require_aware(artifact.pushed_at, "pushed_at")
    require_aware(artifact.indexed_at, "indexed_at")


def _upsert_row(conn, artifact: CogArtifact) -> None:
    import psycopg
    from psycopg.types.json import Jsonb

    _check_upsertable(artifact)
    # `indexed_at` is the server's clock: rows compare across replicas. The
    # card goes in as jsonb verbatim -- the reader's dict, structure
    # preserved -- and `removed_at` is cleared because a row being
    # (re)written was just seen in the registry.
    try:
        conn.execute(
            """
            INSERT INTO collab_cog_artifacts (
                source_id, host, repository, digest, tags, pushed_at, indexed_at, manifest_media_type,
                status, card, cog_id, name, version, kind, publisher, manifest_schema, read_errors, removed_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
            ON CONFLICT (source_id, repository, digest) DO UPDATE SET
                host = EXCLUDED.host,
                tags = EXCLUDED.tags,
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
                removed_at = NULL
            """,
            (
                artifact.source_id,
                artifact.host,
                artifact.repository,
                artifact.digest,
                sorted(set(artifact.tags)),
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
            ),
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


def _mark_removed_rows(conn, source_id, present, *, excluding=()) -> int:
    from psycopg.types.json import Jsonb

    # The present set travels as one jsonb document ({repo: [digest, ...]})
    # and is unnested server-side, so a source with thousands of artifacts is
    # one statement rather than one per repository, and the whole decision is
    # one snapshot. Repositories whose listing failed travel as an array and
    # are excluded from the UPDATE outright.
    document = {repo: sorted(set(digests)) for repo, digests in present.items()}
    shielded = sorted(set(excluding))
    row = conn.execute(
        """
        WITH present AS (
            SELECT repo.key AS repository, digest.value #>> '{}' AS digest
            FROM jsonb_each(%s) AS repo,
                 jsonb_array_elements(repo.value) AS digest
        ),
        marked AS (
            UPDATE collab_cog_artifacts a
            SET removed_at = now()
            WHERE a.source_id = %s
              AND a.removed_at IS NULL
              AND NOT (a.repository = ANY(%s))
              AND NOT EXISTS (
                  SELECT 1 FROM present p
                  WHERE p.repository = a.repository AND p.digest = a.digest
              )
            RETURNING 1
        )
        SELECT count(*) AS n FROM marked
        """,
        (Jsonb(document), source_id, shielded),
    ).fetchone()
    return int(row["n"]) if row else 0


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
