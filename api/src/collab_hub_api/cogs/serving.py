"""Serving pulls through the Hub (issue #179): what may be pulled, and from where.

The ``/v2/`` router (:mod:`..routers.registry`) speaks the OCI Distribution
read API; this module is the part behind it that knows the catalog and the
registry sources. It answers three questions and nothing else:

- :meth:`CogRegistryFront.manifest` -- the manifest a reference names;
- :meth:`CogRegistryFront.blob` -- where a blob can be streamed from, and
  how large it is;
- :meth:`CogRegistryFront.tags` -- a repository's tags.

**Every answer is an exact catalog lookup.** Nothing is scanned, cached or
passed through: each read is a bounded number of catalog queries and at most
one request to the source that holds the content (one more per additional
source holding the same digest, when the first no longer has it).

- **A manifest is pullable iff its digest has a pullable catalog row in that
  repository** -- present, indexed, with a ``cog_id``; what the read API
  lists -- found by ``(repository, digest)``, or by a stored tag that names
  the digest. No page or window stands between a pin and its row.
- **A blob is pullable iff a manifest that is pullable now references it**,
  established from stored data: when the Hub serves a manifest it has just
  verified against its digest, it records that manifest's config and layer
  descriptors (:meth:`~.catalog.CogCatalogStore.record_manifest_blobs`). A
  blob request is then one query joining those rows to the pullable rule. A
  blob no recorded manifest references is unknown, with no request to any
  registry; removing a version takes its blobs with it at once, unless
  another pullable manifest references them.

  The record is written on the first manifest read, which every OCI client
  makes before it asks for a blob, for every source holding that digest, and
  it lives in the shared database, so a blob request reaching another replica
  finds it. Later reads of the same manifest write nothing. A client that
  asks for a blob of a manifest nobody has ever pulled through the Hub gets
  ``BLOB_UNKNOWN`` until it (or anyone) reads the manifest.

**Repository names are the backing repository paths**, unchanged, and are
*not* qualified by source: a client addresses ``<hub>/<repository>@<digest>``
and the Hub finds the source holding it. Two sources carrying the same path
are one repository here -- digests are content, so the same digest in both is
the same artifact, served from whichever answers; a tag both carry resolves
to the newest push, as the catalog orders them.

**Tags come from the catalog**, never from a live listing: a tag exists here
exactly when a pullable row carries it, and resolves to that row's digest. A
tag pushed (or moved) since the last sweep is not served until it is indexed.

**Multi-platform indexes are not traversed.** An index whose own digest is a
pullable row is served as the bytes it is; its child manifests are served
only if their digests are pullable rows themselves, and an index contributes
no blobs. Cog bundles are single manifests.

Failures are :class:`ServeError` subclasses whose messages are written for
the client: they name the Hub repository and the digest and never a backing
host, URL or credential. Upstream response bodies are never read into them.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.concurrency import run_in_threadpool

from .catalog import DEFAULT_TAGS_PAGE, BlobDescriptor, CogCatalogStore
from .oci import (
    MEDIA_TYPE_OCI_MANIFEST,
    BlobStream,
    Manifest,
    OCIError,
    OCINotFound,
    is_index_manifest,
    is_sha256_digest,
    is_tag,
)
from .registry import RegistrySource, is_repository_path
from .registry_credentials import RegistryCredentialStore

if TYPE_CHECKING:
    from .publishing import CogPublisher

logger = logging.getLogger("frames_server.cogs.serving")


class ServeError(Exception):
    """Base for every refusal this module reports. The message is safe to send to a client."""


class RepositoryUnknown(ServeError):
    """The catalog holds no pullable artifact under this repository path."""


class ManifestUnknown(ServeError):
    """The reference names nothing pullable in this repository."""


class BlobUnknown(ServeError):
    """No pullable manifest of this repository is known to reference this blob."""


class BlobTooLarge(ServeError):
    """The blob is larger than ``cogs.serve.max_blob_bytes``."""


class UpstreamUnavailable(ServeError):
    """The source holding the artifact could not serve it right now."""


@dataclass(frozen=True)
class ServedManifest:
    digest: str
    media_type: str
    body: bytes


class ServedBlob:
    """An open, not-yet-read blob. Whoever holds one owns it: drain :attr:`chunks`, then :meth:`aclose`.

    :meth:`aclose` closes the upstream response directly as well as the
    chunk iterator, because closing a generator that never started runs none
    of its cleanup -- and a response abandoned before its first chunk is
    exactly when that matters.
    """

    def __init__(self, stream: BlobStream, size: int, *, max_bytes: int) -> None:
        self._stream = stream
        self.digest = stream.digest
        self.size = size
        # The configured cap is enforced on the bytes themselves, whatever a
        # descriptor said: ``size`` is a publisher's claim until the stream
        # has been counted and hashed.
        self.chunks: AsyncGenerator[bytes] = stream.iter_verified(max_bytes=min(size, max_bytes), expected_size=size)

    @property
    def closed(self) -> bool:
        return self._stream.closed

    async def aclose(self) -> None:
        try:
            await self.chunks.aclose()
        finally:
            await self._stream.aclose()


def manifest_blobs(manifest: Manifest) -> list[BlobDescriptor]:
    """The config and layer descriptors of an image manifest (none for an index)."""

    descriptors = ([manifest.config] if manifest.config is not None else []) + list(manifest.layers)
    return [
        BlobDescriptor(digest=descriptor.digest, size=descriptor.size, media_type=descriptor.media_type)
        for descriptor in descriptors
    ]


class CogRegistryFront:
    """The catalog and the sources, as one read-only registry. One per app; holds no per-request state."""

    def __init__(
        self,
        store: CogCatalogStore,
        sources: Sequence[RegistrySource],
        *,
        max_blob_bytes: int,
    ) -> None:
        self._store = store
        self._sources = {source.id: source for source in sources}
        # What every lookup is scoped to: a row whose source this process is
        # not configured with cannot be served, and must not use up a lookup's
        # candidates either.
        self._source_ids = tuple(self._sources)
        self._max_blob_bytes = max_blob_bytes

    @property
    def sources(self) -> list[RegistrySource]:
        return list(self._sources.values())

    # -- the three reads ------------------------------------------------------

    async def tags(self, repository: str, *, after: str | None = None, limit: int = DEFAULT_TAGS_PAGE) -> list[str]:
        """One page of tags after ``after``; ask for one more than the page to learn whether another follows."""

        self._check_name(repository)
        tags = await run_in_threadpool(
            lambda: self._store.list_pullable_tags(repository, self._source_ids, after=after, limit=limit)
        )
        if not tags:
            # Untagged artifacts make a repository with no tags; no artifacts make no repository.
            await self._require_repository(repository)
        return tags

    async def manifest(self, repository: str, reference: str) -> ServedManifest:
        self._check_name(repository)
        unknown = ManifestUnknown(f"manifest {reference} is not known to {repository}")
        digest = reference if is_sha256_digest(reference) else None
        if digest is None and is_tag(reference):
            # A tag names the newest pullable row carrying it, and nothing
            # more: the digest it resolves to is then looked up like any other.
            named = await run_in_threadpool(
                lambda: self._store.find_pullable(repository, self._source_ids, tag=reference)
            )
            digest = named[0].digest if named else None
        # Every source holding that digest; the next is tried only when one no longer has it.
        rows = (
            await run_in_threadpool(lambda: self._store.find_pullable(repository, self._source_ids, digest=digest))
            if digest is not None
            else []
        )
        if not rows:
            await self._require_repository(repository)
            raise unknown
        unavailable = False
        for row in rows:
            try:
                manifest = await self._sources[row.source_id].oci().fetch_manifest(repository, digest)
            except OCINotFound as exc:
                self._log_upstream(row.source_id, repository, exc)
                continue
            except OCIError as exc:
                self._log_upstream(row.source_id, repository, exc)
                unavailable = True
                continue
            # Verified against its digest a moment ago: what it references is
            # recorded for the blob requests that follow. For every source
            # holding the digest, not only the one that answered -- the same
            # digest is the same manifest, and a blob request can fall back
            # to another source only if that source has the descriptors too.
            # Written once: a digest's descriptors never change, so a read of
            # a manifest already recorded (a client's HEAD, then its GET, then
            # everybody else's) costs no write at all.
            pending = [candidate.source_id for candidate in rows if not candidate.blobs_recorded]
            if pending and not is_index_manifest(manifest):
                await run_in_threadpool(self._record_blobs, pending, repository, digest, manifest_blobs(manifest))
            return ServedManifest(
                digest=digest, media_type=manifest.media_type or MEDIA_TYPE_OCI_MANIFEST, body=manifest.raw
            )
        if unavailable:
            raise UpstreamUnavailable(f"manifest {reference} of {repository} is temporarily unavailable")
        raise unknown

    async def blob_size(self, repository: str, digest: str) -> int:
        """The size of a pullable blob, from the manifest that references it (no registry round trip)."""

        return (await self._locate_blob(repository, digest))[0].size

    async def blob(self, repository: str, digest: str) -> ServedBlob:
        unavailable = False
        for located in await self._locate_blob(repository, digest):
            try:
                stream = await self._sources[located.source_id].oci().open_blob(repository, digest)
            except OCINotFound as exc:
                self._log_upstream(located.source_id, repository, exc)
                continue
            except OCIError as exc:
                self._log_upstream(located.source_id, repository, exc)
                unavailable = True
                continue
            declared = stream.content_length
            if declared is not None and declared != located.size:
                # The source is about to send something other than what the
                # manifest promised; refuse before a single byte is relayed.
                await stream.aclose()
                logger.warning(
                    "cog_serve_blob_size_mismatch", extra={"source_id": located.source_id, "digest": digest}
                )
                unavailable = True
                continue
            return ServedBlob(stream, located.size, max_bytes=self._max_blob_bytes)
        if unavailable:
            raise UpstreamUnavailable(f"blob {digest} of {repository} is temporarily unavailable")
        raise BlobUnknown(f"blob {digest} is not known to {repository}")

    # -- lookups --------------------------------------------------------------

    def _record_blobs(
        self, source_ids: Sequence[str], repository: str, digest: str, blobs: Sequence[BlobDescriptor]
    ) -> None:
        for source_id in source_ids:
            self._store.record_manifest_blobs(source_id, repository, digest, blobs)

    def _check_name(self, repository: str) -> None:
        if not is_repository_path(repository):
            raise RepositoryUnknown(f"repository {repository} is not known to this registry")

    async def _require_repository(self, repository: str) -> None:
        if not await run_in_threadpool(self._store.has_pullable, repository, self._source_ids):
            raise RepositoryUnknown(f"repository {repository} is not known to this registry")

    async def _locate_blob(self, repository: str, digest: str):
        self._check_name(repository)
        unknown = BlobUnknown(f"blob {digest} is not known to {repository}")
        if not is_sha256_digest(digest):
            await self._require_repository(repository)
            raise unknown
        located = await run_in_threadpool(self._store.find_blob, repository, digest, self._source_ids)
        if not located:
            await self._require_repository(repository)
            raise unknown
        if len({candidate.size for candidate in located}) > 1:
            # One digest is one content and one size, so manifests that
            # disagree mean a descriptor is wrong. Nothing is decided here:
            # each candidate is held to its own recorded size and to the
            # digest as its bytes are counted, and a wrong one fails there.
            logger.warning("cog_serve_blob_size_disagreement", extra={"repository": repository, "digest": digest})
        # The limit applies to every candidate, not only the first: falling
        # back to another source must not be a way past it.
        within = [candidate for candidate in located if candidate.size <= self._max_blob_bytes]
        if not within:
            raise BlobTooLarge(
                f"blob {digest} of {repository} is {min(candidate.size for candidate in located)} bytes, "
                f"over this registry's {self._max_blob_bytes}-byte limit"
            )
        return within

    def _log_upstream(self, source_id: str, repository: str, exc: OCIError) -> None:
        # The class and the source id only. OCIError messages carry request
        # paths, never hosts; even so the operator's handle on "which
        # registry" is the source id, and that is what is logged.
        logger.warning(
            "cog_serve_upstream_failed",
            extra={"source_id": source_id, "repository": repository, "error": type(exc).__name__},
        )


@dataclass(frozen=True)
class CogRegistryServing:
    """Everything the ``/v2/`` router and the credential exchange need, built once per app."""

    front: CogRegistryFront
    credentials: RegistryCredentialStore
    host: str
    """``host[:port]`` clients address the Hub registry by; the ``registry`` of an exchanged credential."""
    token_url: str
    """The bearer realm: ``<public url>/v2/token``."""
    credential_ttl_seconds: int
    token_ttl_seconds: int
    max_blob_seconds: float
    publisher: CogPublisher | None = None
    """Pushes through the Hub (issue #180); ``None`` unless a source is marked ``publish: true``."""
    max_metadata_seconds: float = 30.0
    """Deadline on every read that is not a blob body: manifests, tags, and ``HEAD`` of a blob."""
