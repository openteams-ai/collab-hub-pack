"""The OCI Distribution read API on the Hub host (issue #179): Cog pulls through the Hub.

Mounted only when ``cogs.serve.enabled``. Standard clients (``nebi``,
``oras``, ``docker``) speak to it unchanged:

- ``GET|HEAD /v2/`` -- the version check, and where an unauthenticated client
  learns how to authenticate;
- ``GET|HEAD /v2/<name>/manifests/<reference>`` -- by tag or by digest;
- ``GET|HEAD /v2/<name>/blobs/<digest>``;
- ``GET /v2/<name>/tags/list``;
- ``GET /v2/token`` -- the distribution token endpoint.

What is served, and how a blob is shown to be reachable, is
:mod:`..cogs.serving`; this module is the HTTP surface: authentication, the
OCI error format, and streaming.

**Authentication.** Every ``/v2/`` route requires ``Authorization: Bearer
<pull token>`` and answers ``401`` with a ``WWW-Authenticate: Bearer
realm="<hub>/v2/token",service="<registry host>"[,scope="repository:<name>:pull"]``
challenge otherwise. The token endpoint mints a pull token for either

- HTTP Basic auth with a **registry credential** (exchanged at ``POST
  /v1/cogs/registry-credentials``), which is what ``docker login`` /
  ``nebi registry add`` store; or
- a Hub credential the API already accepts (a bearer access token, the
  gateway's ``IdToken-*`` cookie), for a client that attaches it per request
  and never stores it.

A pull token is checked on every request, and so is its owner: on a
membership-resolving deployment the owner's current membership is read again
each time (:func:`require_admitted`), so a member removed after a token was
minted is refused on the next request, not when the token expires.

The path-protection middleware does not run on ``/v2``: its refusal is the
Hub API's JSON envelope, and a registry client needs the challenge above to
find the token endpoint at all. Nothing here is reachable without one of the
two credentials.

**Nothing about the backing registry leaves this module.** Bodies are either
the artifact's own bytes or an error written here; upstream error bodies are
never read, upstream headers are never copied, and upstream redirects are
followed by the Hub's OCI client rather than relayed.

**Streaming.** A blob is relayed chunk by chunk, hashed as it passes, and
its last chunk is held until the hash and the length have been checked, so
a blob that fails verification reaches the client short, never complete and
wrong. Every request runs under one aggregate deadline, from its first
lookup: ``cogs.serve.max_blob_seconds`` for a blob body, thirty seconds for
everything else. The response owns the open upstream blob and closes it
however the exchange ends.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
import time
from collections.abc import AsyncIterator
from datetime import UTC

import anyio
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..cogs.catalog import DEFAULT_TAGS_PAGE, MAX_TAGS_PAGE, CogCatalogUnavailableError
from ..cogs.deadline import BudgetExhausted, request_deadline
from ..cogs.oci import OCIError
from ..cogs.registry import is_repository_path
from ..cogs.registry_credentials import (
    RegistryCredentialsUnavailableError,
    TokenGrant,
    new_pull_token,
    secret_digest,
)
from ..cogs.serving import (
    BlobTooLarge,
    BlobUnknown,
    CogRegistryServing,
    ManifestUnknown,
    RepositoryUnknown,
    ServedBlob,
    ServeError,
    UpstreamUnavailable,
)
from ..frames.auth import NoOrganizationError, auth_context_from_membership, get_auth_context
from ..frames.db import postgres_error_classes
from ..frames.identity import PINNED_IDENTITY_CLAIM
from ..frames.org_source import org_source_resolves_membership
from ..frames.orgs import OrgsUnavailableError

logger = logging.getLogger("frames_server.cogs.registry")

router = APIRouter(include_in_schema=False)

REGISTRY_PATH_PREFIX = "/v2"
API_VERSION_HEADERS = {"Docker-Distribution-API-Version": "registry/2.0"}
MAX_TOKEN_SCOPES = 16
"""Repositories one pull token may name; a client asks for one, a multi-repository tool for a few."""
_PAGE_NUMBER = re.compile(r"[0-9]{1,9}")
MAX_SCOPE_ENTRIES = 64
MAX_SCOPE_LENGTH = 8192
"""How much of a token request's ``scope`` input is looked at at all: entries, and characters per parameter."""


