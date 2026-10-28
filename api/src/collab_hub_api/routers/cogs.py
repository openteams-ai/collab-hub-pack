"""The Cog catalog read API (issue #85): discover, inspect and pin indexed Cogs.

Read-only over the catalog the indexer fills (:mod:`..cogs.catalog`). No
route here contacts a registry: every answer comes from what was captured at
index time, and an install goes to the host the pinned reference names --
the backing registry, or the Hub's own ``/v2/`` surface
(:mod:`.registry`) when ``cogs.serve.enabled`` (issue #179).

Also here, because they are Hub API routes with Hub authentication:
``POST|DELETE /v1/cogs/registry-credentials``, the exchange of a Hub session
for a credential valid only on that ``/v2/`` surface.

- ``GET /v1/cogs`` -- current Cogs, one per ``cog_id`` (its newest present
  version), filtered and paged with ``limit`` + ``offset``. Items carry a
  trimmed card (no ``body``, ``profile_raw``, ``frontmatter_raw``).
- ``GET /v1/cogs/{cog_id}`` -- the current full card plus every indexed version.
- ``GET /v1/cogs/{cog_id}/versions/{digest}`` -- the card for one digest.
  Removed artifacts stay reachable here (installs pin digests); they are
  hidden only from the listing.
- ``.../versions/{digest}/cog.md`` -- the COG.md Markdown body, as indexed.
- ``.../versions/{digest}/reference`` -- the pinned install reference.
- ``GET /v1/cogs/catalog.v1.json`` -- transitional compatibility view in the
  static ``catalog.v1.json`` shape.

``cog_id`` contains a ``/`` (``<publisher>/<name>``), so it is a ``path``
parameter; the fixed routes are declared before the ``{cog_id:path}`` ones
so ``catalog.v1.json`` is never read as a Cog id. A malformed digest is a
422 (request validation, like every other malformed path value here); a
well-formed digest the catalog does not hold for that Cog is a 404.

Authenticated by default, like the frames routes. A deployment that wants
anonymous discovery adds a ``security.paths`` entry: when the rule that
decides a request's path is ``public`` **and** sits at or below
``/v1/cogs`` (so a broad ``/`` or ``/v1`` rule never opens it by accident),
:func:`get_catalog_caller` admits the request without credentials. A path
no rule matches never counts, whatever ``default_access`` says -- an
unconfigured server's default is ``public``. This is the only route family
that honors a ``public`` entry this way; the others require a caller
regardless.

An anonymous answer is **redacted** (:class:`Redaction`): no ``source_id``
anywhere and no reader diagnostics on cards -- internal source names and
failure strings are more than discovery needs. The pinned ``reference``
stays, since a client must know where to pull from, and the ``source_id``
filter is refused (422) rather than answered, so its values cannot be
probed. "Anonymous" means no credentials were presented (or a valid subject
with no organization); presented credentials that are rejected are a 401,
as on every other route.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError

from ..cogs.catalog import (
    STATUS_INDEXED,
    CatalogFilter,
    CogArtifact,
    CogCatalogStore,
    CogCatalogUnavailableError,
)
from ..cogs.deadline import BudgetExhausted
from ..cogs.models import (
    CatalogV1,
    CatalogV1Repository,
    CogDetail,
    CogEntry,
    CogErrorResponse,
    CogListEntry,
    CogListPage,
    CogLocation,
    CogReference,
    CogVersion,
    PullOnlyRegistryCredentialRequest,
    RegistryCredentialRequest,
    RegistryCredentialResponse,
    client_reference,
)
from ..cogs.registry_credentials import (
    CREDENTIAL_ID_PATTERN,
    SCOPE_PUBLISH,
    RegistryCredentialsUnavailableError,
    new_credential_id,
    new_credential_secret,
    secret_digest,
)
from ..cogs.serving import CogRegistryServing
from ..dependencies import get_cog_catalog_store, get_cog_registry_serving
from ..frames import error_codes
from ..frames.auth import AuthContext, NoOrganizationError, get_auth_context, session_id_of
from ..frames.orgs import PLATFORM_ROLE_OPERATOR
from ..path_protection import request_path, winning_rule
from .frames import error_response

router = APIRouter(prefix="/cogs", tags=["cogs"])

COGS_PATH = "/v1/cogs"


def _within_cogs(rule_path: str) -> bool:
    base = rule_path.rstrip("/")
    return base == COGS_PATH or base.startswith(COGS_PATH + "/")


def presents_credentials(request: Request) -> bool:
    """Whether the request tries to authenticate: an ``IdToken-*`` cookie or an ``Authorization`` header.

    Presence, not value: an empty or whitespace ``Authorization`` header and
    an ``IdToken-*`` cookie with no value are credentials the verifier
    rejects, not their absence, so they keep their 401. The cookie-name rule
    is the one :func:`~..frames.auth.get_id_token` applies; any
    ``Authorization`` scheme counts, because a client that sent one meant to
    be someone.
    """

    return "authorization" in request.headers or any(name.startswith("IdToken-") for name in request.cookies)


def get_catalog_caller(request: Request) -> AuthContext | None:
    """The caller, or ``None`` for an anonymous request a ``public`` catalog rule admits.

    Consults the same protection map the middleware enforces
    (``app.state.path_rules``, resolved by :func:`~..path_protection.winning_rule`).
    Under a ``public`` catalog rule credentials are optional, not ignored:

    - no credentials at all: anonymous (``None``), unless the dev shortcut
      names a caller;
    - credentials :func:`get_auth_context` accepts: that caller, and the full
      answer;
    - credentials it rejects (401): the 401, exactly as the frames routes
      answer -- a bad token and a verifier that cannot run (no JWKS, JWKS
      unreachable) both surface as 401 there, and neither may pass for an
      anonymous visit;
    - a valid subject with no organization (:class:`NoOrganizationError`,
      403): anonymous, since a public page needs no organization;
    - anything else propagates.

    Anything else -- no matching rule, an ``authenticated`` rule, a ``public``
    rule broader than ``/v1/cogs`` -- is exactly :func:`get_auth_context`.
    """

    rule = winning_rule(request_path(request), getattr(request.app.state, "path_rules", ()))
    if rule is not None and rule.access == "public" and _within_cogs(rule.path):
        try:
            return get_auth_context(request)
        except NoOrganizationError:
            return None
        except HTTPException as exc:
            if exc.status_code == status.HTTP_401_UNAUTHORIZED and not presents_credentials(request):
                return None
            raise
    return get_auth_context(request)


ANONYMOUS_REFUSED_FILTERS = ("source_id",)
"""List filters an anonymous caller may not pass: they name values it never sees."""


def get_listing_caller(request: Request) -> AuthContext | None:
    """:func:`get_catalog_caller`, refusing an anonymous caller's :data:`ANONYMOUS_REFUSED_FILTERS` first.

    A dependency runs before the route's own query validation, so the
    refusal is decided on the raw query string: an empty or over-long value
    is refused the same way, and never reaches the generic 422 handler,
    which would echo it back in ``details``.
    """

    caller = get_catalog_caller(request)
    if caller is None:
        refused = [name for name in ANONYMOUS_REFUSED_FILTERS if name in request.query_params]
        if refused:
            # Filtering by a value the caller may not see would let it probe
            # for source ids by watching which requests return items.
            raise RequestValidationError(
                [
                    {
                        "type": "value_error",
                        "loc": ("query", name),
                        "msg": f"{name} is not available to anonymous callers",
                    }
                    for name in refused
                ]
            )
    return caller


def _merged_exclude(first: dict | None, second: dict | None) -> dict | None:
    """Two ``model_dump(exclude=...)`` specs as one."""

    if not first or not second:
        return first or second
    merged = dict(first)
    for key, value in second.items():
        mine = merged.get(key)
        if isinstance(mine, dict) and isinstance(value, dict):
            merged[key] = _merged_exclude(mine, value)
        elif mine is not True:
            merged[key] = value
    return merged


@dataclass(frozen=True)
class Redaction:
    """What the caller may see; the one place a response is cut down for it.

    Each response model declares its anonymous cut as ``ANONYMOUS_EXCLUDE``
    (see :mod:`..cogs.models`); an authenticated caller gets the model whole
    -- except for who published a version through the Hub
    (``PUBLICATION_EXCLUDE``), which is shown only on a Hub that accepts
    publishes, and never to an anonymous caller. On a Hub that does not, the
    keys are absent, so its answers are what they were before they existed.
    """

    anonymous: bool
    publication: bool = False

    @classmethod
    def for_caller(cls, caller: AuthContext | None, request: Request | None = None) -> Redaction:
        serving = get_cog_registry_serving(request) if request is not None else None
        publishing = serving is not None and serving.publisher is not None
        return cls(anonymous=caller is None, publication=publishing and caller is not None)

    def respond(self, model: BaseModel, *, omit: dict | None = None) -> JSONResponse:
        """Apply anonymous, publication and route-specific exclusions together."""

        kind = type(model)
        exclude = kind.ANONYMOUS_EXCLUDE if self.anonymous else None
        if not self.publication:
            exclude = _merged_exclude(exclude, getattr(kind, "PUBLICATION_EXCLUDE", None))
        exclude = _merged_exclude(exclude, omit)
        return JSONResponse(model.model_dump(mode="json", exclude=exclude))


def get_registry_host(request: Request) -> str | None:
    """The Hub's own registry host when it serves pulls (issue #179), else ``None``.

    What decides the host of every ``reference`` this API hands out: the Hub
    when ``cogs.serve.enabled``, the backing registry otherwise.
    """

    serving = get_cog_registry_serving(request)
    return serving.host if serving is not None else None


class CogRegistryNotServedError(LookupError):
    """This Hub does not serve pulls (``cogs.serve.enabled`` is off)."""


def require_registry_serving(request: Request) -> CogRegistryServing:
    serving = get_cog_registry_serving(request)
    if serving is None:
        raise CogRegistryNotServedError()
    return serving


AuthDep = Annotated[AuthContext | None, Depends(get_catalog_caller)]
RegistryHostDep = Annotated[str | None, Depends(get_registry_host)]
ServingDep = Annotated[CogRegistryServing, Depends(require_registry_serving)]
HubAuthDep = Annotated[AuthContext, Depends(get_auth_context)]
ListingAuthDep = Annotated[AuthContext | None, Depends(get_listing_caller)]
CatalogDep = Annotated[CogCatalogStore, Depends(get_cog_catalog_store)]

DIGEST_PATTERN = r"^sha256:[a-f0-9]{64}$"
"""The only digest form the indexer stores (the OCI client accepts sha256 alone)."""

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200
FILTER_MAX_LENGTH = 512

CogIdPath = Annotated[str, Path(description="The Cog id, `<publisher>/<name>`; its `/` is part of the path.")]
DigestPath = Annotated[str, Path(pattern=DIGEST_PATTERN, description="`sha256:` followed by 64 lowercase hex digits.")]


def _filter(description: str) -> object:
    return Query(min_length=1, max_length=FILTER_MAX_LENGTH, description=description)


NOT_FOUND = {404: {"model": CogErrorResponse, "description": "No such Cog, or no such version of it."}}
UNAVAILABLE = {503: {"model": CogErrorResponse, "description": "No catalog storage is configured."}}


class CogNotFoundError(LookupError):
    """No current version of this Cog is indexed."""


class CogVersionNotFoundError(LookupError):
    """This digest is not indexed as a version of this Cog."""


class CogPublishForbiddenError(PermissionError):
    """The caller does not hold the publish permission."""


class CogRegistryCredentialNotFoundError(LookupError):
    """No live registry credential with this id belongs to the caller."""


def _location_key(row: CogArtifact) -> tuple:
    # Present before removed, then newest pushed (unknown last), then most
    # recently indexed, then a stable tiebreak -- the catalog's own order.
    pushed = row.pushed_at.timestamp() if row.pushed_at else 0.0
    indexed = row.indexed_at.timestamp() if row.indexed_at else 0.0
    return (row.removed_at is not None, row.pushed_at is None, -pushed, -indexed, row.source_id, row.repository)


def _version_locations(store: CogCatalogStore, cog_id: str, digest: str) -> list[CogArtifact]:
    """Every indexed location of ``digest`` as a version of ``cog_id``, preferred first; 404 when none."""

    rows = [row for row in store.locations(digest) if row.status == STATUS_INDEXED and row.cog_id == cog_id]
    if not rows:
        raise CogVersionNotFoundError(cog_id)
    return sorted(rows, key=_location_key)


@router.get(
    "",
    response_model=CogListPage,
    responses=UNAVAILABLE,
    summary="List current Cogs",
)
def list_cogs(
    request: Request,
    _auth: ListingAuthDep,
    store: CatalogDep,
    registry_host: RegistryHostDep,
    kind: Annotated[str | None, _filter("The card's `kind` (e.g. `complete`, `model`, `context`).")] = None,
    publisher: Annotated[str | None, _filter("The card's `publisher`, exactly.")] = None,
    provides: Annotated[str | None, _filter("An entry of the card's `provides`.")] = None,
    requires: Annotated[str | None, _filter("A capability named in the card's `requires`.")] = None,
    accepts: Annotated[str | None, _filter("An io type in the card's `io.accepts`.")] = None,
    produces: Annotated[str | None, _filter("An io type in the card's `io.produces`.")] = None,
    q: Annotated[str | None, _filter("Case-insensitive substring of the Cog's name or description.")] = None,
    source_id: Annotated[
        str | None,
        _filter(
            "Only this registry source; the newest version is chosen within it. "
            "Refused (422) for anonymous callers, who never see source ids."
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JSONResponse:
    """One entry per `cog_id`: its newest present version, ordered by `cog_id`.

    Each item's card is **trimmed**: it omits `body`, `profile_raw` and
    `frontmatter_raw`, which `GET /v1/cogs/{cog_id}/versions/{digest}` serves
    (and `.../cog.md` the body).

    `source_id` scopes which versions are considered; every other filter
    tests that newest version, so without `source_id` an entry is always the
    version `GET /v1/cogs/{cog_id}` serves (with it, the newest in that source,
    which may be older). `q` matches `name` or a string `description`,
    case-insensitively for ASCII. Removed artifacts, non-Cog artifacts and
    failed reads never appear.
    """

    # An anonymous source_id never gets here: get_listing_caller refused it.
    filters = CatalogFilter(
        kind=kind,
        publisher=publisher,
        source_id=source_id,
        provides=provides,
        requires=requires,
        accepts=accepts,
        produces=produces,
        q=q,
    )
    # One row past the page says whether another page exists, without a
    # count query and without an empty trailing request.
    rows = store.list_current(filters, limit=limit + 1, offset=offset)
    page = CogListPage(
        items=[CogListEntry.of(row, registry_host) for row in rows[:limit]],
        limit=limit,
        offset=offset,
        next_offset=offset + limit if len(rows) > limit else None,
    )
    return Redaction.for_caller(_auth, request).respond(page)


@router.get(
    "/catalog.v1.json",
    response_model=CatalogV1,
    responses=UNAVAILABLE,
    summary="Transitional catalog.v1.json view",
)
def catalog_v1(_auth: AuthDep, store: CatalogDep) -> CatalogV1:
    """**Transitional.** The index in the static `catalog.v1.json` shape, for clients that still read it.

    One entry per repository path holding a present, indexed Cog, sorted and
    deduplicated across sources: `namespace` is the first path segment,
    `name` the rest, `description` the newest version's card description.
    The shape carries no registry host and no digest; new clients use
    `GET /v1/cogs`. A single-segment repository path has no namespace and is
    left out.
    """

    repositories = []
    for row in store.list_repositories():
        namespace, _, name = row.repository.partition("/")
        if not name:
            continue
        description = (row.card or {}).get("description")
        repositories.append(
            CatalogV1Repository(
                namespace=namespace,
                name=name,
                description=description if isinstance(description, str) else "",
            )
        )
    return CatalogV1(repositories=repositories)


NOT_SERVED = {404: {"model": CogErrorResponse, "description": "This Hub does not serve pulls itself."}}


def _pull_only_refusal(body: object) -> RequestValidationError | None:
    """How the pull-only contract refuses this body, or ``None`` if it accepts it."""

    try:
        PullOnlyRegistryCredentialRequest.model_validate(body)
    except ValidationError as exc:
        return RequestValidationError(
            [{**error, "loc": ("body", *error["loc"])} for error in exc.errors(include_url=False)], body=body
        )
    return None


class _ExchangeRoute(APIRoute):
    """The exchange route, validating as a Hub that accepts no publishes always has, when it accepts none.

    The request model names ``publish`` as a scope because some Hubs accept
    it. One with no publish source must answer every malformed request with
    the pull-only contract's own validation errors -- the scope it
    advertises, the errors it lists and their order -- so a body the
    request model refused is validated again against that contract, and its
    errors are the answer.
    """

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def exchange(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                serving = getattr(request.app.state, "cog_registry_serving", None)
                if serving is None or serving.publisher is not None or not isinstance(exc.body, dict):
                    raise
                raise (_pull_only_refusal(exc.body) or exc) from None

        return exchange


def create_registry_credential(
    request: Request, auth: HubAuthDep, serving: ServingDep, body: RegistryCredentialRequest | None = None
) -> RegistryCredentialResponse:
    """A username and secret for the Hub's own registry (`/v2/`), valid nowhere else.

    What a client hands to `nebi registry add` / `docker login` / `oras
    login` in place of its Hub token: pull-only, short-lived, revocable, and
    refused by every Hub API. The secret is returned once and stored only as
    a digest. Exchange a fresh one before each install and delete the
    caller's credentials at sign-out.

    404 `cog_registry_not_served` when this Hub does not serve pulls
    (`cogs.serve.enabled` is off): pull from the `reference` the catalog
    gives, with whatever credential that registry takes.

    `{"scope": "publish"}` asks for a credential that may also push
    (`nebi publish`, `oras push`). On a Hub that accepts no publishes
    `publish` is not a scope at all: 422, exactly as any unknown scope. 403
    `cog_publish_forbidden` when the caller does not hold the publish
    permission. Holding the credential is
    not the permission: it is checked again, with the repository's
    ownership, on every push.
    """

    scope = (body or RegistryCredentialRequest()).scope
    if scope == SCOPE_PUBLISH:
        if serving.publisher is None:
            # No publish source: this Hub's contract is the pull-only one,
            # and the refusal is that contract's own validation error.
            raise _pull_only_refusal({"scope": scope})
        if not serving.publisher.policy.permits(auth.user, auth.org_role, auth.platform_role):
            raise CogPublishForbiddenError()
    secret = new_credential_secret()
    credential = serving.credentials.create_credential(
        credential_id=new_credential_id(),
        user_id=auth.user,
        secret_hash=secret_digest(secret),
        scope=scope,
        session_id=session_id_of(request),
        ttl_seconds=serving.credential_ttl_seconds,
        # The organization a publish is attributed to where there is no
        # membership table to re-read; None for an operator with none.
        org_id=auth.home_org_id,
    )
    return RegistryCredentialResponse(
        id=credential.id,
        registry=serving.host,
        username=credential.id,
        secret=secret,
        scope=scope,
        expires_at=credential.expires_at,
    )


router.add_api_route(
    "/registry-credentials",
    create_registry_credential,
    methods=["POST"],
    response_model=RegistryCredentialResponse,
    status_code=status.HTTP_201_CREATED,
    responses={**NOT_SERVED, **UNAVAILABLE},
    summary="Exchange the Hub session for a registry credential",
    route_class_override=_ExchangeRoute,
)


@router.delete(
    "/registry-credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**NOT_FOUND, **UNAVAILABLE},
    summary="Revoke one registry credential",
)
def revoke_registry_credential(
    auth: HubAuthDep,
    serving: ServingDep,
    credential_id: Annotated[str, Path(pattern=CREDENTIAL_ID_PATTERN)],
) -> Response:
    """Revoke one of the caller's credentials; the pull tokens minted from it stop working at once.

    404 `cog_registry_credential_not_found` for an id that does not exist,
    has expired, or belongs to someone else -- the three are not told apart.
    """

    if not serving.credentials.revoke_credential(credential_id, auth.user):
        raise CogRegistryCredentialNotFoundError(credential_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/registry-credentials",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**NOT_SERVED, **UNAVAILABLE},
    summary="Revoke every registry credential of the caller",
)
def revoke_registry_credentials(auth: HubAuthDep, serving: ServingDep) -> Response:
    """Revoke all of the caller's credentials and pull tokens. What a client calls at sign-out; idempotent."""

    serving.credentials.revoke_all(auth.user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{cog_id:path}/versions/{digest}/cog.md",
    response_class=PlainTextResponse,
    responses={
        200: {"content": {"text/markdown": {"schema": {"type": "string"}}}, "description": "The COG.md body."},
        **NOT_FOUND,
        **UNAVAILABLE,
    },
    summary="COG.md body of one version",
)
def get_cog_md(_auth: AuthDep, store: CatalogDep, cog_id: CogIdPath, digest: DigestPath) -> PlainTextResponse:
    """The Markdown body of the version's `COG.md` (after its frontmatter), as captured at index time.

    Never fetched live. The frontmatter itself is on the card, parsed
    (`frontmatter`) and verbatim (`frontmatter_raw`).
    """

    card = _version_locations(store, cog_id, digest)[0].card or {}
    body = card.get("body")
    return PlainTextResponse(body if isinstance(body, str) else "", media_type="text/markdown; charset=utf-8")


