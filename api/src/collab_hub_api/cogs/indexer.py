"""Reconciliation indexer: how Cogs get from a registry into the catalog (issue #84).

One sweep, per configured source:

1. enumerate repositories and artifacts through the source adapter;
2. skip digests already in the catalog with the same tag set; a changed tag
   set (or a digest that was marked removed and is back) is written without
   refetching anything;
3. for a new digest fetch the manifest, select ``COG.md`` and the profile
   file it names, read the card, and insert. A manifest with no ``COG.md``
   is recorded as *non-Cog* with a reason so a repository full of images is
   not re-read every cycle;
4. mark this source's rows whose digest is no longer present as
   ``removed_at = now()`` -- never delete;
5. record per-artifact failures in ``read_errors`` and carry on; log a
   summary and export it as metrics.

Three things keep a sweep from doing damage:

- **Single flight, by deployment shape; the lock is a belt.** The chart
  runs the indexer as its own one-replica workload with a ``Recreate``
  strategy (issue #148), so on a healthy release there is never a second
  sweeper -- not across API replicas, not across a rollout. Under that sits
  the store's sweep lock (a session-level Postgres advisory lock), which
  catches what the shape cannot: an operator running two indexer releases
  against one database. The loser logs and waits for the next interval. The
  lock hands the winner a connection-bound *view* of the store; every read
  and write of the sweep goes through it and so rides the lock's own session
  (issue #128), which is what makes cancellation simple. Every store call
  of a sweep -- the acquisition, the view's reads and writes, the release --
  runs on one thread of the sweep's own, in submission order, so a
  cancelled sweep queues its release behind whatever call is still in
  flight and moves on without waiting: the release runs the moment that
  call returns, or, when the pod goes first, the session dies and the
  server drops the lock and the in-flight write together. No draining, no
  deadlines, nothing handed to a second thread: what a cancellation leaves
  behind is one running call, its queued release, and nothing else.
- **Removal needs a complete picture.** Rows are only marked removed for a
  source whose enumeration fully succeeded this sweep. A registry that
  answers ``list_repositories`` with an error, one repository's listing that
  fails, or an enumeration past the per-source bound leaves that source's
  removal step skipped -- a transient outage must not mark a whole catalog
  gone. The adapters uphold their half: past their own limits they *raise*
  rather than silently truncate, because a truncated list presented as
  complete is exactly what turns a bound into false removals.
- **One artifact cannot poison the sweep.** The whole per-artifact read
  is guarded (any exception, not just the OCI hierarchy -- a client bug must
  cost one row, not the sweep), and a card Postgres cannot store (NUL in a
  string, or oversized) is recorded as ``failed`` with a reason instead of
  aborting. Database *outages* still abort the sweep on purpose: retrying
  per artifact against a dead database would spend the whole budget learning
  the same fact.

**What is and is not kept out of storage and logs.** Cards are the published
bundle's own content, stored verbatim -- that is the contract (parent issue
acceptance: the reader's output, structure preserved), so anything a
publisher writes into ``COG.md`` or the profile lands in the catalog as-is.
The guarantee this module makes is narrower and absolute: *configured
registry credentials* never reach cards, ``read_errors``, or logs.
``read_errors`` and log lines therefore carry exception class names -- plus
the message only for the OCI hierarchy, whose contract is that messages name
neither URLs nor headers -- never raw URLs, tokens, or exception chains.

Fairness under bounds: enumeration is complete (or the source is marked
failed), while *fetching* is budgeted per sweep. Already-known digests cost
nothing, so successive sweeps walk past what earlier sweeps indexed and the
tail of a large registry is reached instead of starved; deferred artifacts
are still part of the present set, so removal stays correct.

Targeted entry points for the webhook receiver (#86): :meth:`CogIndexer.reindex`
for one ``(source_id, repository, digest)`` and :meth:`CogIndexer.mark_removed`
for a delete. Neither takes the sweep lock -- both are idempotent single-row
writes, and a sweep running at the same time converges to the same state.
Neither can ride a running sweep's lock session either: they write through
the store itself, whose methods take their own pooled connections and never
consult the lock (issue #148), so they land concurrently with a sweep.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime

from ..frames.observability import COG_INDEX_ARTIFACTS, COG_INDEX_SWEEP_DURATION, COG_INDEX_SWEEPS
from .bundle import COG_ENTRY_FILE, PROG_KIND, CogCard, read_cog_bundle
from .catalog import (
    STATUS_FAILED,
    STATUS_INDEXED,
    STATUS_NON_COG,
    CogArtifact,
    CogCatalogDataError,
    CogCatalogStore,
    KnownArtifact,
    SweepView,
    card_search_fields,
    contains_nul,
)
from .frontmatter import read_cog_document
from .oci import (
    DEFAULT_MAX_BUNDLE_FILE_BYTES,
    LOCKFILE_TITLE,
    MEDIA_TYPE_PIXI_LOCK,
    Descriptor,
    Manifest,
    OCIError,
    OCINotFound,
    select_bundle_layers,
)
from .profile import PIXI_MANIFEST, PROFILE_PARSED
from .registry import ArtifactRef, RegistrySource

logger = logging.getLogger("frames_server.cogs.indexer")

DEFAULT_INTERVAL_SECONDS = 300

STORE_WORKER_THREADS = 4
"""Size of the thread pool the targeted entry points' store calls run on.