def registry_path(path: str) -> bool:
    """Whether ``path`` belongs to the registry surface, which authenticates itself."""

    return path == REGISTRY_PATH_PREFIX or path.startswith(REGISTRY_PATH_PREFIX + "/")


class RegistryError(Exception):
    """A refusal in the OCI error format: ``{"errors": [{"code", "message", "detail"}]}``."""

    def __init__(self, status_code: int, code: str, message: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}


class BlobStreamAborted(Exception):
    """A blob response was abandoned after its headers were sent.

    Raised out of the response body so the server drops the connection
    instead of completing it: the client sees a short body and fails its own
    digest check. Carries a message written here, with no cause attached, so
    the server's traceback of it names no upstream.
    """


def error_response(exc: RegistryError) -> JSONResponse:
    return JSONResponse(
        {"errors": [{"code": exc.code, "message": exc.message, "detail": {}}]},
        status_code=exc.status_code,
        headers={**API_VERSION_HEADERS, **exc.headers},
    )


def _serving(request: Request) -> CogRegistryServing:
    return request.app.state.cog_registry_serving


def _quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _challenge(serving: CogRegistryServing, repository: str | None, error: str | None = None) -> dict[str, str]:
    parts = [f'realm="{_quoted(serving.token_url)}"', f'service="{_quoted(serving.host)}"']
    if repository is not None and is_repository_path(repository):
        parts.append(f'scope="repository:{repository}:pull"')
    if error is not None:
        parts.append(f'error="{error}"')
    return {"WWW-Authenticate": "Bearer " + ",".join(parts)}


def _unauthorized(serving: CogRegistryServing, repository: str | None, *, insufficient: bool = False) -> RegistryError:
    return RegistryError(
        status.HTTP_401_UNAUTHORIZED,
        "UNAUTHORIZED",
        "authentication required",
        _challenge(serving, repository, "insufficient_scope" if insufficient else None),
    )


def _unavailable() -> RegistryError:
    return RegistryError(status.HTTP_503_SERVICE_UNAVAILABLE, "UNAVAILABLE", "the registry is temporarily unavailable")


def _start_budget(seconds: float) -> None:
    """Start this request's budget for blocking store calls: catalog, credentials, and the membership re-check.

    A context variable, so the threadpool hop carries it and the stores read
    it without being handed it; see :mod:`..cogs.deadline`.
    """

    request_deadline.set(time.monotonic() + seconds)


async def _within_budget(request: Request, operation) -> Response:
    """Run a metadata operation under the aggregate deadline, in the registry's error format.

    The coroutine timeout covers what the database-side budget cannot:
    waiting for a threadpool slot before any store call has even begun.
    """

    budget = _serving(request).max_metadata_seconds
    _start_budget(budget)
    try:
        async with asyncio.timeout(budget):
            return await operation()
    except RegistryError as exc:
        return error_response(exc)
    except TimeoutError:
        return error_response(_unavailable())
    except _storage_errors():
        return error_response(_unavailable())


def _storage_errors() -> tuple[type[Exception], ...]:
    return (
        RegistryCredentialsUnavailableError,
        CogCatalogUnavailableError,
        OrgsUnavailableError,
        BudgetExhausted,
        *postgres_error_classes(),
    )


def require_admitted(request: Request, user_id: str) -> None:
    """Refuse a principal the catalog would not admit *now*.

    A pull token (and the credential it came from) outlives the request that
    proved who its owner was, so the owner's standing is read again wherever
    one is used: on a membership-resolving deployment, the same store lookup
    the catalog's own authentication makes, with the same outcome -- a
    removed member, or one who never had an organization, is refused, and a
    lookup that fails propagates (to a 503) rather than admitting anyone.

    Under claims-sourced auth there is nothing server-side to re-read: the
    organization is whatever the Hub token said when it was presented, and
    the token and credential lifetimes are the bound.
    """

    if not org_source_resolves_membership():
        return
    try:
        auth_context_from_membership({PINNED_IDENTITY_CLAIM: user_id}, request.app.state.org_store)
    except NoOrganizationError:
        raise _denied("this account is not part of an organization") from None


def _find_grant(request: Request, token: str) -> TokenGrant | None:
    grant = _serving(request).credentials.find_token(secret_digest(token))
    if grant is not None:
        require_admitted(request, grant.user_id)
    return grant