@router.get(
    "/{cog_id:path}/versions/{digest}/reference",
    response_model=CogReference,
    responses={**NOT_FOUND, **UNAVAILABLE},
    summary="Pinned install reference of one version",
)
def get_cog_reference(
    _auth: AuthDep, store: CatalogDep, registry_host: RegistryHostDep, cog_id: CogIdPath, digest: DigestPath
) -> JSONResponse:
    """`<host>/<repository>@<digest>`, ready for `nebi import`.

    When the digest was indexed in several sources or repositories, the
    newest-pushed present location is the `reference` and the others are
    listed under `locations`. A digest whose every location was removed is
    still answered (`present: false`): the artifact may already be gone from
    its registry.

    When the Hub serves pulls itself, every `reference` names the Hub's
    registry host, and a platform operator additionally gets
    `backing_reference` -- where the artifact is stored -- on the answer and
    on each location. Nobody else is told the backing registry's host.
    """

    preferred, *others = _version_locations(store, cog_id, digest)
    # The backing location is an operator's business only, and only worth
    # saying when `reference` no longer says it.
    backing = registry_host is not None and _auth is not None and _auth.platform_role == PLATFORM_ROLE_OPERATOR
    answer = CogReference(
        reference=client_reference(preferred, registry_host),
        backing_reference=preferred.reference if backing else None,
        source_id=preferred.source_id,
        repository=preferred.repository,
        digest=preferred.digest,
        present=preferred.present,
        locations=[CogLocation.of(row, registry_host, backing=backing) for row in others],
    )
    # Absent rather than null, so the answer of a Hub that does not serve
    # pulls is byte-for-byte what it was before the field existed.
    return Redaction.for_caller(_auth).respond(answer, omit=None if backing else CogReference.BACKING_EXCLUDE)


