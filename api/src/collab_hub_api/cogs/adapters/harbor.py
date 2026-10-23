"""``harbor`` registry source: Harbor's REST API for enumeration and its webhooks for change events.

Everything else — manifests, blobs, tags — goes through the generic OCI client,
which this adapter merely constructs with the robot credential. Three Harbor
facts shape the code:

- Repositories are listed **per project** (``/api/v2.0/projects/{project}/repositories``);
  ``/v2/_catalog`` is restricted. The ``name`` field is already project-prefixed
  (``cogs/cog-a1b2``) and *is* the OCI repository path.
- The artifacts route wants the repository name **without** the project prefix
  and with every ``/`` double-encoded as ``%252F`` (Harbor's router decodes the
  path once before matching).
- Webhooks arrive in one of two formats, "Default" and "CloudEvents", carrying
  the same ``event_data`` shape. Payload shapes verified against
  https://goharbor.io/docs/2.13.0/working-with-projects/project-configuration/configure-webhooks/
  and the structs in ``src/pkg/notifier/model/event.go`` plus the type mapping in
  ``src/pkg/notifier/formats/cloudevents.go`` of the Harbor source tree.

``host`` derives from the external ``url``; ``api_url`` is an in-cluster
transport override and never appears in a stored reference.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from ..oci import BasicCredentials, OCIClient
from ..registry import (
    ArtifactRef,
    CogRegistrySourceConfig,
    OCIClientFactory,
    RegistryEvent,
    RegistrySourceAuthError,
    RegistrySourceError,
    RegistrySourceProtocolError,
    WebhookRequest,
    http_verify,
    is_digest,
    is_repository_path,
    parse_timestamp,
    registry_host,
)

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v2.0"
PAGE_SIZE = 100
MAX_PAGES = 100
"""10,000 repositories per project or artifacts per repository; past that the enumeration is refused, not truncated."""
MAX_WEBHOOK_BODY_BYTES = 1024 * 1024

# Default-format `type` values and their normalized kinds. Every other type
# Harbor emits is recognized (so the receiver can answer "ignored") but yields
# no events.
_DEFAULT_EVENT_KINDS = {"PUSH_ARTIFACT": "push", "DELETE_ARTIFACT": "delete"}
_DEFAULT_IGNORED_TYPES = frozenset(
    {
        "PULL_ARTIFACT",
        "SCANNING_COMPLETED",
        "SCANNING_STOPPED",
        "SCANNING_FAILED",
        "QUOTA_EXCEED",
        "QUOTA_WARNING",
        "REPLICATION",
        "TAG_RETENTION",
    }
)
# CloudEvents-format `type` values (prefix "harbor." per cloudevents.go).
_CLOUDEVENTS_EVENT_KINDS = {"harbor.artifact.pushed": "push", "harbor.artifact.deleted": "delete"}
_CLOUDEVENTS_TYPE_PREFIX = "harbor."

_LINK_NEXT = re.compile(r'<[^>]*>\s*;\s*rel="?next"?', re.IGNORECASE)


class HarborRegistrySource:
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
        self._projects = tuple(config.projects)
        # The REST base is the origin of api_url (or url) plus /api/v2.0; an
        # api_url that already ends in /api/v2.0 is accepted so both spellings
        # from the issue work. The OCI client shares the origin so in-cluster
        # traffic stays in-cluster, with token_url covering the bearer dance.
        origin = _origin(config.api_url or config.url)
        self._api_base = origin + API_PREFIX
        credentials = (
            BasicCredentials(config.credentials.username, config.credentials.password.get_secret_value())
            if config.credentials.configured
            else None
        )
        self._http = httpx.AsyncClient(
            auth=httpx.BasicAuth(credentials.username, credentials.password) if credentials else None,
            timeout=config.request_timeout_seconds,
            verify=http_verify(config.ca_bundle_path),
            transport=http_transport,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )
        self._oci = oci_client_factory(
            origin,
            credentials=credentials,
            token_url=config.token_url or None,
            ca_bundle_path=config.ca_bundle_path or None,
            timeout_seconds=config.request_timeout_seconds,
            transport=http_transport,
            redirect_hosts=tuple(config.blob_redirect_hosts),
            restrict_redirects=restrict_redirects,
        )
        self._closed = False

    # -- enumeration -----------------------------------------------------------

    async def list_repositories(self) -> list[str]:
        repos: set[str] = set()
        for project in self._projects:
            for entry in await self._paged(f"/projects/{quote(project, safe='')}/repositories"):
                name = entry.get("name") if isinstance(entry, dict) else None
                if not is_repository_path(name) or not name.startswith(f"{project}/"):
                    logger.warning(
                        "cogs.registry: source %s project %s listed a repository name that is not an OCI path "
                        "under the project; skipping",
                        self.id,
                        project,
                    )
                    continue
                repos.add(name)
        return sorted(repos)

    async def list_artifacts(self, repo: str) -> list[ArtifactRef]:
        project, _, name = repo.partition("/")
        if not name or project not in self._projects or not is_repository_path(repo):
            raise RegistrySourceError(
                f"source {self.id!r}: repository {repo!r} is not under a configured project "
                f"({', '.join(self._projects)})"
            )
        # Harbor's router decodes the path once before matching, so a nested
        # repository name (a/b) must arrive as a%252Fb to survive as one segment.
        encoded_name = quote(quote(name, safe=""), safe="")
        entries = await self._paged(
            f"/projects/{quote(project, safe='')}/repositories/{encoded_name}/artifacts",
            params={"with_tag": "true"},
        )
        # One ref per digest even if the listing repeats one: tags accumulate,
        # the first usable timestamp and media type win.
        tags_by_digest: dict[str, set[str]] = {}
        details: dict[str, ArtifactRef] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            digest = entry.get("digest")
            if not is_digest(digest):
                logger.warning(
                    "cogs.registry: source %s repository %s listed an artifact without a digest", self.id, repo
                )
                continue
            tags = entry.get("tags")
            names = tags_by_digest.setdefault(digest, set())
            if isinstance(tags, list):
                names.update(tag["name"] for tag in tags if isinstance(tag, dict) and isinstance(tag.get("name"), str))
            media_type = entry.get("manifest_media_type")
            seen = details.get(digest)
            details[digest] = ArtifactRef(
                digest=digest,
                pushed_at=(seen.pushed_at if seen else None) or parse_timestamp(entry.get("push_time")),
                media_type=(seen.media_type if seen else None)
                or (media_type if isinstance(media_type, str) and media_type else None),
            )
        return [
            ArtifactRef(
                digest=digest,
                tags=tuple(sorted(tags_by_digest[digest])),
                pushed_at=details[digest].pushed_at,
                media_type=details[digest].media_type,
            )
            for digest in sorted(details)
        ]

    def oci(self) -> OCIClient:
        return self._oci

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._http.aclose()
        finally:
            await self._oci.aclose()

    async def _paged(self, path: str, *, params: dict[str, str] | None = None) -> list[Any]:
        """Walk Harbor's page/page_size pagination.

        The ``Link: <...>; rel="next"`` header is the authoritative "more"
        signal; without a ``Link`` header at all, a full page means more. The
        page number is always ours — the URL inside ``Link`` is never followed,
        so a registry cannot redirect the enumeration elsewhere.
        """

        items: list[Any] = []
        for page in range(1, MAX_PAGES + 1):
            query = {**(params or {}), "page": str(page), "page_size": str(PAGE_SIZE)}
            try:
                response = await self._http.get(self._api_base + path, params=query)
            except httpx.HTTPError as exc:
                raise RegistrySourceError(f"source {self.id!r}: request to the registry API failed: {exc}") from exc
            self._check_status(response, path)
            try:
                batch = response.json()
            except ValueError as exc:
                raise RegistrySourceProtocolError(f"source {self.id!r}: {path} page {page} is not JSON") from exc
            if not isinstance(batch, list):
                raise RegistrySourceProtocolError(f"source {self.id!r}: {path} page {page} is not a JSON list")
            items.extend(batch)
            link = response.headers.get("link")
            has_more = _LINK_NEXT.search(link) is not None if link is not None else len(batch) >= PAGE_SIZE
            if not has_more:
                return items
        raise RegistrySourceProtocolError(
            f"source {self.id!r}: {path} did not end within {MAX_PAGES} pages of {PAGE_SIZE}; "
            "refusing a truncated listing"
        )

    def _check_status(self, response: httpx.Response, path: str) -> None:
        status = response.status_code
        if status == 200:
            return
        # Messages name the path and status only: the credential is never part
        # of them, and Harbor's error body is not echoed either.
        if status in (401, 403):
            raise RegistrySourceAuthError(
                f"source {self.id!r}: the registry API refused the configured credential for {path} (HTTP {status})"
            )
        if status == 404:
            raise RegistrySourceError(f"source {self.id!r}: {path} does not exist on the registry (HTTP 404)")
        raise RegistrySourceProtocolError(f"source {self.id!r}: {path} answered HTTP {status}")

    # -- webhooks --------------------------------------------------------------

    def parse_event(self, request: WebhookRequest) -> list[RegistryEvent] | None:
        """Translate a Harbor webhook delivery in either format.

        ``None`` when the body is not a Harbor payload — or is a Harbor payload
        for a project (or host) this source does not own, so a second source
        configured for that project can claim it. ``[]`` for Harbor event types
        the indexer does not act on.
        """

        if len(request.body) > MAX_WEBHOOK_BODY_BYTES:
            logger.debug("cogs.registry: source %s ignoring an oversized webhook body", self.id)
            return None
        try:
            payload = json.loads(request.body)
        except (ValueError, RecursionError):
            # RecursionError: a body nested deeper than the interpreter's stack.
            return None
        if not isinstance(payload, dict):
            return None
        event_type = payload.get("type")
        if not isinstance(event_type, str):
            return None

        if "specversion" in payload:
            if not event_type.startswith(_CLOUDEVENTS_TYPE_PREFIX):
                return None
            kind = _CLOUDEVENTS_EVENT_KINDS.get(event_type)
            data = payload.get("data")
        elif event_type in _DEFAULT_EVENT_KINDS or event_type in _DEFAULT_IGNORED_TYPES:
            kind = _DEFAULT_EVENT_KINDS.get(event_type)
            data = payload.get("event_data")
        else:
            return None

        # Ownership first, for ignored types too: a scan event for another
        # source's project must stay claimable by that source.
        repository = data.get("repository") if isinstance(data, dict) else None
        if not isinstance(repository, dict):
            return []
        namespace = repository.get("namespace")
        if namespace not in self._projects:
            logger.debug(
                "cogs.registry: source %s dropping a webhook for project %r (not configured)", self.id, namespace
            )
            return None
        if kind is None:
            return []
        repo = repository.get("repo_full_name")
        if not isinstance(repo, str) or not repo:
            repo = f"{namespace}/{repository.get('name')}"
        if not is_repository_path(repo):
            logger.warning("cogs.registry: source %s webhook named a repository that is not an OCI path", self.id)
            return []

        resources = data.get("resources")
        grouped: dict[str | None, set[str]] = {}
        for resource in resources if isinstance(resources, list) else []:
            if not isinstance(resource, dict):
                continue
            resource_url = resource.get("resource_url")
            if isinstance(resource_url, str) and resource_url and resource_url.split("/", 1)[0] != self.host:
                # Same project name on a different registry: not this source's.
                # Warned rather than debug-logged because a mismatch between the
                # configured url and the registry's external endpoint would
                # otherwise drop every event silently.
                logger.warning(
                    "cogs.registry: source %s (host %s) dropping a webhook whose resource_url names another host",
                    self.id,
                    self.host,
                )
                return None
            digest = resource.get("digest")
            digest = digest if is_digest(digest) else None
            tag = resource.get("tag")
            tags = grouped.setdefault(digest, set())
            # Harbor reports the digest itself as the "tag" of an untagged artifact.
            if isinstance(tag, str) and tag and tag != digest and not is_digest(tag):
                tags.add(tag)
        if not grouped:
            return []
        return [
            RegistryEvent(kind=kind, repo=repo, digest=digest, tags=tuple(sorted(tags)))  # type: ignore[arg-type]
            for digest, tags in sorted(grouped.items(), key=lambda item: item[0] or "")
        ]


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"
