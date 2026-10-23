"""``static`` registry source: a configured repository list, generic OCI only.

Needs no vendor API, so it works against any OCI registry and doubles as the
indexer's test double. Repositories come from config and/or from an index
document at ``index_url`` shaped like ``catalog.v1.json``::

    {"schemaVersion": 1, "repositories": [{"namespace": "acme", "name": "data-explorer"}]}

Artifacts are enumerated through the generic client alone: ``list_tags`` then
one ``get_manifest`` per tag, grouped by manifest digest. Untagged artifacts
are therefore invisible to this source (the distribution API offers no
portable way to list them); a static source has no webhook either, so
``parse_event`` always answers "not mine".
"""

from __future__ import annotations

import json
import logging

import httpx

from ..oci import BasicCredentials, OCIClient, OCINotFound
from ..registry import (
    CREATED_ANNOTATION,
    ArtifactRef,
    CogRegistrySourceConfig,
    OCIClientFactory,
    RegistryEvent,
    RegistrySourceError,
    RegistrySourceProtocolError,
    WebhookRequest,
    http_verify,
    is_repository_path,
    parse_timestamp,
    registry_host,
)

logger = logging.getLogger(__name__)

INDEX_SCHEMA_VERSION = 1
MAX_INDEX_BYTES = 1024 * 1024
"""An index is a list of names; a document past this is not one."""
MAX_TAGS_PER_REPOSITORY = 200
"""Bound on manifests fetched per repository; a Cog repository has a handful of tags.

Past the bound the adapter **raises** rather than truncating. A truncated
list returned as if complete is worse than no list: the indexer would treat
it as authoritative, replace retained rows' tags with the truncated view,
and mark every artifact outside the prefix as removed. An error keeps that
source's removal step skipped and says which repository is out of shape.
"""


class StaticRegistrySource:
    """See the module docstring. Constructed by ``build_registry_sources``."""

    def __init__(
        self,
        config: CogRegistrySourceConfig,
        *,
        oci_client_factory: OCIClientFactory,
        http_transport: httpx.AsyncBaseTransport | None = None,
        restrict_redirects: bool = False,
    ) -> None:
        self.id = config.id
        self.host = registry_host(config.url)
        self._repositories = tuple(config.repositories)
        self._index_url = config.index_url
        credentials = (
            BasicCredentials(config.credentials.username, config.credentials.password.get_secret_value())
            if config.credentials.configured
            else None
        )
        self._oci = oci_client_factory(
            config.url,
            credentials=credentials,
            token_url=config.token_url or None,
            ca_bundle_path=config.ca_bundle_path or None,
            timeout_seconds=config.request_timeout_seconds,
            transport=http_transport,
            redirect_hosts=tuple(config.blob_redirect_hosts),
            restrict_redirects=restrict_redirects,
        )
        # The index is fetched anonymously: it may live anywhere, and the
        # registry credential must not be presented to an arbitrary URL.
        self._http: httpx.AsyncClient | None = (
            httpx.AsyncClient(
                timeout=config.request_timeout_seconds,
                verify=http_verify(config.ca_bundle_path),
                transport=http_transport,
                follow_redirects=False,
            )
            if config.index_url
            else None
        )
        self._closed = False

    async def list_repositories(self) -> list[str]:
        repos = set(self._repositories)
        if self._index_url:
            repos.update(await self._fetch_index())
        return sorted(repos)

    async def list_artifacts(self, repo: str) -> list[ArtifactRef]:
        tags = sorted(set(await self._oci.list_tags(repo)))
        if len(tags) > MAX_TAGS_PER_REPOSITORY:
            # Never truncate: see MAX_TAGS_PER_REPOSITORY. The indexer treats
            # this like any other failed listing -- reconcile what it can,
            # skip removal for the source.
            raise RegistrySourceProtocolError(
                f"source {self.id!r}: repository {repo} lists {len(tags)} tags, over the"
                f" {MAX_TAGS_PER_REPOSITORY} bound; refusing to enumerate a truncated view"
            )
        grouped: dict[str, list[str]] = {}
        details: dict[str, ArtifactRef] = {}
        for tag in tags:
            try:
                manifest = await self._oci.get_manifest(repo, tag)
            except OCINotFound:
                # Deleted between list_tags and here; the next sweep settles it.
                continue
            grouped.setdefault(manifest.digest, []).append(tag)
            details.setdefault(
                manifest.digest,
                ArtifactRef(
                    digest=manifest.digest,
                    pushed_at=parse_timestamp(manifest.annotations.get(CREATED_ANNOTATION)),
                    media_type=manifest.media_type or None,
                ),
            )
        return [
            ArtifactRef(
                digest=digest,
                tags=tuple(sorted(grouped[digest])),
                pushed_at=details[digest].pushed_at,
                media_type=details[digest].media_type,
            )
            for digest in sorted(grouped)
        ]

    def oci(self) -> OCIClient:
        return self._oci

    def parse_event(self, request: WebhookRequest) -> list[RegistryEvent] | None:
        return None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._http is not None:
                await self._http.aclose()
        finally:
            await self._oci.aclose()

    async def _fetch_index(self) -> list[str]:
        assert self._http is not None
        try:
            async with self._http.stream("GET", self._index_url) as response:
                if response.status_code != 200:
                    # The URL is never echoed: index_url may carry an access
                    # token in its path, and the operator knows the URL from
                    # the source's own configuration.
                    raise RegistrySourceProtocolError(
                        f"source {self.id!r}: the configured index_url answered HTTP {response.status_code}"
                    )
                body = await _read_capped(response, MAX_INDEX_BYTES, what=f"source {self.id!r} index")
        except httpx.HTTPError as exc:
            # Class name only: httpx messages embed the request URL.
            raise RegistrySourceError(
                f"source {self.id!r}: fetching the configured index_url failed: {type(exc).__name__}"
            ) from exc
        return parse_index_document(body, what=f"source {self.id!r} index")