@router.get(
    "/{cog_id:path}/versions/{digest}",
    response_model=CogEntry,
    responses={**NOT_FOUND, **UNAVAILABLE},
    summary="Card of one version",
)
def get_cog_version(
    request: Request,
    _auth: AuthDep, store: CatalogDep, registry_host: RegistryHostDep, cog_id: CogIdPath, digest: DigestPath
) -> JSONResponse:
    """The exact card indexed for this digest -- what an install pins -- in full.

    Removed versions are served too, with `removed_at` set.
    """

    entry = CogEntry.of(_version_locations(store, cog_id, digest)[0], registry_host)
    return Redaction.for_caller(_auth, request).respond(entry)


@router.get(
    "/{cog_id:path}",
    response_model=CogDetail,
    responses={**NOT_FOUND, **UNAVAILABLE},
    summary="A Cog and its versions",
)
def get_cog(
    request: Request, _auth: AuthDep, store: CatalogDep, registry_host: RegistryHostDep, cog_id: CogIdPath
) -> JSONResponse:
    """The Cog's current full card (its newest present version) and every indexed version, newest first.

    404 when no version is present, even if removed ones are indexed: those
    stay reachable by digest.
    """

    versions = store.list_versions(cog_id, include_removed=True)
    current = next((row for row in versions if row.present), None)
    if current is None:
        raise CogNotFoundError(cog_id)
    detail = CogDetail(
        **CogEntry.of(current, registry_host).model_dump(),
        versions=[CogVersion.of(row, registry_host) for row in versions],
    )
    return Redaction.for_caller(_auth, request).respond(detail)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(CogNotFoundError)
    async def cog_not_found_handler(_request: Request, _exc: CogNotFoundError):
        return error_response(status.HTTP_404_NOT_FOUND, "cog_not_found", "Cog not found")

    @app.exception_handler(CogVersionNotFoundError)
    async def cog_version_not_found_handler(_request: Request, _exc: CogVersionNotFoundError):
        return error_response(status.HTTP_404_NOT_FOUND, "cog_version_not_found", "Cog version not found")

    @app.exception_handler(CogRegistryNotServedError)
    async def cog_registry_not_served_handler(_request: Request, _exc: CogRegistryNotServedError):
        return error_response(
            status.HTTP_404_NOT_FOUND, "cog_registry_not_served", "This Hub does not serve Cog pulls itself"
        )

    @app.exception_handler(CogPublishForbiddenError)
    async def cog_publish_forbidden_handler(_request: Request, _exc: CogPublishForbiddenError):
        return error_response(
            status.HTTP_403_FORBIDDEN, "cog_publish_forbidden", "This account may not publish Cogs"
        )

    @app.exception_handler(CogRegistryCredentialNotFoundError)
    async def cog_registry_credential_not_found_handler(_request: Request, _exc: CogRegistryCredentialNotFoundError):
        return error_response(
            status.HTTP_404_NOT_FOUND, "cog_registry_credential_not_found", "Registry credential not found"
        )

    @app.exception_handler(BudgetExhausted)
    async def cog_budget_exhausted_handler(_request: Request, _exc: BudgetExhausted):
        # A store call that ran out of time before it could reach the
        # database: a TimeoutError of this module's, not a psycopg error, so
        # the app's database handlers do not match it. The same answer as a
        # pool timeout, which is what it is. ``/v2`` maps it itself, into the
        # registry's error format, before it gets here.
        return error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            error_codes.DATABASE_UNAVAILABLE,
            "The frames database is currently unavailable",
        )

    @app.exception_handler(RegistryCredentialsUnavailableError)
    async def cog_registry_credentials_unavailable_handler(_request: Request, exc: RegistryCredentialsUnavailableError):
        return error_response(status.HTTP_503_SERVICE_UNAVAILABLE, "cog_catalog_unavailable", str(exc))

    @app.exception_handler(CogCatalogUnavailableError)
    async def cog_catalog_unavailable_handler(_request: Request, exc: CogCatalogUnavailableError):
        return error_response(status.HTTP_503_SERVICE_UNAVAILABLE, "cog_catalog_unavailable", str(exc))