async def _grant(request: Request, repository: str | None) -> TokenGrant:
    """The live pull token this request presents, whose owner is still admitted, allowed on ``repository``."""

    serving = _serving(request)
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise _unauthorized(serving, repository)
    # One threadpool hop: the token lookup and the admission lookup both block.
    grant = await run_in_threadpool(_find_grant, request, token)
    if grant is None:
        raise _unauthorized(serving, repository)
    if repository is not None and not grant.allows_pull(repository):
        raise _unauthorized(serving, repository, insufficient=True)
    return grant


# -- the token endpoint ---------------------------------------------------------


def requested_repositories(scopes: list[str]) -> list[str]:
    """The repositories a token request may be granted ``pull`` on.

    ``scope`` may repeat and may carry several space-separated entries. Only
    ``repository:<name>:<actions>`` with ``pull`` among the actions grants
    anything; every other action (``push``, ``delete``) and resource type is
    dropped rather than refused, which is how the token spec says a server
    narrows a request -- the client finds out when it uses the token.
    """

    names: dict[str, None] = {}
    examined = 0
    for scope in scopes[:MAX_SCOPE_ENTRIES]:
        for entry in scope[:MAX_SCOPE_LENGTH].split():
            examined += 1
            if examined > MAX_SCOPE_ENTRIES or len(names) >= MAX_TOKEN_SCOPES:
                return list(names)
            kind, _, rest = entry.partition(":")
            name, _, actions = rest.rpartition(":")
            if kind != "repository" or "pull" not in actions.split(","):
                continue
            if is_repository_path(name):
                names.setdefault(name)
    return list(names)


def _basic_credential(header: str) -> tuple[str, str] | None:
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return "", ""
    username, _, password = decoded.partition(":")
    return username, password


def _token_refused(serving: CogRegistryServing) -> RegistryError:
    return RegistryError(
        status.HTTP_401_UNAUTHORIZED,
        "UNAUTHORIZED",
        "authentication required",
        {"WWW-Authenticate": f'Basic realm="{_quoted(serving.host)}"'},
    )


def _denied(message: str) -> RegistryError:
    return RegistryError(status.HTTP_403_FORBIDDEN, "DENIED", message)


def _hub_principal(request: Request) -> str:
    """The caller of a token request, by the rule ``GET /v1/cogs`` applies: a Hub credential the API accepts."""

    serving = _serving(request)
    try:
        return get_auth_context(request).user
    except NoOrganizationError:
        raise _denied("this account is not part of an organization") from None
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            raise _token_refused(serving) from None
        raise


def _credential_principal(request: Request, username: str, password: str) -> tuple[str, str]:
    """``(user, credential id)`` for a registry credential that is live and whose owner is still admitted."""

    serving = _serving(request)
    credential = serving.credentials.find_credential(username, secret_digest(password)) if username else None
    if credential is None:
        raise _token_refused(serving)
    # The credential outlives the request it was exchanged on; its owner's
    # standing is read again at every mint, as it is at every read.
    require_admitted(request, credential.user_id)
    return credential.user_id, credential.id


