"""Publishing through the Hub (issue #180): pushes written through to one source.

The ``/v2/`` router speaks the push half of the OCI Distribution API; this
module is what stands behind it. A push is accepted from a client that holds
nothing but a Hub sign-in, checked, and written to the one registry source
marked ``publish: true`` with the Hub's own credential for that source.

**Who may push.** Three things, all checked on *every* push request and
before a byte is forwarded (:meth:`CogPublisher.authorize`):

1. the caller holds the **publish permission** (:class:`PublishPolicy`): a
   role or a user id the deployment's configuration names. Nobody by
   default, and being able to pull never implies it;
2. the repository is one the caller's **organization owns**, or a new one. A
   repository belongs to the organization that first publishes to it
   through the Hub, from *before* its first manifest is forwarded (see
   below); later pushes need membership of that organization. A platform
   operator may push to any that is settled;
3. a repository the catalog already knows but that was **not** published
   through the Hub (it has no owner record) accepts pushes from platform
   operators only.

**Uploads** are sessions (:mod:`.publish_store`): the client sees the Hub's
own session id, the backing registry's session URL stays in the database,
and bytes are streamed through -- counted against ``max_blob_bytes``, never
buffered. The backing registry verifies each blob against its digest when
the upload is closed. Three rules keep the two sides in step:

- the Hub's slot is taken **before** the registry is asked to open its
  session, so the per-user cap holds before anything exists upstream, and
  is held by its opener until the registry's session is attached to it;
- a session the Hub lets go of -- past the cap, expired, cancelled, dead --
  is cancelled at the registry **before** its record is deleted, a few per
  request, and a record whose cancellation failed is kept until it succeeds;
- one request at a time may write to a session: each takes the session's
  **lease** before it forwards anything, and gives it back only when it
  knows what the registry took. After any write whose outcome is not known
  -- a timeout, a lost response, a failure to record it -- the session is
  **retired**: the Hub's byte count can no longer be trusted, so the client
  is told the upload is unknown and starts again.

**A manifest is validated before it is committed**
(:meth:`CogPublisher.put_manifest`). The blobs it names were just uploaded,
so the Hub reads the bundle with the catalog's own reader, through the
indexer's code path, *before* forwarding the manifest. A bundle the catalog
would not list -- not a Cog, no id, or a reader error -- is refused with the
reader's errors, nothing is written to the registry and nothing is listed.

**The name is its publisher's organization's before the manifest is
forwarded, and nothing is listed until the registry has accepted it.** The
Hub first writes the repository's row, *pending* and owned by the
publisher's organization, and a record of the attempt. The registry's
answer then decides: on acceptance the row is committed, the attempt is
marked accepted and the catalog row is written; on a definite refusal the
attempt is dropped and, if it was the organization's last one in flight,
so is the pending row. When the outcome is unknown nothing is withdrawn:
the registry may hold the manifest, so the name stays that organization's
-- no other organization can take it, only the same one can try again, and
after a grace period sweeps look there and settle it if they find content.
Only an attempt known to be accepted ever says who published a digest.

**An accepted manifest is indexed in the request**: the row the validation
produced is stored through the indexer's lock-less targeted path, in one
transaction with its tag assignment and the authenticated publisher (Hub
user and organization), under the request's own deadline. If that write
fails, the answer says so -- the manifest is stored, and not listed -- rather
than reporting a publish that the catalog does not show. A later sweep
reconciles the row like any other and leaves the publisher alone.

Failures are :class:`PublishError` subclasses whose messages are written for
the client and name no backing host, URL or credential.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import anyio
from starlette.concurrency import run_in_threadpool

from ..frames.orgs import PLATFORM_ROLE_OPERATOR
from .catalog import STATUS_INDEXED, CogCatalogStore, new_attempt_id
from .deadline import renew as renew_budget
from .indexer import CogIndexer
from .oci import (
    Descriptor,
    Manifest,
    OCIAuthError,
    OCIError,
    OCIProtocolError,
    OCIRejected,
    OCIThrottled,
    OCITransportError,
    _parse_manifest,
    is_index_manifest,
    is_sha256_digest,
    is_tag,
)
from .publish_store import PendingLimitError, PublishStore, UploadLimitError, UploadSession, new_upload_id
from .registry import ArtifactRef, RegistrySource, is_repository_path
from .serving import manifest_blobs

logger = logging.getLogger("frames_server.cogs.publishing")

PUBLISH_ROLES = ("operator", "owner", "member")
"""The roles ``cogs.publish.allowed_roles`` may name: the platform role, then the two organization roles."""

LEASE_MARGIN_SECONDS = 60.0
"""A session's lease outlasts the longest request that can hold it (``max_blob_seconds``) by this much."""