The sweep does not use it: a sweep's calls run on a single thread of their
own (see :meth:`CogIndexer.__init__`), so a webhook write never queues
behind a sweep statement and a sweep's release never queues behind a
webhook write.
"""

INDEXER_SHUTDOWN_TIMEOUT_SECONDS = 5.0
"""Deadline the app lifespan puts on waiting for the cancelled indexer task.

A healthy release is one unlock statement; this is room for that and for a
release queued behind an in-flight statement that the server will end
promptly. Past it the task is left pending and logged: it dies with the
process, and the session takes the lock and any in-flight write with it --
the outcome issue #148 designs for, since the indexer is one pod that is
recreated, never rolled over. The bound is on the **lifespan's wait**, not
on the worker threads: a store call blocked on a peer that acknowledges
packets but never answers is not ended by ``statement_timeout`` (the server
never runs it) nor by keepalives (the peer is not dead); those threads go
when the process does.
"""

MAX_ARTIFACTS_PER_REPOSITORY = 10_000
"""Bound on one repository's enumerated artifacts.

A registry is somebody else's system: this is what stops a runaway or hostile
one from turning a sweep into an unbounded loop. The bound is per repository
and an over-bound repository is treated exactly like one whose listing failed
-- skipped, ``sources_failed`` counted, removal disabled for the source --
while every *other* repository still reconciles. Deliberately not a
per-source prefix: a prefix cut at the same sorted position every sweep
would permanently starve the artifacts behind it (round-2 codex finding).
"""

MAX_NEW_FETCHES_PER_SWEEP = 1_000
"""Fetch budget: how many *new* artifacts one sweep will read per source.

Distinct from the enumeration bound above, and the reason a big source makes
progress instead of starving its tail: enumeration stays complete (so removal
and tag reconciliation stay correct for every artifact), while fetching --
the expensive part, one manifest plus up to three blobs each -- is capped.
Digests already in the catalog cost nothing against the budget, so each sweep
fetches the next batch beyond what earlier sweeps indexed. Artifacts past the
budget are counted as ``deferred`` and picked up next sweep.
"""

MAX_CARD_BYTES = 8 * 1024 * 1024
"""Cap on one card's JSON text. Bundle files are capped at 256 KiB each and a
card embeds at most a few of them; a card past this is not a Cog description,
it is a payload, and it is recorded as ``failed`` rather than stored."""

MAX_ERROR_CHARS = 500
"""Cap on one ``read_errors`` entry; the row is a diagnosis, not a dump."""

MAX_TITLES_IN_REASON = 8
"""How many layer titles a non-Cog reason names before it says "and N more"."""

# Outcome labels for the per-artifact counter. Bounded by construction; the
# summary below carries one field per label.
OUTCOME_INDEXED = "indexed"
OUTCOME_SKIPPED = "skipped"
OUTCOME_RETAGGED = "retagged"
OUTCOME_NON_COG = "non_cog"
OUTCOME_FAILED = "failed"
OUTCOME_REMOVED = "removed"
OUTCOME_DEFERRED = "deferred"

RETRY_SLOT_EVERY = 4
"""Every fourth fetch slot goes to a retry of a ``failed`` row (see :meth:`CogIndexer._schedule`)."""


@dataclass
class SweepSummary:
    """What one sweep did. Logged at the end and exported as counters."""

    indexed: int = 0
    skipped: int = 0
    retagged: int = 0
    non_cog: int = 0
    failed: int = 0
    removed: int = 0
    deferred: int = 0
    """New artifacts past this sweep's fetch budget; next sweep's work."""
    sources: int = 0
    sources_failed: int = 0
    """Sources whose enumeration did not complete; their removal step was skipped."""
    locked_out: bool = False
    """Another sweeper held the sweep lock; nothing was done."""
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)
    """Source-level (not per-artifact) failures: stage + exception class, never URLs."""

    def as_log_fields(self) -> dict[str, object]:
        fields = asdict(self)
        fields["errors"] = len(self.errors)
        return fields


@dataclass
class _Enumeration:
    """One source's artifacts as seen this sweep, and whether the picture is complete."""

    present: dict[str, list[ArtifactRef]] = field(default_factory=dict)
    complete: bool = True
    """Whether the *repository list* itself was obtained. False = nothing can be declared gone."""
    failed_repositories: list[str] = field(default_factory=list)
    """Repositories whose own listing failed or was refused: reconciled nothing, removal skipped for them only."""
    errors: list[str] = field(default_factory=list)