def _mint(request: Request) -> JSONResponse:
    serving = _serving(request)
    basic = _basic_credential(request.headers.get("Authorization", ""))
    if basic is not None:
        user, credential_id = _credential_principal(request, *basic)
    else:
        user, credential_id = _hub_principal(request), None
    token = new_pull_token()
    grant = serving.credentials.create_token(
        token_hash=secret_digest(token),
        user_id=user,
        credential_id=credential_id,
        repositories=requested_repositories(request.query_params.getlist("scope")),
        ttl_seconds=serving.token_ttl_seconds,
    )
    if grant is None:
        # The credential was revoked or expired between the check and the mint.
        raise _token_refused(serving)
    expires_in = max(int((grant.expires_at - grant.issued_at).total_seconds()), 0)
    return JSONResponse(
        {
            "token": token,
            "access_token": token,
            "expires_in": expires_in,
            "issued_at": grant.issued_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        headers={**API_VERSION_HEADERS, "Cache-Control": "no-store"},
    )


@router.get(REGISTRY_PATH_PREFIX + "/token")
async def token(request: Request) -> Response:
    # One threadpool hop for the whole mint: the credential lookup, the
    # membership re-check and the insert are all blocking store calls.
    return await _within_budget(request, lambda: run_in_threadpool(_mint, request))


# -- the read API ---------------------------------------------------------------


def parse_registry_path(rest: str) -> tuple[str, str, str] | None:
    """``(kind, name, reference)`` for ``<name>/manifests/<ref>``, ``<name>/blobs/<digest>``, ``<name>/tags/list``.

    The marker is taken from the right: a repository may itself contain a
    component called ``manifests`` or ``blobs``, and a reference never
    contains a ``/``.
    """

    if rest.endswith("/tags/list"):
        return "tags", rest[: -len("/tags/list")], ""
    best: tuple[int, str] | None = None
    for kind in ("manifests", "blobs"):
        index = rest.rfind(f"/{kind}/")
        if index > 0 and (best is None or index > best[0]):
            best = (index, kind)
    if best is None:
        return None
    index, kind = best
    return kind, rest[:index], rest[index + len(kind) + 2 :]


class _BlobResponse(StreamingResponse):
    """A blob relayed under a wall-clock deadline, by a response that owns the open blob.

    The deadline covers time spent waiting on the *client* as well as on the
    source: a reader that stops reading would otherwise hold a registry
    connection open for as long as it liked.

    Ownership is the other half. However the response ends -- completed,
    past its deadline while blocked in ``send``, the client gone, a failure
    before the first chunk -- the upstream response is closed here, in a
    ``finally`` shielded from the cancellation that may be what ended it.
    The chunk generators cannot be relied on for that: one suspended at a
    ``yield`` is only cleaned up when something closes it, and one that
    never started has no cleanup to run.
    """

    def __init__(self, blob: ServedBlob, *, deadline: float, what: str, headers: dict[str, str]) -> None:
        super().__init__(_relay(blob, what), headers=headers)
        self._blob = blob
        self._deadline = deadline
        self._what = what

    async def __call__(self, scope, receive, send) -> None:
        try:
            async with asyncio.timeout_at(self._deadline):
                await super().__call__(scope, receive, send)
        except TimeoutError:
            logger.warning("cog_serve_blob_timed_out", extra={"blob": self._what})
            raise BlobStreamAborted(f"{self._what}: exceeded the registry's time limit") from None
        finally:
            with anyio.CancelScope(shield=True):
                await self._blob.aclose()


async def _relay(blob: ServedBlob, what: str) -> AsyncIterator[bytes]:
    try:
        async for chunk in blob.chunks:
            yield chunk
    except OCIError as exc:
        # After the headers: the only honest signal left is a short body. The
        # class name is logged; the cause is cut so nothing upstream rides
        # along in the server's traceback.
        logger.warning("cog_serve_blob_aborted", extra={"blob": what, "error": type(exc).__name__})
        raise BlobStreamAborted(f"{what}: the blob could not be delivered intact") from None


def _digest_headers(digest: str) -> dict[str, str]:
    return {**API_VERSION_HEADERS, "Docker-Content-Digest": digest, "ETag": f'"{digest}"'}


async def _serve(request: Request, rest: str) -> Response:
    serving = _serving(request)
    head = request.method == "HEAD"
    now = asyncio.get_running_loop().time()
    parsed = parse_registry_path(rest)
    kind = parsed[0] if parsed is not None else None
    streaming = kind == "blobs" and not head
    # One aggregate deadline per request, set before the first lookup: the
    # token, the catalog, the source, and (for a blob body) the relay to the
    # client all spend from it.
    budget = serving.max_blob_seconds if streaming else serving.max_metadata_seconds
    deadline = now + budget
    # The same budget for the blocking store calls, which a cancelled
    # coroutine cannot stop: the database is told when to give up instead.
    _start_budget(min(budget, serving.max_metadata_seconds))
    blob: ServedBlob | None = None
    try:
        async with asyncio.timeout_at(deadline):
            if parsed is None:
                await _grant(request, None)
                raise RegistryError(status.HTTP_404_NOT_FOUND, "NAME_UNKNOWN", "repository name not known to registry")
            _kind, name, reference = parsed
            await _grant(request, name)

            if kind == "tags":
                return await _tags_response(request, serving, name)

            if kind == "manifests":
                manifest = await serving.front.manifest(name, reference)
                headers = {
                    **_digest_headers(manifest.digest),
                    "Content-Type": manifest.media_type,
                    "Content-Length": str(len(manifest.body)),
                }
                return Response(b"" if head else manifest.body, headers=headers)

            headers = {**_digest_headers(reference), "Content-Type": "application/octet-stream"}
            if head:
                size = await serving.front.blob_size(name, reference)
                return Response(b"", headers={**headers, "Content-Length": str(size)})
            blob = await serving.front.blob(name, reference)
    except TimeoutError:
        if blob is not None:
            await blob.aclose()
        raise _unavailable() from None
    return _BlobResponse(
        blob,
        deadline=deadline,
        what=f"{name}@{reference}",
        headers={**headers, "Content-Length": str(blob.size)},
    )


async def _tags_response(request: Request, serving: CogRegistryServing, name: str) -> Response:
    """One page of tags: ``n`` of them (a bounded default without ``n``) after ``last``, and a ``Link`` to the next."""

    raw_n = request.query_params.get("n")
    # ASCII decimal, and short: str.isdigit() also accepts "²", and int() refuses a number of thousands of digits.
    if raw_n is not None and not _PAGE_NUMBER.fullmatch(raw_n):
        raise RegistryError(status.HTTP_400_BAD_REQUEST, "PAGINATION_NUMBER_INVALID", "n must be a number")
    n = min(int(raw_n), MAX_TAGS_PAGE) if raw_n is not None else DEFAULT_TAGS_PAGE
    last = request.query_params.get("last") or None
    # One more than the page says whether another follows, without a count.
    tags = await serving.front.tags(name, after=last, limit=n + 1)
    headers = dict(API_VERSION_HEADERS)
    if len(tags) > n:
        tags = tags[:n]
        if tags:
            headers["Link"] = f'<{REGISTRY_PATH_PREFIX}/{name}/tags/list?n={n}&last={tags[-1]}>; rel="next"'
    return JSONResponse({"name": name, "tags": tags}, headers=headers)


_SERVE_ERRORS: dict[type[ServeError], tuple[int, str]] = {
    RepositoryUnknown: (status.HTTP_404_NOT_FOUND, "NAME_UNKNOWN"),
    ManifestUnknown: (status.HTTP_404_NOT_FOUND, "MANIFEST_UNKNOWN"),
    BlobUnknown: (status.HTTP_404_NOT_FOUND, "BLOB_UNKNOWN"),
    BlobTooLarge: (status.HTTP_403_FORBIDDEN, "DENIED"),
    UpstreamUnavailable: (status.HTTP_503_SERVICE_UNAVAILABLE, "UNAVAILABLE"),
}


async def _answer(request: Request, rest: str) -> Response:
    try:
        return await _serve(request, rest)
    except RegistryError as exc:
        return error_response(exc)
    except ServeError as exc:
        status_code, code = _SERVE_ERRORS[type(exc)]
        return error_response(RegistryError(status_code, code, str(exc)))
    except _storage_errors():
        return error_response(_unavailable())


@router.api_route(REGISTRY_PATH_PREFIX, methods=["GET", "HEAD"])
@router.api_route(REGISTRY_PATH_PREFIX + "/", methods=["GET", "HEAD"])
async def base(request: Request) -> Response:
    """The version check: ``200 {}`` with a pull token, the bearer challenge without one."""

    async def check() -> Response:
        await _grant(request, None)
        return JSONResponse({}, headers=API_VERSION_HEADERS)

    return await _within_budget(request, check)


@router.api_route(REGISTRY_PATH_PREFIX + "/{rest:path}", methods=["GET", "HEAD"])
async def read(request: Request, rest: str) -> Response:
    return await _answer(request, rest)


@router.api_route(REGISTRY_PATH_PREFIX, methods=["POST", "PUT", "PATCH", "DELETE"])
@router.api_route(REGISTRY_PATH_PREFIX + "/{rest:path}", methods=["POST", "PUT", "PATCH", "DELETE"])
async def write(_request: Request, rest: str = "") -> Response:
    """Pushes and deletes are not served: this surface is read-only."""

    return error_response(
        RegistryError(status.HTTP_405_METHOD_NOT_ALLOWED, "UNSUPPORTED", "this registry is read-only")
    )