STALE_UPLOADS_PER_REQUEST = 4
STALE_CLEANUP_SECONDS = 5.0
STALE_LEASE_SECONDS = 60.0
"""Cleaning up after sessions the Hub let go of: how many per request, within how long, each claimed how long."""


class PublishError(Exception):
    """Base for every refusal this module reports. The message is safe to send to a client."""


class PublishDenied(PublishError):
    """The caller may not push here: no publish permission, or the repository is not theirs."""


class RepositoryInvalid(PublishError):
    """Not a repository name."""


class UploadUnknown(PublishError):
    """No such upload session for this caller and repository (or it expired)."""


class UploadInvalid(PublishError):
    """The upload request does not fit the session: a chunk out of order, a range that disagrees, a session in use."""

    def __init__(self, message: str, *, received: int | None = None) -> None:
        super().__init__(message)
        self.received = received


class UploadTooLarge(PublishError):
    """The blob is larger than ``cogs.serve.max_blob_bytes``."""


class DigestInvalid(PublishError):
    """The digest is malformed, or the bytes do not hash to it."""


class ManifestInvalid(PublishError):
    """The manifest was refused; ``errors`` says why, one entry per problem."""

    def __init__(self, message: str, errors: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.errors = errors or (message,)


class PublishLimited(PublishError):
    """A bound was reached: unfinished uploads of this user's, or pending repositories of this organization's."""


UploadLimited = PublishLimited


class PublishUnavailable(PublishError):
    """The publish source could not take the write right now."""


class ManifestUnlisted(PublishError):
    """The registry accepted the manifest, and the catalog does not list it.

    ``retryable`` says whether putting the manifest again can help: it can
    when the write could not be made, and cannot when the catalog refused
    the row itself.
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable

    @classmethod
    def not_yet(cls) -> ManifestUnlisted:
        return cls(
            "the manifest was stored in the registry but could not be listed in the catalog yet; "
            "put it again, or wait for the catalog to index it",
            retryable=True,
        )


manifest_accepted: ContextVar[bool] = ContextVar("cog_publish_manifest_accepted", default=False)
"""Whether this request's manifest has been accepted by the registry.

Set by :meth:`CogPublisher.put_manifest` the moment the registry says yes,
for the one failure that method cannot report itself: the request running
out of time during the bookkeeping that follows. The router reads it, so
the answer can still say the manifest is stored.
"""


@dataclass(frozen=True)
class PublishPolicy:
    """Who holds the publish permission. Empty -- the default -- means nobody."""

    allowed_roles: frozenset[str] = frozenset()
    allowed_users: frozenset[str] = frozenset()

    def permits(self, user_id: str, org_role: str | None, platform_role: str | None) -> bool:
        if user_id in self.allowed_users:
            return True
        if platform_role == PLATFORM_ROLE_OPERATOR and "operator" in self.allowed_roles:
            return True
        return org_role in ("owner", "member") and org_role in self.allowed_roles


@dataclass(frozen=True)
class Publisher:
    """The caller of a push, as the Hub resolves them **now**: never as a token remembered them."""

    user_id: str
    org_id: str | None
    org_role: str | None = None
    platform_role: str | None = None

    @property
    def is_operator(self) -> bool:
        return self.platform_role == PLATFORM_ROLE_OPERATOR


class _ManifestInHand:
    """The registry client, answering for one manifest that has not been pushed yet.

    The reader asks its client for the manifest and then for the layers it
    needs. During validation the manifest exists only in this request; the
    layers were just uploaded and are read from the registry, verified
    against their digests like any other read.
    """

    def __init__(self, client, manifest: Manifest) -> None:
        self._client = client
        self._manifest = manifest
        self.unavailable = False

    async def get_manifest(self, repo: str, ref: str) -> Manifest:
        return self._manifest

    async def get_blob(self, repo: str, descriptor: Descriptor | str, *, max_bytes: int) -> bytes:
        try:
            return await self._client.get_blob(repo, descriptor, max_bytes=max_bytes)
        except (OCITransportError, OCIAuthError):
            # The registry could not be asked. That is an outage, not a
            # verdict on the bundle, and must not be reported as one.
            self.unavailable = True
            raise


class _SourceInHand:
    def __init__(self, source: RegistrySource, client: _ManifestInHand) -> None:
        self.id = source.id
        self.host = source.host
        self._client = client

    def oci(self) -> _ManifestInHand:
        return self._client


class CogPublisher:
    """Pushes, written through to the publish source. One per app; holds no per-request state."""

    def __init__(
        self,
        *,
        catalog: CogCatalogStore,
        store: PublishStore,
        source: RegistrySource,
        indexer: CogIndexer,
        policy: PublishPolicy,
        max_blob_bytes: int,
        max_blob_seconds: float = 900.0,
        max_pending_repositories: int = 20,
    ) -> None:
        self._catalog = catalog
        self._store = store
        self._source = source
        self._indexer = indexer
        self.policy = policy
        self._max_blob_bytes = max_blob_bytes
        self._lease_seconds = max_blob_seconds + LEASE_MARGIN_SECONDS
        self._max_pending = max_pending_repositories

    @property
    def source_id(self) -> str:
        return self._source.id

    def close(self) -> None:
        self._indexer.close()

    # -- authorization ---------------------------------------------------------

    def authorize(self, publisher: Publisher, repository: str) -> None:
        """Refuse a push this caller may not make to this repository. Blocking: two store reads at most.

        Run on every push request, before anything is sent to the registry.
        """

        if not is_repository_path(repository):
            raise RepositoryInvalid(f"{repository} is not a repository name")
        if not self.policy.permits(publisher.user_id, publisher.org_role, publisher.platform_role):
            raise PublishDenied("this account does not hold the permission to publish Cogs")
        record = self._store.get_repository(repository)
        if record is not None:
            self._require_owner(publisher, record.owner_org_id, repository)
        # Content the catalog has indexed in a repository nobody has been
        # confirmed to own -- no row, or a row still pending, which a sweep
        # would have committed had the content been its organization's --
        # was not published through the Hub.
        if (record is None or not record.committed) and not publisher.is_operator:
            if self._catalog.repository_known(repository):
                raise self._out_of_band(repository)

    def _out_of_band(self, repository: str) -> PublishDenied:
        return PublishDenied(
            f"{repository} already exists and was not published through this Hub; "
            "only a platform operator may publish to it"
        )

    async def _require_new(self, publisher: Publisher, repository: str) -> None:
        """Before a name is given to anyone: refuse one the registry already holds content under.

        The catalog only knows what a sweep has indexed. A repository pushed
        to the registry directly a moment ago is not in it yet, and granting
        its name here would hand somebody else's content to the publisher's
        organization. So the registry is asked -- one request -- and an
        answer that is not a clear "nothing there" fails closed: nothing is
        reserved and nothing is forwarded.
        """

        if publisher.is_operator:
            return
        try:
            holds_content = await self._source.oci().has_tags(repository)
        except OCIError as exc:
            self._log_upstream("repository check", exc)
            raise PublishUnavailable(
                "the registry could not be asked whether this repository exists; try again"
            ) from None
        if holds_content:
            raise self._out_of_band(repository)

    async def _require_unowned_new(self, publisher: Publisher, repository: str) -> None:
        """For a name nobody owns yet: refuse it if the registry already holds content there.

        :meth:`authorize` knows only what the catalog has indexed, so a
        repository pushed to the registry directly passes it. This is the
        registry's answer, asked before anything reaches the repository --
        an upload opening, a blob check, a manifest's validation -- and not
        only when the name is reserved: otherwise blobs could be written
        into, and digests probed in, another team's repository. Later writes
        to an upload go through the session it opened, so are covered by
        this check at its opening.
        """

        if publisher.is_operator:
            return
        if await run_in_threadpool(self._store.get_repository, repository) is None:
            await self._require_new(publisher, repository)

    def _require_owner(self, publisher: Publisher, owner_org_id: str | None, repository: str) -> None:
        if publisher.is_operator:
            return
        if publisher.org_id is None or owner_org_id != publisher.org_id:
            raise PublishDenied(f"{repository} belongs to another organization")

    # -- blobs -----------------------------------------------------------------

    async def blob_size(self, publisher: Publisher, repository: str, digest: str) -> int | None:
        """The size of a blob the publish source already holds in this repository, or ``None``.

        For a caller already authorized to push here: lets a client skip an
        upload it does not need. It makes nothing pullable. A name the
        caller could not publish to answers ``None``, like a blob that is
        not there: it says nothing about what another repository holds.
        """

        if not is_sha256_digest(digest):
            return None
        try:
            await self._require_unowned_new(publisher, repository)
        except PublishError:
            return None
        try:
            return await self._source.oci().blob_size(repository, digest)
        except OCIError as exc:
            self._log_upstream("blob check", exc)
            return None

    async def start(self, publisher: Publisher, repository: str) -> UploadSession:
        """Open an upload: the Hub's slot first, then the registry's session, then the two are tied together."""

        await self._require_unowned_new(publisher, repository)
        session = await self._open(publisher, repository)
        try:
            location = await self._source.oci().start_upload(repository)
        except BaseException as exc:
            # Nothing was opened at the registry (or nothing the Hub was told
            # of): the slot is given back, whatever ended the attempt.
            await self._settle(self._store.close_upload, session.id)
            if isinstance(exc, OCIError):
                raise self._unavailable("upload start", exc) from None
            raise
        # The slot is still this request's: its lease has kept the cap and
        # the cleanup away from it while the registry was opening.
        try:
            attached = await run_in_threadpool(
                lambda: self._store.attach_upload(session.id, lease=session.lease, upstream_location=location)
            )
        except BaseException:
            await self._drop_unattached(publisher, repository, location)
            raise
        if not attached:
            await self._drop_unattached(publisher, repository, location)
            raise PublishUnavailable("the upload could not be opened; try again")
        await self._retire_stale()
        return replace(session, upstream_location=location, lease=None)

    async def _drop_unattached(self, publisher: Publisher, repository: str, location: str) -> None:
        """The registry opened a session the Hub has no slot for: cancel it there, or remember where it is."""

        with anyio.CancelScope(shield=True):
            if await self._source.oci().cancel_upload(repository, location):
                return
        # Not known to be gone. Its location is all that can ever cancel it,
        # so it is kept, as a dead session for the cleanup to retry.
        await self._settle(
            lambda: self._store.record_orphan(
                upload_id=new_upload_id(),
                user_id=publisher.user_id,
                repository=repository,
                source_id=self._source.id,
                upstream_location=location,
            )
        )

    async def _open(self, publisher: Publisher, repository: str) -> UploadSession:
        def open_slot() -> UploadSession:
            return self._store.open_upload(
                upload_id=new_upload_id(),
                user_id=publisher.user_id,
                repository=repository,
                source_id=self._source.id,
                lease_seconds=self._lease_seconds,
            )

        try:
            return await run_in_threadpool(open_slot)
        except UploadLimitError:
            pass
        # Every slot this caller has is taken by sessions still waiting to be
        # cancelled at the registry. Work on those, then ask once more.
        await self._retire_stale(user_id=publisher.user_id)
        try:
            return await run_in_threadpool(open_slot)
        except UploadLimitError:
            raise UploadLimited("too many uploads are open or still being cleaned up; try again shortly") from None

    async def _retire_stale(self, *, user_id: str | None = None) -> None:
        """Cancel, at the registry, a few sessions the Hub has let go of; then forget them. Best effort, bounded.

        Dead sessions -- timed out, retired by the per-user cap, or left by a
        write whose outcome nobody recorded -- are claimed a few at a time. Each is cancelled at the registry
        *first* and its record deleted only once the registry says the
        session is gone; one that could not be cancelled keeps its record and
        is claimed again later. Nothing here fails the request that runs it.
        """

        try:
            async with asyncio.timeout(STALE_CLEANUP_SECONDS):
                stale = await run_in_threadpool(
                    lambda: self._store.claim_stale_uploads(
                        limit=STALE_UPLOADS_PER_REQUEST, lease_seconds=STALE_LEASE_SECONDS, user_id=user_id
                    )
                )
                for session in stale:
                    if session.upstream_location is not None and not await self._source.oci().cancel_upload(
                        session.repository, session.upstream_location
                    ):
                        logger.warning("cog_publish_stale_upload_kept", extra={"source_id": self._source.id})
                        continue
                    await run_in_threadpool(self._store.close_upload, session.id)
        except Exception as exc:
            logger.warning(
                "cog_publish_stale_cleanup_failed", extra={"source_id": self._source.id, "error": type(exc).__name__}
            )

    async def status(self, publisher: Publisher, repository: str, upload_id: str) -> UploadSession:
        session = await run_in_threadpool(
            lambda: self._store.get_upload(upload_id, user_id=publisher.user_id, repository=repository)
        )
        if session is None:
            raise UploadUnknown("blob upload unknown to registry")
        return session

    @asynccontextmanager
    async def _leased(self, publisher: Publisher, repository: str, upload_id: str) -> AsyncIterator[_Held]:
        """The session, held by this request alone until the block ends.

        Taken before anything is sent to the registry's session, so of two
        requests for one upload -- on any two replicas -- one forwards and
        the other is told to retry. Only the row is held, never a database
        connection.

        How the block ends decides what becomes of the session. Bookkeeping
        that recorded the outcome has already let the lease go. A block that
        ends knowing the registry took nothing gives the lease back,
        unchanged. A block that ends *not knowing* -- ``held.uncertain``,
        set before anything is forwarded and cleared only on a definite
        answer -- retires the session: its byte count may be wrong, and a
        later write must not build on it. If even that cannot be recorded,
        the lease simply runs out, which the store reads the same way.
        """

        session = await run_in_threadpool(
            lambda: self._store.lease_upload(
                upload_id, user_id=publisher.user_id, repository=repository, lease_seconds=self._lease_seconds
            )
        )
        if session is None:
            # Not there, or in use: the status read says which.
            await self.status(publisher, repository, upload_id)
            raise UploadInvalid("another request is writing to this upload; retry when it has finished")
        held = _Held(session)
        try:
            yield held
        finally:
            if held.leased and held.uncertain:
                await self._retire(held)
            elif held.leased:
                await self._settle(lambda: self._store.release_upload(upload_id, lease=session.lease))

    async def _retire(self, held: _Held) -> None:
        """End a session after a write whose outcome is unknown. Never raises."""

        logger.warning("cog_publish_upload_retired", extra={"source_id": self._source.id})
        with anyio.CancelScope(shield=True):
            try:
                await self._abandon(held)
            except Exception as exc:
                logger.warning(
                    "cog_publish_bookkeeping_failed", extra={"source_id": self._source.id, "error": type(exc).__name__}
                )

    async def _settle(self, call, /, *args) -> None:
        """Run a piece of session bookkeeping that must happen however the request is ending. Never raises."""

        with anyio.CancelScope(shield=True):
            renew_budget()
            try:
                await run_in_threadpool(call, *args)
            except Exception as exc:
                logger.warning(
                    "cog_publish_bookkeeping_failed", extra={"source_id": self._source.id, "error": type(exc).__name__}
                )

    async def append(
        self,
        publisher: Publisher,
        repository: str,
        upload_id: str,
        content: AsyncIterable[bytes],
        *,
        content_range: tuple[int, int] | None,
        length: int | None,
    ) -> UploadSession:
        """Forward the next chunk. ``content_range`` is the ``(first, last)`` byte the client claims, if it said."""

        async with self._leased(publisher, repository, upload_id) as held:
            session = held.session
            span = self._check_range(session, content_range, length)
            self._check_size(session.received + (length or 0))
            counter = _Counted(content, limit=self._max_blob_bytes - session.received, expect=span)
            held.uncertain = True
            try:
                location = await self._source.oci().upload_chunk(
                    repository, session.upstream_location, counter.chunks(), offset=session.received, length=length
                )
            except _LimitExceeded:
                await self._abandon(held)
                raise UploadTooLarge(self._too_large()) from None
            except _LengthMismatch:
                await self._abandon(held)
                raise UploadInvalid(_RANGE_LENGTH) from None
            except OCIThrottled as exc:
                # Try again later, not the client's fault. After a 429 the
                # registry took nothing and the session is given back as it
                # was, for the same chunk to be sent again; after a 408 what
                # it took is not known, and the session is retired.
                if exc.status == 429:
                    held.uncertain = False
                raise self._unavailable("upload chunk", exc) from None
            except OCIRejected as exc:
                # A definite no: the registry took nothing.
                if exc.status == 404:
                    await self._abandon(held, cancel=False)
                elif exc.status == 413:
                    # Over the registry's own limit: no chunk will make it fit.
                    await self._abandon(held)
                    raise UploadTooLarge(_TOO_LARGE_UPSTREAM) from None
                held.uncertain = False
                raise self._write_failed("upload chunk", exc, session) from None
            except OCIAuthError as exc:
                held.uncertain = False
                raise self._unavailable("upload chunk", exc) from None
            except OCIError as exc:
                # Sent, and no usable answer: it may have taken all of it, some, or none.
                raise self._unavailable("upload chunk", exc) from None
            renew_budget()
            received = session.received + counter.count
            moved = await run_in_threadpool(
                lambda: self._store.advance_upload(
                    upload_id,
                    lease=session.lease,
                    expected_received=session.received,
                    received=received,
                    upstream_location=location,
                )
            )
            if not moved:
                # The session expired, or this request held it past its
                # lease: the registry has bytes the record does not account
                # for, and nothing can be trusted about its position.
                await self._abandon(held)
                raise UploadInvalid("the upload expired while it was being written to and has been cancelled")
            held.leased = False
            return replace(session, received=received, upstream_location=location)

    async def finish(
        self,
        publisher: Publisher,
        repository: str,
        upload_id: str,
        digest: str,
        content: AsyncIterable[bytes] | None,
        *,
        length: int | None,
        content_range: tuple[int, int] | None = None,
    ) -> None:
        """Close an upload as ``digest`` (optionally with its last bytes); the registry verifies the digest."""

        if not is_sha256_digest(digest):
            raise DigestInvalid("the digest must be sha256: followed by 64 lowercase hex digits")
        async with self._leased(publisher, repository, upload_id) as held:
            session = held.session
            span = self._check_range(session, content_range, length if content is not None else 0)
            self._check_size(session.received + (length or 0))
            counter = (
                _Counted(content, limit=self._max_blob_bytes - session.received, expect=span)
                if content is not None
                else None
            )
            held.uncertain = True
            try:
                await self._source.oci().finish_upload(
                    repository,
                    session.upstream_location,
                    digest,
                    counter.chunks() if counter is not None else None,
                    length=length,
                )
            except _LimitExceeded:
                await self._abandon(held)
                raise UploadTooLarge(self._too_large()) from None
            except _LengthMismatch:
                await self._abandon(held)
                raise UploadInvalid(_RANGE_LENGTH) from None
            except OCIThrottled as exc:
                # Try again later, not the client's fault. After a 429 the
                # registry took nothing: the session is given back, usable,
                # and the client closes it again. After a 408 it is retired.
                if exc.status == 429:
                    held.uncertain = False
                raise self._unavailable("upload finish", exc) from None
            except OCIRejected as exc:
                await self._abandon(held, cancel=exc.status != 404)
                if exc.status == 404:
                    raise UploadUnknown("blob upload unknown to registry") from None
                if exc.status == 413:
                    raise UploadTooLarge(_TOO_LARGE_UPSTREAM) from None
                if exc.status == 400:
                    # The registry checked the bytes against the digest and they did not match.
                    raise DigestInvalid("the uploaded content does not match the digest") from None
                raise UploadInvalid("the registry refused to complete the upload") from None
            except OCIAuthError as exc:
                held.uncertain = False
                raise self._unavailable("upload finish", exc) from None
            except OCIError as exc:
                raise self._unavailable("upload finish", exc) from None
            renew_budget()
            await run_in_threadpool(self._store.close_upload, upload_id)
            held.leased = False

    async def cancel(self, publisher: Publisher, repository: str, upload_id: str) -> None:
        async with self._leased(publisher, repository, upload_id) as held:
            await self._abandon(held)

    async def _abandon(self, held: _Held, *, cancel: bool = True) -> None:
        """End a session this request holds: cancelled at the registry first, and only then forgotten.

        From its first line the session's fate is unknown until the record
        says otherwise: if the cancellation is cut off, or what it did cannot
        be recorded, the hold must not be given back as if nothing had
        happened (see :meth:`_leased`).
        """

        held.uncertain = True
        renew_budget()
        session = held.session
        gone = True
        if cancel:
            gone = await self._source.oci().cancel_upload(session.repository, session.upstream_location)
        # A session the registry may still hold keeps its record, expired,
        # for the cleanup to try again; either way it is no longer writable.
        await run_in_threadpool(self._store.close_upload if gone else self._store.retire_upload, session.id)
        held.leased = False

    def _check_range(self, session: UploadSession, content_range: tuple[int, int] | None, length: int | None):
        """The number of bytes a ``Content-Range`` promises, once it has been checked; ``None`` without one."""

        if content_range is None:
            return None
        first, last = content_range
        if last < first:
            raise UploadInvalid("the Content-Range ends before it starts")
        if first != session.received:
            raise UploadInvalid("the chunk does not start where the upload left off", received=session.received)
        span = last - first + 1
        if length is not None and length != span:
            raise UploadInvalid(_RANGE_LENGTH)
        return span

    def _check_size(self, total: int) -> None:
        if total > self._max_blob_bytes:
            raise UploadTooLarge(self._too_large())

    def _too_large(self) -> str:
        return f"the blob is over this registry's {self._max_blob_bytes}-byte limit"

    def _write_failed(self, what: str, exc: OCIRejected, session: UploadSession) -> PublishError:
        if exc.status == 404:
            return UploadUnknown("blob upload unknown to registry")
        return UploadInvalid("the registry refused the chunk", received=session.received)

    # -- manifests -------------------------------------------------------------

    async def put_manifest(
        self, publisher: Publisher, repository: str, reference: str, body: bytes, content_type: str
    ) -> str:
        """Validate, commit and index one manifest. Returns its digest."""

        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if is_sha256_digest(reference):
            if reference != digest:
                raise DigestInvalid("the manifest does not hash to the digest it was put under")
            tag = None
        elif is_tag(reference):
            tag = reference
        else:
            raise ManifestInvalid("the reference is neither a tag nor a sha256 digest")
        try:
            manifest = _parse_manifest(body, digest, content_type_fallback=content_type)
        except OCIProtocolError as exc:
            raise ManifestInvalid(str(exc)) from None
        if is_index_manifest(manifest):
            raise ManifestInvalid("multi-platform indexes are not accepted: a Cog bundle is a single manifest")
        if any(layer.size > self._max_blob_bytes for layer in manifest.layers):
            raise ManifestInvalid(f"a layer is over this registry's {self._max_blob_bytes}-byte limit")

        # A name nobody owns yet is checked at the registry first: its
        # content, if it has any, is somebody else's, and validation would
        # read the bundle's blobs from it. It is also the cheaper check.
        await self._require_unowned_new(publisher, repository)
        row = await self._validate(repository, manifest, (tag,) if tag is not None else ())

        # This attempt's id, before anything is recorded: the hold on the
        # name and the record of the attempt are both taken back by it.
        attempt = new_attempt_id()
        # The name is this organization's from here, durably and before the
        # registry can have anything: pending until the registry answers.
        # Of two organizations publishing a new name at once, one holds it
        # and the other is refused here, before the write.
        try:
            record = await self._reserve(publisher, repository, attempt)
        except PendingLimitError:
            raise PublishLimited(
                f"this organization already has {self._max_pending} repositories whose first publish is unresolved, "
                "which is the limit; publish to one of them again, or ask a platform operator to release one"
            ) from None
        if record.committed:
            self._require_owner(publisher, record.owner_org_id, repository)
        elif record.owner_org_id != publisher.org_id:
            raise PublishDenied(f"{repository} is being published by another organization")
        # This attempt, on record before the registry can have the manifest.
        # It says who published the digest only once it is marked accepted.
        try:
            await run_in_threadpool(
                lambda: self._catalog.note_publication(
                    attempt, self._source.id, repository, digest, user_id=publisher.user_id, org_id=publisher.org_id
                )
            )
        except BaseException:
            # Nothing was forwarded: as definite as a refusal.
            await self._withdraw(publisher, repository, attempt)
            raise

        try:
            await self._source.oci().put_manifest(repository, reference, body, manifest.media_type or content_type)
        except OCIThrottled as exc:
            # Try again later. After a 429 the registry stored nothing, as
            # definite as a refusal; after a 408 it may have, and nothing is
            # withdrawn.
            if exc.status == 429:
                await self._withdraw(publisher, repository, attempt)
            raise self._unavailable("manifest put", exc) from None
        except (OCIRejected, OCIAuthError) as exc:
            # A definite no: nothing was stored, so this attempt is withdrawn.
            await self._withdraw(publisher, repository, attempt)
            if isinstance(exc, OCIRejected):
                if exc.status == 413:
                    raise ManifestInvalid("the registry refused the manifest as too large") from None
                raise ManifestInvalid("the registry refused the manifest") from None
            raise self._unavailable("manifest put", exc) from None
        except OCIError as exc:
            # Unknown: the registry may hold the manifest. Nothing is
            # withdrawn -- the name stays this organization's, pending, and
            # the attempt stays unaccepted, attributing nothing.
            raise self._unavailable("manifest put", exc) from None

        # Accepted. The attempt may now say who published the digest, the
        # repository is settled, and the row is written now -- from the row
        # the validation produced -- so the catalog lists it without waiting
        # for a sweep.
        manifest_accepted.set(True)
        try:
            await run_in_threadpool(self._catalog.accept_publication, attempt)
            owner = await run_in_threadpool(
                lambda: self._store.commit_repository(
                    repository, source_id=self._source.id, owner_org_id=publisher.org_id, created_by=publisher.user_id
                )
            )
            self._require_owner(publisher, owner.owner_org_id, repository)
            stored = await self._indexer.record_published(row, tag=tag)
            if stored.status == STATUS_INDEXED:
                await run_in_threadpool(
                    self._catalog.record_manifest_blobs, self._source.id, repository, digest, manifest_blobs(manifest)
                )
        except PublishDenied:
            # Only if an operator released the pending row under this
            # request and another organization then took the name.
            logger.error("cog_publish_ownership_lost", extra={"repository": repository, "digest": digest})
            raise
        except Exception as exc:
            logger.error(
                "cog_publish_unlisted",
                extra={"repository": repository, "digest": digest, "error": type(exc).__name__},
            )
            raise ManifestUnlisted.not_yet() from None
        if stored.status != STATUS_INDEXED:
            logger.error("cog_publish_unlisted", extra={"repository": repository, "digest": digest, "error": "data"})
            raise ManifestUnlisted(
                "the manifest was stored in the registry, but the catalog could not index it and does not list it",
                retryable=False,
            )
        logger.info(
            "cog_published",
            extra={"repository": repository, "digest": digest, "cog_id": row.cog_id, "user": publisher.user_id},
        )
        return digest

    async def _reserve(self, publisher: Publisher, repository: str, attempt: str):
        """Reserve the name for this attempt; if the request ends meanwhile, let the reservation finish, then undo it.

        A thread is not interrupted by the request's deadline: a reservation
        cut off may still land, after the request has given up on it. So the
        call runs as a task of its own, and a cancelled request waits for it
        -- its statements are bounded by the request's budget -- before
        taking it back by this attempt's id. Taken back any earlier, a
        reservation landing late would hold the name with nothing forwarded
        and nothing left to release it. Nothing was forwarded either way.
        """

        worker = asyncio.ensure_future(
            run_in_threadpool(
                lambda: self._store.reserve_repository(
                    repository,
                    source_id=self._source.id,
                    owner_org_id=publisher.org_id,
                    created_by=publisher.user_id,
                    max_pending=self._max_pending,
                    attempt_id=attempt,
                )
            )
        )
        try:
            return await asyncio.shield(worker)
        except PendingLimitError:
            raise
        except BaseException:
            with anyio.CancelScope(shield=True):
                with suppress(BaseException):
                    await worker
                await self._withdraw(publisher, repository, attempt)
            raise

    async def _withdraw(self, publisher: Publisher, repository: str, attempt: str) -> None:
        """Take back what this attempt recorded: its note and its hold on the name. Safe for either if absent."""

        def withdraw() -> None:
            self._catalog.forget_publication(attempt)
            self._store.release_repository(repository, owner_org_id=publisher.org_id, attempt_id=attempt)

        await self._settle(withdraw)

    async def _validate(self, repository: str, manifest: Manifest, tags: tuple[str, ...]):
        """Read the bundle with the catalog's reader; refuse what the catalog would not list."""

        client = _ManifestInHand(self._source.oci(), manifest)
        artifact = ArtifactRef(digest=manifest.digest, tags=tags, pushed_at=datetime.now(UTC))
        row = await self._indexer.inspect(_SourceInHand(self._source, client), repository, artifact)
        if client.unavailable:
            raise PublishUnavailable("the bundle could not be read back for validation; try again")
        errors = tuple(row.read_errors)
        if row.status != STATUS_INDEXED:
            raise ManifestInvalid("the bundle is not a Cog the catalog can index", errors)
        if errors:
            raise ManifestInvalid("the bundle has errors and would not be listed correctly", errors)
        if not row.cog_id:
            raise ManifestInvalid("the bundle declares no id, so the catalog could not list it")
        return row

    # -- failures --------------------------------------------------------------

    def _unavailable(self, what: str, exc: OCIError) -> PublishUnavailable:
        self._log_upstream(what, exc)
        return PublishUnavailable("the registry is temporarily unable to accept this write")

    def _log_upstream(self, what: str, exc: OCIError) -> None:
        # The source id and the error's class: never its message's host, and
        # never a response body (none is read). A refused credential is an
        # operator's problem and is logged as one.
        level = logging.ERROR if isinstance(exc, OCIAuthError) else logging.WARNING
        logger.log(
            level,
            "cog_publish_upstream_failed",
            extra={"source_id": self._source.id, "step": what, "error": type(exc).__name__},
        )


_RANGE_LENGTH = "the Content-Range does not match the number of bytes sent"
_TOO_LARGE_UPSTREAM = "the blob is over the publish registry's own size limit"


@dataclass
class _Held:
    """A session this request holds the lease of; ``leased`` goes false once bookkeeping has let it go."""

    session: UploadSession
    leased: bool = True
    uncertain: bool = False
    """Something was forwarded to the registry session and what it took has not been established."""


def settle_found(store: PublishStore, catalog: CogCatalogStore):
    """What a sweep calls with what it found in a source: settles pending repositories, strictly.

    A pending repository is committed only when the sweep found, in it, a
    digest the registry is **known to have accepted** from its organization
    for that repository: the publish whose acceptance was recorded and whose
    commit was not. Nothing else found there proves whose it is. A digest
    the organization merely *attempted* does not either -- the attempt may
    have stored nothing, and the same bytes can be pushed by anyone -- so
    the row stays pending, the content is indexed like any out-of-band
    content, one line says so, and an operator resolves it.
    """

    def settle(source_id: str, found) -> None:
        for record in store.pending_repositories(source_id):
            digests = set(found.get(record.repository, ()))
            if not digests:
                continue
            accepted = catalog.accepted_digests(source_id, record.repository, record.owner_org_id)
            if digests & accepted:
                store.commit_found(source_id, record.repository, owner_org_id=record.owner_org_id)
            else:
                logger.warning(
                    "cog_publish_pending_repository_holds_other_content",
                    extra={"source_id": source_id, "repository": record.repository},
                )

    return settle


class _LimitExceeded(Exception):
    """Raised inside a request body stream once it has carried more than the limit allows."""


class _LengthMismatch(Exception):
    """Raised inside a request body stream that is not the length its ``Content-Range`` promised."""


class _Counted:
    """A request body, counted as it is forwarded: cut off at ``limit`` bytes, and held to ``expect`` if given."""

    def __init__(self, content: AsyncIterable[bytes], *, limit: int, expect: int | None = None) -> None:
        self._content = content
        self._limit = limit
        self._expect = expect
        self.count = 0

    async def chunks(self) -> AsyncIterator[bytes]:
        async for chunk in self._content:
            self.count += len(chunk)
            if self.count > self._limit:
                raise _LimitExceeded()
            if self._expect is not None and self.count > self._expect:
                raise _LengthMismatch()
            yield chunk
        if self._expect is not None and self.count != self._expect:
            raise _LengthMismatch()