class CogIndexer:
    """The reconciliation loop over a set of registry sources and one catalog store."""

    def __init__(
        self,
        store: CogCatalogStore,
        sources: Sequence[RegistrySource],
        *,
        max_artifacts_per_repository: int = MAX_ARTIFACTS_PER_REPOSITORY,
        max_new_fetches_per_sweep: int = MAX_NEW_FETCHES_PER_SWEEP,
        max_bytes_per_file: int = DEFAULT_MAX_BUNDLE_FILE_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._sources = list(sources)
        self._max_artifacts = max_artifacts_per_repository
        self._max_new_fetches = max_new_fetches_per_sweep
        self._max_bytes_per_file = max_bytes_per_file
        self._clock = clock
        self.last_summary: SweepSummary | None = None
        self._executor = ThreadPoolExecutor(max_workers=STORE_WORKER_THREADS, thread_name_prefix="cog-index-store")
        # The sweep's own, single thread. Every store call of a sweep -- the
        # lock acquisition, the view's reads and writes, the release -- is
        # submitted here and runs in submission order, which is the whole
        # cancellation story: a release queued behind a call that a
        # cancellation abandoned mid-flight cannot overtake it (whether the
        # call is inside psycopg or still building its statement), cannot
        # run on the event loop, and is not dropped by the cancellation --
        # the sweep only ever stops waiting for it.
        self._sweep_thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cog-index-sweep")
        # Per-source fairness state for the fetch budget (see _schedule): the
        # interleave phase advances every sweep, and the retry order is a
        # rotating list of (repository, digest) -- identities, not positions,
        # so candidates entering or leaving between sweeps cannot make the
        # rotation skip one.
        self._phase: dict[str, int] = {}
        self._retry_order: dict[str, list[tuple[str, str]]] = {}

    @property
    def sources(self) -> list[RegistrySource]:
        return list(self._sources)

    def close(self) -> None:
        """Stop accepting store calls. Does not interrupt calls already running.

        The targeted pool drops work that never started. The sweep thread
        does **not**: the only call that can be queued on it is a lock
        release behind an abandoned call, and that release must still run.
        """

        self._executor.shutdown(wait=False, cancel_futures=True)
        self._sweep_thread.shutdown(wait=False)

    # -- threading discipline ---------------------------------------------------

    async def _on_thread(self, func, /, *args, **kwargs):
        """Run a targeted entry point's store call on the shared worker pool."""

        return await asyncio.wrap_future(self._executor.submit(func, *args, **kwargs))

    async def _on_sweep_thread(self, func, /, *args, **kwargs):
        """Run one of the sweep's store calls on the sweep thread, in order with the rest.

        Cancellation returns at once: a thread cannot be interrupted, and the
        sweep does not wait for it (the drain issue #148 removed). The call
        finishes on the sweep thread and its result is dropped; the release
        the cancelled sweep queues next runs after it. A call that had not
        started is dropped unrun (``wrap_future`` cancels it) -- which for
        the sweep can only be the acquisition, and the release then finds
        nothing entered.
        """

        return await asyncio.wrap_future(self._sweep_thread.submit(func, *args, **kwargs))

    def _queue_release(self, lock, entering: Future) -> Future:
        """Queue the lock's exit on the sweep thread, behind whatever call is still running there.

        Never dropped once queued: the sweep may stop waiting for it (a
        cancellation), the executor may be closed (``close`` does not cancel
        queued work), but the exit runs. :func:`_exit_entered` exits nothing
        if the acquisition never ran or raised.
        """

        return self._sweep_thread.submit(_exit_entered, lock, entering)

    # -- the sweep ------------------------------------------------------------

    async def sweep(self) -> SweepSummary:
        """Reconcile every source once, under the sweep lock.

        Never raises for a per-artifact or per-source failure; does raise for
        a database outage (the store's own errors) and propagates cancellation
        -- in both cases after the lock's release has been submitted.
        """

        started = self._clock()
        summary = SweepSummary(sources=len(self._sources))
        # The lock is taken and released on a worker thread: it is a blocking
        # database call, and the connection it occupies stays checked out for
        # the whole sweep, carrying the sweep's reads and writes through the
        # view it yields so they die with the lock session (issue #128).
        lock = self._store.sweep_lock()
        entering: Future = self._sweep_thread.submit(lock.__enter__)
        try:
            try:
                view: SweepView | None = await asyncio.wrap_future(entering)
            except asyncio.CancelledError:
                # Cancelled while the acquisition may still be running. Not
                # waited for, but whatever it enters gets its matching exit:
                # queued behind it on the sweep thread, so it runs the moment
                # __enter__ returns. An acquisition that never started is
                # cancelled unrun, one that raised entered nothing; both
                # exit nothing.
                self._queue_release(lock, entering)
                raise
            cancelled = False
            try:
                if view is None:
                    summary.locked_out = True
                    COG_INDEX_SWEEPS.labels(result="locked_out").inc()
                    logger.info("cog_index_sweep_skipped", extra={"reason": "another sweeper holds the sweep lock"})
                    return summary
                for source in self._sources:
                    await self._sweep_source(source, summary, view)
                COG_INDEX_SWEEPS.labels(result="completed").inc()
            except asyncio.CancelledError:
                # A shutdown mid-sweep, not a failure of the sweep.
                COG_INDEX_SWEEPS.labels(result="cancelled").inc()
                cancelled = True
                raise
            except BaseException:
                COG_INDEX_SWEEPS.labels(result="failed").inc()
                raise
            finally:
                # The release goes behind whatever the sweep thread is still
                # running (a view call a cancellation abandoned, for one). A
                # cancelled sweep does not wait for it; every other outcome
                # does -- and a cancellation that lands during that wait
                # leaves the queued release exactly where it is.
                release = self._queue_release(lock, entering)
                if not cancelled:
                    await _await_without_cancelling(release)
        finally:
            summary.duration_seconds = self._clock() - started
            COG_INDEX_SWEEP_DURATION.observe(summary.duration_seconds)
            self.last_summary = summary
            logger.info("cog_index_sweep", extra=summary.as_log_fields())
        return summary

    async def _sweep_source(self, source: RegistrySource, summary: SweepSummary, view: SweepView) -> None:
        enumeration = await self._enumerate(source)
        if enumeration.errors:
            summary.sources_failed += 1
            for error in enumeration.errors:
                summary.errors.append(f"{source.id}: {error}")
                logger.warning("cog_index_source_failed", extra={"source": source.id, "reason": error})
        if not enumeration.complete:
            # No repository list: nothing to reconcile and nothing that can be
            # declared gone.
            return

        known = {(row.repository, row.digest): row for row in await self._on_sweep_thread(view.known, source.id)}
        # Three classes, mirroring _reconcile's decision: bookkeeping (known,
        # not failed) costs no budget; a fetch happens for digests the catalog
        # has never seen and for retries of failed rows. Which fetches get
        # this sweep's budget is decided by _schedule, which interleaves the
        # two classes so neither can starve the other.
        unseen: list[tuple[str, ArtifactRef, None]] = []
        retries: list[tuple[str, ArtifactRef, KnownArtifact]] = []
        for repository, artifacts in enumeration.present.items():
            for artifact in artifacts:
                known_row = known.get((repository, artifact.digest))
                if known_row is None:
                    unseen.append((repository, artifact, None))
                elif known_row.status == STATUS_FAILED:
                    retries.append((repository, artifact, known_row))
                else:
                    outcome = await self._reconcile(source, repository, artifact, known_row, view)
                    _count(summary, outcome)
        fetch, deferred = self._schedule(source.id, unseen, retries)
        for repository, artifact, known_row in fetch:
            if known_row is not None:
                # Recorded as the attempt begins, not when the sweep was
                # planned: a sweep that raises or is cancelled partway must
                # not rotate past candidates it never tried.
                self._note_retry_attempt(source.id, (repository, artifact.digest))
            _count(summary, await self._reconcile(source, repository, artifact, known_row, view))
        for _ in range(deferred):
            _count(summary, OUTCOME_DEFERRED)

        # Removal is per repository: what was not enumerated cannot be
        # declared gone, so repositories whose listing failed keep their rows
        # untouched, while every fully listed repository -- and every
        # repository that has vanished from the source's list -- is
        # reconciled. Deferred fetches do NOT skip removal: those artifacts
        # were enumerated and stand in the present set.
        present = {repo: [artifact.digest for artifact in artifacts] for repo, artifacts in enumeration.present.items()}
        removed = await self._on_sweep_thread(
            view.mark_removed, source.id, present, excluding=enumeration.failed_repositories
        )
        summary.removed += removed
        if removed:
            COG_INDEX_ARTIFACTS.labels(outcome=OUTCOME_REMOVED).inc(removed)

    def _schedule(self, source_id: str, unseen: list, retries: list) -> tuple[list, int]:
        """Split this sweep's fetches into (fetch now, number deferred) so neither class starves.

        Slots are dealt in a fixed pattern -- every ``RETRY_SLOT_EVERY``-th
        slot to a retry of a failed row, the rest to never-seen artifacts --
        with a class that has run out yielding its slot to the other. Two
        pieces of per-source state make it fair *across* sweeps, not just
        within one: the pattern's **phase** advances each sweep, so even a
        budget of one alternates classes over successive sweeps instead of
        always serving slot zero; and retries are served from a **rotating
        order of identities** (see :meth:`_retry_candidates`), so a stable
        prefix of permanent failures cannot occupy the retry slots forever
        while a recoverable failure behind it waits. Never-seen artifacts keep
        enumeration order (once indexed they cost nothing, so the tail is
        reached without rotation).
        """

        phase = self._phase.get(source_id, 0)
        self._phase[source_id] = (phase + 1) % RETRY_SLOT_EVERY
        unseen_queue = list(unseen)
        retry_queue = self._retry_candidates(source_id, retries)
        fetch: list = []
        slot = phase
        while len(fetch) < self._max_new_fetches and (unseen_queue or retry_queue):
            take_retry = slot % RETRY_SLOT_EVERY == RETRY_SLOT_EVERY - 1
            if (take_retry and retry_queue) or not unseen_queue:
                fetch.append(retry_queue.pop(0))
            else:
                fetch.append(unseen_queue.pop(0))
            slot += 1
        return fetch, len(unseen_queue) + len(retry_queue)

    def _retry_candidates(self, source_id: str, retries: list) -> list:
        """This source's failed rows in rotation order, oldest attempt first.

        The order is remembered as ``(repository, digest)`` identities rather
        than a position, because the candidate list changes between sweeps: a
        newly failed artifact can appear anywhere in enumeration order, and a
        recovered one disappears. A positional cursor moved by those edits and
        could step over a candidate every sweep, which is the starvation this
        is here to prevent. Remembered identities that are still failing keep
        their order; ones that are new go behind them; ones that are gone are
        forgotten, so the state stays the size of the failed set.
        """

        by_key = {
            (repository, artifact.digest): (repository, artifact, known) for repository, artifact, known in retries
        }
        remembered = [key for key in self._retry_order.get(source_id, ()) if key in by_key]
        seen = set(remembered)
        order = remembered + [key for key in by_key if key not in seen]
        self._retry_order[source_id] = order
        return [by_key[key] for key in order]

    def _note_retry_attempt(self, source_id: str, key: tuple[str, str]) -> None:
        """Move one identity to the back of its source's rotation, as its attempt begins."""

        order = self._retry_order.get(source_id)
        if order and key in order:
            order.remove(key)
            order.append(key)

    async def _enumerate(self, source: RegistrySource) -> _Enumeration:
        # Broad excepts on purpose: adapters talk to third-party systems and
        # this seam is the isolation boundary -- an unexpected exception from
        # one source (a client bug, a malformed response nobody anticipated)
        # must cost that source's sweep, not the other sources'. Store errors
        # never pass through here, so a database outage still aborts.
        result = _Enumeration()
        try:
            repositories = await source.list_repositories()
        except Exception as exc:
            result.complete = False
            result.errors.append(f"list_repositories: {_describe(exc)}")
            return result
        for repository in repositories:
            try:
                artifacts = await source.list_artifacts(repository)
            except OCINotFound:
                # A configured repository that does not exist (yet) has no
                # artifacts; that is an answer, not a failure, and its rows
                # -- if it ever had any -- are correctly marked removed.
                artifacts = []
            except Exception as exc:
                result.failed_repositories.append(repository)
                result.errors.append(f"list_artifacts {repository}: {_describe(exc)}; removal skipped for it")
                continue
            if len(artifacts) > self._max_artifacts:
                # Treated exactly like a failed listing of this one
                # repository, and never as a prefix: enumeration always runs
                # to the end of the repository list, so the artifacts behind
                # an over-bound repository are not starved and the bound
                # cannot silently misrepresent a partial view as complete.
                # Only this repository's rows are shielded from removal.
                logger.warning(
                    "cog_index_repository_over_bound",
                    extra={"source": source.id, "repository": repository, "bound": self._max_artifacts},
                )
                result.failed_repositories.append(repository)
                result.errors.append(
                    f"list_artifacts {repository}: over the {self._max_artifacts}-artifact bound;"
                    " removal skipped for it"
                )
                continue
            result.present[repository] = artifacts
        return result

    async def _reconcile(
        self,
        source: RegistrySource,
        repository: str,
        artifact: ArtifactRef,
        known: KnownArtifact | None,
        view: SweepView,
    ) -> str:
        tags = tuple(sorted(set(artifact.tags)))
        if known is not None and known.status != STATUS_FAILED:
            if known.tags == tags and not known.removed:
                return OUTCOME_SKIPPED
            # Tag change, or a digest that is back after being marked removed:
            # the card is the digest's and cannot have changed. Write the tags
            # (which also clears removed_at) and move on without a fetch.
            await self._on_sweep_thread(
                view.update_tags, source.id, repository, artifact.digest, tags, pushed_at=artifact.pushed_at
            )
            return OUTCOME_RETAGGED
        row = await self._read_artifact(source, repository, artifact)
        row = await self._store_row(row, view, self._on_sweep_thread)
        if row.status == STATUS_INDEXED:
            return OUTCOME_INDEXED
        if row.status == STATUS_NON_COG:
            return OUTCOME_NON_COG
        return OUTCOME_FAILED

    async def _store_row(self, row: CogArtifact, view: SweepView, run: Callable) -> CogArtifact:
        """Upsert the row; a card the database refuses becomes a ``failed`` row, not a poisoned sweep.

        The card is pre-validated (:func:`_card_unstorable`), so this catch is
        the second line: whatever representability rule the database enforces
        that the validation did not anticipate. Only the store's *data* error
        is caught -- an outage raises through, because retrying every artifact
        against a dead database is not resilience.

        ``view`` is the sweep's connection-bound view under the lock and
        ``run`` the sweep thread, or -- for the lock-less :meth:`reindex`
        path -- the store itself and the targeted pool, so the write takes a
        pooled connection of its own (issue #148). Passed explicitly rather
        than inferred: the in-memory store is its own view.
        """

        try:
            await run(view.upsert, row)
            return row
        except CogCatalogDataError as exc:
            fallback = _with(row, status=STATUS_FAILED, card=None, read_errors=_errors(f"store: {type(exc).__name__}"))
            await run(view.upsert, fallback)
            return fallback

    # -- targeted entry points (webhook receiver, #86) --------------------------

    async def reindex(
        self,
        source_id: str,
        repository: str,
        digest: str,
        *,
        tags: Sequence[str] = (),
        pushed_at: datetime | None = None,
    ) -> CogArtifact:
        """Fetch and (re)write one artifact regardless of what the catalog holds.

        For a push event. The row is written whether the read succeeds
        (``indexed``/``non_cog``) or not (``failed``); the returned row is what
        was stored. Raises ``KeyError`` for an unknown source id.
        """

        source = self._source(source_id)
        ref = ArtifactRef(digest=digest, tags=tuple(sorted(set(tags))), pushed_at=pushed_at)
        row = await self._read_artifact(source, repository, ref)
        return await self._store_row(row, self._store, self._on_thread)

    async def mark_removed(self, source_id: str, repository: str, digest: str) -> bool:
        """Mark one artifact removed (a delete event). Returns whether a present row was marked."""

        self._source(source_id)
        marked = await self._on_thread(self._store.mark_removed_one, source_id, repository, digest)
        if marked:
            COG_INDEX_ARTIFACTS.labels(outcome=OUTCOME_REMOVED).inc()
        return marked

    def _source(self, source_id: str) -> RegistrySource:
        for source in self._sources:
            if source.id == source_id:
                return source
        raise KeyError(f"no registry source with id {source_id!r}")

    # -- reading one artifact ---------------------------------------------------

    async def _read_artifact(self, source: RegistrySource, repository: str, artifact: ArtifactRef) -> CogArtifact:
        """Turn one enumerated artifact into the row to store. Never raises except for cancellation.

        The guard is deliberately broader than the OCI hierarchy: the client's
        contract is to wrap its failures, but a contract is not a proof, and a
        failure it missed (a header-encoding bug, an unanticipated response
        shape) must cost this one row -- recorded by class name only, since an
        unknown exception's message is not known to be safe -- never the sweep.
        ``except Exception`` does not catch ``CancelledError``, so cancellation
        still propagates.
        """

        base = CogArtifact(
            source_id=source.id,
            host=source.host,
            repository=repository,
            digest=artifact.digest,
            status=STATUS_FAILED,
            tags=tuple(sorted(set(artifact.tags))),
            pushed_at=artifact.pushed_at,
            manifest_media_type=artifact.media_type,
        )
        client = source.oci()
        try:
            manifest = await client.get_manifest(repository, artifact.digest)
            base = _with(base, manifest_media_type=manifest.media_type or artifact.media_type)
            if _bundle_layer(manifest, COG_ENTRY_FILE) is None:
                return await self._read_without_entry(client, repository, manifest, base)
            files = await self._fetch_cog_files(client, repository, manifest)
            card = read_cog_bundle(files, bundle_paths=_titles(manifest))
            return _indexed(base, card)
        except OCIError as exc:
            # Recorded, not raised: one broken artifact must not stop a sweep.
            # OCI messages are kept -- the client's contract is that they name
            # neither URLs nor headers.
            return _with(base, status=STATUS_FAILED, read_errors=_errors(f"fetch: {_describe(exc)}"))
        except Exception as exc:
            return _with(base, status=STATUS_FAILED, read_errors=_errors(f"read: {type(exc).__name__}"))

    async def _fetch_cog_files(self, client, repository: str, manifest: Manifest) -> dict[str, bytes]:
        """``COG.md`` first, then the profile file its frontmatter names (plus ``pixi.toml`` for tasks).

        The manifest pointer is only known after ``COG.md`` is parsed, so this
        is two rounds of fetching rather than one guess at a file name. Never
        the lockfile: ``select_bundle_layers`` drops it by title *and* media
        type, and the direct entry fetch applies the same media-type exclusion
        (:func:`_bundle_layer`) so a layer titled ``COG.md`` that is really
        the lockfile is not fetched either.
        """

        entry_layer = _bundle_layer(manifest, COG_ENTRY_FILE)
        assert entry_layer is not None
        entry = await client.get_blob(repository, entry_layer, max_bytes=self._max_bytes_per_file)
        files = {COG_ENTRY_FILE: entry}
        pointer = read_cog_document(entry).manifest_path
        layers = select_bundle_layers(manifest, manifest_file=pointer)
        for title, descriptor in layers.items():
            if title in files or title == LOCKFILE_TITLE:
                continue
            files[title] = await client.get_blob(repository, descriptor, max_bytes=self._max_bytes_per_file)
        return files

    async def _read_without_entry(self, client, repository: str, manifest: Manifest, base: CogArtifact) -> CogArtifact:
        """No ``COG.md``: a Prog if ``pixi.toml`` declares a capability, otherwise a non-Cog with a reason."""

        pixi = _bundle_layer(manifest, PIXI_MANIFEST)
        if pixi is None:
            return _with(
                base,
                status=STATUS_NON_COG,
                read_errors=_errors(
                    f"manifest carries no {COG_ENTRY_FILE} or {PIXI_MANIFEST} layer; {_layer_summary(manifest)}"
                ),
            )
        files = {PIXI_MANIFEST: await client.get_blob(repository, pixi, max_bytes=self._max_bytes_per_file)}
        card = read_cog_bundle(files, bundle_paths=_titles(manifest))
        if card.kind == PROG_KIND and card.profile_status == PROFILE_PARSED:
            return _indexed(base, card)
        return _with(
            base,
            status=STATUS_NON_COG,
            read_errors=_errors(*card.errors) or _errors(f"manifest carries no {COG_ENTRY_FILE} layer"),
        )

    # -- the loop -----------------------------------------------------------------

    async def run(
        self,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        run_on_startup: bool = True,
        jitter: float = 0.1,
        stop: asyncio.Event | None = None,
    ) -> None:
        """Sweep on startup (unless told not to) and then every ``interval_seconds``, jittered, until cancelled.

        A sweep that raises (a store outage, a bug) is logged -- by exception
        class, never with a chain that could echo somebody's URL -- and the
        loop continues: the next interval retries. Cancellation propagates.
        ``jitter`` is a fraction of the interval added or removed at random so
        two sweepers that do exist (two releases on one database) drift apart
        instead of contending for the lock at the same instant every cycle.
        """

        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        first = True
        while stop is None or not stop.is_set():
            if not first or run_on_startup:
                try:
                    await self.sweep()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("cog_index_sweep_failed", extra={"error": type(exc).__name__})
            first = False
            delay = interval_seconds * (1 + random.uniform(-jitter, jitter))  # noqa: S311 - scheduling jitter
            if stop is None:
                await asyncio.sleep(delay)
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                continue


# -- helpers ------------------------------------------------------------------


def _exit_entered(lock, entering: Future) -> None:
    """Exit ``lock`` if its acquisition entered anything; the sweep thread's release step.

    Runs on the sweep thread after the acquisition (same thread, submission
    order), so ``entering`` is done: a cancelled future never ran, a failed
    one entered nothing, and either way there is nothing to exit -- calling
    ``__exit__`` on a context that was never entered would *run* the
    acquisition. An exit failure is logged by class name; raising would only
    reach a sweep that may no longer be waiting.
    """

    if entering.cancelled() or entering.exception() is not None:
        return
    try:
        lock.__exit__(None, None, None)
    except Exception as exc:
        logger.error("cog_index_lock_release_failed", extra={"error": type(exc).__name__})


async def _await_without_cancelling(worker: Future):
    """Await a concurrent future without cancelling it when the awaiter is cancelled.

    ``asyncio.wrap_future`` cancels a not-yet-started future when the
    awaiting task is cancelled -- right for a view call, wrong for a queued
    lock release, which must run whether or not anybody still waits for it.
    """

    loop = asyncio.get_running_loop()
    done: asyncio.Future = loop.create_future()

    def settle() -> None:
        if done.cancelled():
            return
        if worker.cancelled():
            done.set_exception(RuntimeError("the release was cancelled before it ran"))
        elif (exc := worker.exception()) is not None:
            done.set_exception(exc)
        else:
            done.set_result(worker.result())

    def deliver(_: Future) -> None:
        with suppress(RuntimeError):  # the loop is closed: nobody is waiting
            loop.call_soon_threadsafe(settle)

    worker.add_done_callback(deliver)
    return await done


def _count(summary: SweepSummary, outcome: str) -> None:
    setattr(summary, outcome, getattr(summary, outcome) + 1)
    COG_INDEX_ARTIFACTS.labels(outcome=outcome).inc()


def _with(artifact: CogArtifact, **changes) -> CogArtifact:
    return replace(artifact, **changes)


def _errors(*items: str) -> tuple[str, ...]:
    """Bound and sanitize ``read_errors`` entries.

    Reader errors can embed published content (a conflicting name, a layer
    title), which may carry NUL -- the one character ``jsonb`` refuses -- and
    can be arbitrarily long. Each entry is NUL-escaped and capped so the error
    column can always be stored, whatever the card contained.
    """

    cleaned = []
    for item in items:
        item = item.replace("\x00", "\\x00")
        if len(item) > MAX_ERROR_CHARS:
            item = item[: MAX_ERROR_CHARS - 1] + "…"
        cleaned.append(item)
    return tuple(cleaned)


def _card_unstorable(document: dict) -> str | None:
    """Why this card cannot go into ``jsonb``, or ``None`` when it can.

    Postgres ``jsonb`` refuses ``\\u0000`` anywhere in a string, and the
    catalog refuses to be a blob store. Checked *before* the insert so one
    unstorable card is one ``failed`` row instead of a database error that
    poisons every later sweep.
    """

    if contains_nul(document):
        # Checked on the PARSED values, never by searching the serialized
        # text: json.dumps escapes a literal backslash, so the substring
        # "\\u0000" also appears for the harmless literal backslash-u-0-0-0-0
        # in a doc -- a card discussing NUL must index; only a card
        # *containing* one may not (round-2 codex finding).
        return "card: contains NUL (\\u0000), which jsonb cannot store"
    try:
        dumped = json.dumps(document)
    except (TypeError, ValueError) as exc:
        return f"card: not JSON-serializable ({type(exc).__name__})"
    if len(dumped) > MAX_CARD_BYTES:
        return f"card: {len(dumped)} bytes of JSON exceeds the {MAX_CARD_BYTES}-byte cap"
    return None


def _indexed(base: CogArtifact, card: CogCard) -> CogArtifact:
    document = card.to_dict()
    issue = _card_unstorable(document)
    if issue is not None:
        return _with(base, status=STATUS_FAILED, card=None, read_errors=_errors(issue, *card.errors))
    return _with(
        base,
        status=STATUS_INDEXED,
        card=document,
        read_errors=_errors(*card.errors),
        **card_search_fields(document),
    )


def _bundle_layer(manifest: Manifest, title: str) -> Descriptor | None:
    """The layer carrying ``title``, unless it is really the lockfile.

    The same media-type exclusion ``select_bundle_layers`` applies, enforced
    on the direct fetches too: a descriptor *titled* ``COG.md`` or
    ``pixi.toml`` but carrying the lockfile media type is the one large layer
    the card never needs, whatever its title claims.
    """

    layer = manifest.layer_by_title(title)
    if layer is None or layer.media_type == MEDIA_TYPE_PIXI_LOCK:
        return None
    return layer


def _titles(manifest: Manifest) -> list[str]:
    return [layer.title for layer in manifest.layers if layer.title]


def _layer_summary(manifest: Manifest) -> str:
    titles = _titles(manifest)
    if not titles:
        return f"{len(manifest.layers)} layer(s), none titled"
    shown = ", ".join(titles[:MAX_TITLES_IN_REASON])
    more = len(titles) - MAX_TITLES_IN_REASON
    return f"layers: {shown}" + (f" and {more} more" if more > 0 else "")


def _describe(exc: BaseException) -> str:
    """One failure as ``read_errors``/log text: class name, plus the message only when it is known safe.

    The OCI hierarchy's contract is that messages carry neither URLs nor
    headers, so those messages are diagnostic and kept. Anything else --
    adapter errors whose text may quote a configured URL, unexpected
    exceptions whose text may quote anything -- is recorded by class alone.
    """

    if isinstance(exc, OCIError):
        message = str(exc)
        return f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return type(exc).__name__