async def _read_capped(response: httpx.Response, max_bytes: int, *, what: str) -> bytes:
    declared = response.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise RegistrySourceProtocolError(f"{what} declares {declared} bytes; the cap is {max_bytes}")
    chunks: list[bytes] = []
    received = 0
    async for chunk in response.aiter_bytes():
        received += len(chunk)
        if received > max_bytes:
            raise RegistrySourceProtocolError(f"{what} exceeds the {max_bytes}-byte cap")
        chunks.append(chunk)
    return b"".join(chunks)


def parse_index_document(body: bytes, *, what: str = "index") -> list[str]:
    """Validate a ``catalog.v1.json``-shaped document and return its repository paths, sorted.

    Strict on purpose: the index is an operator-published artifact, so a
    malformed entry is a publishing bug worth failing loudly on, not a row to
    skip quietly.
    """

    try:
        document = json.loads(body)
    except ValueError as exc:
        raise RegistrySourceProtocolError(f"{what} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise RegistrySourceProtocolError(f"{what} must be a JSON object")
    if document.get("schemaVersion") != INDEX_SCHEMA_VERSION:
        raise RegistrySourceProtocolError(
            f"{what} has schemaVersion {document.get('schemaVersion')!r}; expected {INDEX_SCHEMA_VERSION}"
        )
    entries = document.get("repositories")
    if not isinstance(entries, list):
        raise RegistrySourceProtocolError(f"{what} must carry a 'repositories' list")
    repos: list[str] = []
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RegistrySourceProtocolError(f"{what} repositories[{position}] must be an object")
        namespace = entry.get("namespace")
        name = entry.get("name")
        if not isinstance(namespace, str) or not isinstance(name, str):
            raise RegistrySourceProtocolError(f"{what} repositories[{position}] needs string 'namespace' and 'name'")
        repo = f"{namespace}/{name}"
        if not is_repository_path(repo):
            raise RegistrySourceProtocolError(
                f"{what} repositories[{position}] is not an OCI repository path: {repo!r}"
            )
        if repo in repos:
            raise RegistrySourceProtocolError(f"{what} lists {repo!r} twice")
        repos.append(repo)
    return sorted(repos)
