import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager, nullcontext, suppress
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from .cogs.indexer import INDEXER_SHUTDOWN_TIMEOUT_SECONDS
from .config import (
    BaseConfig,
    build_active_frame_store,
    build_audit_log,
    build_cog_catalog_store,
    build_cog_indexing,
    build_connector_store,
    build_frames_store,
    build_group_store,
    build_history_store,
    build_invitation_email_delivery,
    build_invitation_service,
    build_model_access,
    build_model_catalog,
    build_org_store,
    build_platform_role_admin,
    build_platform_role_sync,
    build_postgres_pools,
    build_service_access_granter,
    build_task_store,
    build_usage_store,
    build_user_directory_client,
    migrate_collab_schema,
    preflight_collab_schema,
)
from .frames import error_codes
from .frames.auth import (
    NoOrganizationError,
    current_auth_context,
    enforce_https_jwks_urls,
    get_auth_context,
    get_caller_identity,
)
from .frames.authorization import verify_protected_routes
from .frames.db import postgres_error_classes
from .frames.identity import enforce_single_issuer_for_pin, identity_pinned_to_sub
from .frames.mcp_server import create_mcp_server
from .frames.observability import RequestObservabilityMiddleware, configure_logging, metrics_response
from .frames.org_source import (
    ORG_SOURCE_ENV,
    ORG_SOURCE_MEMBERSHIP,
    ORG_SOURCE_SINGLE,
    enforce_membership_org_source_preconditions,
    org_source_is_single,
    org_source_resolves_membership,
    single_org_declaration,
)
from .frames.orgs import OrgsUnavailableError, UnavailableOrgStore
from .frames.store import ConcurrentFrameUpdateError
from .path_protection import PathProtectionMiddleware, api_path, request_path
from .routers import (
    admin,
    admin_api,
    admin_ui,
    cogs,
    connectors,
    frame_groups,
    frames,
    identity,
    invitations,
    invite,
    org_invitations,
    tasks,
    usage,
    user_directory,
    web,
)
from .web.authz import platform_role_source_name, verify_web_route_protection
from .web.guard import WebSessionGuardMiddleware
from .web.pages import WebSecurityHeadersMiddleware
from .web.surface import (
    WEB_SURFACE_PREFIXES,
    build_web_surface,
    enforce_web_surface_map_access,
    enforce_web_surface_preconditions,
)


def _api_path(path: str) -> bool:
    # Single definition, shared with the path-protection middleware so a 401 it
    # raises has the same body shape as one raised inside the router.
    return api_path(path)


def _unauthorized_response(request: Request, exc: HTTPException) -> Response:
    """Shape a middleware credential refusal exactly like the same one in a route.

    The middleware runs before routing, so the app's exception handlers never
    see it; clients must not be able to tell the two apart. An exception with
    its own ``error_code`` (``no_organization``) keeps the machine-readable
    envelope on every surface — that code is the whole content of the state.
    """

    code = getattr(exc, "error_code", None)
    if code or _api_path(request_path(request)):
        return frames.error_response(exc.status_code, code or error_codes.UNAUTHORIZED, str(exc.detail))
    return JSONResponse({"detail": str(exc.detail)}, status_code=exc.status_code)


def _database_error_classes() -> tuple[type[Exception], ...]:
    """psycopg's "database unavailable" exceptions, or none if psycopg is absent."""

    try:
        return postgres_error_classes()
    except ImportError:  # pragma: no cover - psycopg is a hard dependency
        return ()


def _unavailable_response(exc: Exception, database_errors: tuple[type[Exception], ...]) -> Response | None:
    """Map a failed credential check onto a 503, or ``None`` if it is not one.

    Only two things belong here, and both mean "the server cannot answer the
    membership question right now", which is emphatically not "this caller has
    no organization" and equally not "this caller is not signed in".
    """

    if isinstance(exc, OrgsUnavailableError):
        return frames.error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            error_codes.ORGANIZATIONS_UNAVAILABLE,
            "Organization storage is not configured",
        )
    if database_errors and isinstance(exc, database_errors):
        return frames.error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            error_codes.DATABASE_UNAVAILABLE,
            "The frames database is currently unavailable",
        )
    return None


class McpAuthMiddleware(BaseHTTPMiddleware):
    """Authenticate MCP traffic, which never reaches the outer app's handlers.

    The MCP app is mounted, so nothing registered on the outer FastAPI app
    applies to it: every outcome of the credential check has to be turned into
    a response right here. Responses use the same ``{"error": {"code", ...}}``
    envelope as the HTTP API, because a plain-text body would erase exactly the
    distinctions that matter — ``no_organization`` against a plain refusal, and
    an unavailable database against a server bug.
    """

    def __init__(self, app: FastAPI, authenticate: Callable[[Request], object]):
        super().__init__(app)
        self.authenticate = authenticate
        self._database_errors = _database_error_classes()

    async def dispatch(self, request: Request, call_next):
        token = None
        try:
            # Threadpool for the same reason as the path-protection middleware:
            # `authenticate` is synchronous and, once it resolves membership,
            # blocking. Inline it would hold the event loop for the whole pool
            # wait timeout during a database outage.
            auth_context = await run_in_threadpool(self.authenticate, request)
            token = current_auth_context.set(auth_context)
        except HTTPException as exc:
            code = getattr(exc, "error_code", None) or {
                status.HTTP_401_UNAUTHORIZED: error_codes.UNAUTHORIZED,
                status.HTTP_403_FORBIDDEN: error_codes.FORBIDDEN,
            }.get(exc.status_code, error_codes.HTTP_ERROR)
            return frames.error_response(exc.status_code, code, str(exc.detail))
        except Exception as exc:
            response = _unavailable_response(exc, self._database_errors)
            if response is None:
                raise
            return response
        try:
            return await call_next(request)
        finally:
            if token is not None:
                current_auth_context.reset(token)


logger = logging.getLogger("frames_server.core")


def _close_quietly(candidate: object) -> None:
    """Close something that may not be closeable, and never mask a shutdown.

    The granter seam is a Protocol: the disabled implementation owns no
    connections and has no ``close``, and a third implementation might not
    either. So the attribute is checked rather than assumed.

    Exceptions are swallowed deliberately. This runs in a ``finally`` during
    teardown, where raising would replace whatever actually ended the app --
    including a real error someone is trying to read -- with a failure to tidy
    up a socket.
    """

    close = getattr(candidate, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:  # pragma: no cover - teardown must not raise
        logger.warning("granter_close_failed", exc_info=True)


def make_app(config: BaseConfig) -> FastAPI:
    configure_logging()
    # Identity policy is settled before anything else starts: a mistyped
    # FRAMES_AUTH_IDENTITY_CLAIM must fail the rollout rather than 500 the first
    # authenticated request, and when the pin is on the set of trusted issuers
    # has to collapse to one (a bare 'sub' is unique only within an issuer).
    identity_pinned_to_sub()
    enforce_single_issuer_for_pin()
    # Same fail-fast contract for the JWKS URLs both verifiers fetch signing
    # keys from (issue #77): a cleartext http URL would let an on-path attacker
    # substitute the key set, so it fails the rollout here — visible in the
    # pod's events — rather than silently fetching keys over http.
    enforce_https_jwks_urls()
    # Same fail-fast contract for where the caller's organization comes from
    # (issue #63): a mistyped FRAMES_AUTH_ORG_SOURCE, membership resolution
    # without the identity pin it is keyed on, a leftover retired
    # FRAMES_AUTH_DEFAULT_* fallback, or an incomplete single-org declaration
    # (issue #91) all fail the rollout here rather than misresolving tenancy
    # on the first authenticated request.
    org_source_resolves_membership()
    enforce_membership_org_source_preconditions()
    # Same fail-fast contract for the browser web surface (issue #88): a
    # configured client id with a missing/incoherent realm, or a protection
    # map that would 401 the sign-in flow itself, fails the rollout here
    # rather than dead-ending every operator's browser.
    enforce_web_surface_preconditions(config)
    # One pool registry for every Postgres-backed store: stores sharing a URL
    # share a pool, opened at startup and closed at shutdown below.
    postgres_pools = build_postgres_pools(config)
    frames_store = build_frames_store(config)
    active_frame_store = build_active_frame_store(config, postgres_pools)
    history_store = build_history_store(config, postgres_pools)
    group_store = build_group_store(config, postgres_pools)
    invitation_email_delivery = build_invitation_email_delivery(config)
    invitation_service = build_invitation_service(config, postgres_pools)
    # #180: what an accepted invitation grants, and what can grant it. The
    # granter is disabled until a deployment is given membership authority
    # (an internal issue); the group list is empty unless a
    # deployment says otherwise, because nothing should grant service access by
    # default -- that is the shape of the behaviour this replaced.
    service_access_granter = build_service_access_granter(config)
    granted_service_groups = tuple(config.frames.service_access.grant_on_acceptance)
    user_directory_client = build_user_directory_client(config)
    usage_store = build_usage_store(config, postgres_pools)
    task_store = build_task_store(config, postgres_pools)
    org_store = build_org_store(config, postgres_pools)
    # After the store, because the reconcile writes through whichever one
    # this deployment actually has.
    platform_role_sync = build_platform_role_sync(config, postgres_pools, org_store)
    audit_log = build_audit_log(config, postgres_pools)
    platform_role_admin = build_platform_role_admin(config, postgres_pools)
    connector_store = build_connector_store(config, postgres_pools)
    model_catalog = build_model_catalog(config)
    model_access = build_model_access(config, postgres_pools)
    # The collab_ tenancy tables' store carries no DDL of its own, so their
    # migration is invoked here rather than falling out of a store's
    # construction the way the frames_server_ tables' DDL does. Same trigger
    # (frames.postgres.url + auto_migrate), same startup failure semantics.
    migrate_collab_schema(config, postgres_pools)
    # The Cog catalog (issue #84) rides the same collab_ migration (version
    # 7) and is always built so the catalog read API is up whether or not
    # this replica sweeps registries. The indexer -- and the registry
    # sources it owns -- exist only when cogs.index.enabled (issue #87).
    cog_catalog_store = build_cog_catalog_store(config, postgres_pools)
    cog_indexing = build_cog_indexing(config, cog_catalog_store)
    # The run API (/v1/runs) is behind a feature flag, and so is its import: it is the one
    # place the API uses the execution package, which an image need not ship until it is on.
    run_service = None
    if config.features.enabled("cog_runs"):
        from .routers import runs as runs_router

        run_service = runs_router.build_run_service(config.runs)
    if org_source_resolves_membership():
        # Third membership precondition (the env-only ones are checked above):
        # the organization store must have a real backend. Membership is an
        # authorization input, so a store that fails closed would 503 every
        # authenticated request for as long as the pod lived — refuse the
        # rollout instead of shipping an outage.
        if isinstance(org_store, UnavailableOrgStore):
            raise RuntimeError(
                f"{ORG_SOURCE_ENV}={ORG_SOURCE_MEMBERSHIP} (and ={ORG_SOURCE_SINGLE}) requires an"
                " organization store: set the shared frames.postgres URL (or"
                " frames.orgs.backend=memory for local development)."
            )
        # And the schema behind it must be new enough to serve (issue #96).
        # This is the first consumer of the collab_ tables, so it is the first
        # build that a version skew can actually break.
        preflight_collab_schema(config, postgres_pools)
    single_org = single_org_declaration()
    if single_org is not None:
        # The declaration, stated where an operator reads startup output: on
        # this deployment, any account arriving through one of these identity
        # providers becomes a member of this organization on its first
        # authenticated request — so the *providers'* membership policy is the
        # hub's access policy, and an unrestricted provider on this list is
        # visible here rather than inferred from behaviour. The organization
        # row itself is created on first admission, not here: startup
        # deliberately tolerates an unreachable database.
        logger.info(
            "single_org_mode_active",
            extra={
                "org": single_org.org_id,
                "org_name": single_org.org_name,
                "member_sources": sorted(single_org.member_sources),
            },
        )
    # Logged here rather than during config validation, which runs before
    # logging is configured.
    for flag in config.features.retired_names:
        logger.warning("feature_flag_retired_ignored", extra={"flag": flag})
    mcp = create_mcp_server(frames_store, active_store=active_frame_store)
    mcp_app = mcp.streamable_http_app()
    # MCP traffic authenticates through the same get_auth_context, which
    # records seen users and resolves membership off the owning app's state —
    # the mounted MCP app has its own state object, so give it both stores.
    mcp_app.state.usage_store = usage_store
    mcp_app.state.org_store = org_store

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        mcp_lifespan = mcp.session_manager.run() if config.frames.mcp_session_manager_enabled else nullcontext()
        async with mcp_lifespan:
            # Open without waiting: Postgres being down at startup must not
            # crash the app — requests fail with a 503 until it comes back.
            postgres_pools.open()
            app.state.postgres_pools = postgres_pools
            app.state.frames_store = frames_store
            app.state.active_frame_store = active_frame_store
            app.state.history_store = history_store
            app.state.group_store = group_store
            app.state.invitation_email_delivery = invitation_email_delivery
            app.state.invitation_service = invitation_service
            app.state.platform_role_sync = platform_role_sync
            app.state.audit_log = audit_log
            app.state.platform_role_admin = platform_role_admin
            app.state.connector_store = connector_store
            app.state.model_catalog = model_catalog
            app.state.model_access = model_access
            app.state.model_groups = dict(config.frames.model_access.model_groups)
            app.state.service_access_granter = service_access_granter
            app.state.granted_service_groups = granted_service_groups
            app.state.user_directory_client = user_directory_client
            app.state.usage_store = usage_store
            app.state.task_store = task_store
            app.state.org_store = org_store
            app.state.mcp_server = mcp
            app.state.connectors_config = config.connectors
            app.state.features = config.features
            app.state.run_service = run_service
            # Process-wide bound on concurrent generic GitHub reads (api_get).
            # Created once here, where the sizing config is in hand and we're
            # already inside the event loop — so the route needs no lazy
            # get-or-create dance and its no-await-between invariant disappears.
            app.state.github_api_get_semaphore = asyncio.Semaphore(
                config.connectors.github.api_get_max_concurrency
            )
            # Observe-only gauge of how many requests are in the acquire phase
            # (queued for a permit). A plain int is safe here: asyncio is
            # single-threaded and the route never awaits between reading and
            # mutating it. Logged in the throttle event so a deferred queue-length
            # cap (max_waiting) stays a monitored deferral, not a blind one.
            app.state.github_api_get_waiters = 0
            # For the catalog API (#85) and the webhook receiver (#86):
            # the store is always there; the indexer and its sources only
            # on a sweeping replica.
            app.state.cog_catalog_store = cog_catalog_store
            app.state.cog_indexer = cog_indexing.indexer if cog_indexing is not None else None
            app.state.cog_registry_sources = cog_indexing.indexer.sources if cog_indexing is not None else []
            cog_index_task: asyncio.Task | None = None
            if cog_indexing is not None:
                # After the migration (which ran in make_app) and after the
                # pools opened: the first sweep may be immediate. The loop
                # jitters its interval so replicas drift apart, and the
                # store's advisory lock keeps them from sweeping at once.
                cog_index_task = asyncio.create_task(
                    cog_indexing.indexer.run(
                        interval_seconds=cog_indexing.interval_seconds,
                        run_on_startup=cog_indexing.run_on_startup,
                    ),
                    name="cog-index",
                )
            if config.web.enabled:
                # Again at boot, and this is the **last** time either check
                # runs. make_app verifies what *it* registered; this sees
                # anything registered between make_app returning and the
                # server accepting traffic, which is where a page router
                # added by a caller of make_app lands. Raising here fails the
                # deploy rather than the first request.
                #
                # A route registered *after* this point is not rechecked, and
                # that is a deliberate boundary rather than an oversight — see
                # `verify_web_route_protection`, which documents what these
                # checks are and, more importantly, what they are not.
                verify_web_route_protection(app.routes)
                enforce_web_surface_map_access(app.routes, config)
            try:
                yield
            finally:
                # First, before the pools close: a sweep in flight may hold
                # the lock connection and be mid-write. Cancel, wait for it
                # to unwind (its finally releases the lock), then close the
                # registry clients it was talking to.
                if cog_index_task is not None:
                    # Bounded: the indexer's drain already has a deadline, and
                    # this is the outer wall -- a database that stopped
                    # answering must not be able to hang app shutdown.
                    # asyncio.wait, not wait_for: wait_for cancels the task
                    # again on timeout and then AWAITS it, and the indexer
                    # defers repeated cancellations until its worker finishes
                    # -- which is the wait this deadline exists to bound.
                    # Past the deadline the task stays pending and dies with
                    # the process; any lock it still holds is the server's to
                    # release with the connection. A reference is kept below
                    # for as long as this frame lives so it is not finalized
                    # while pending.
                    cog_index_task.cancel()
                    _, still_pending = await asyncio.wait({cog_index_task}, timeout=INDEXER_SHUTDOWN_TIMEOUT_SECONDS)
                    if still_pending:
                        # Deliberately NOT closing the indexer's executor here:
                        # the abandoned task may yet come back and need to
                        # submit its lock release, and a shut-down executor
                        # would turn that into an exception -- losing the
                        # unlock this whole path exists to protect. Close it
                        # when the task does finish, whenever that is, so a
                        # process that outlives this lifespan (a reload, a
                        # test holding the app) does not keep idle workers.
                        if cog_indexing is not None:
                            indexer = cog_indexing.indexer
                            cog_index_task.add_done_callback(lambda _task: indexer.close())
                        logger.error(
                            "cog_indexer_shutdown_abandoned",
                            extra={"timeout_seconds": INDEXER_SHUTDOWN_TIMEOUT_SECONDS},
                        )
                    else:
                        if cog_indexing is not None and cog_indexing.indexer.pending_late_releases:
                            logger.warning(
                                "cog_indexer_shutdown_with_late_release_pending",
                                extra={"pending": cog_indexing.indexer.pending_late_releases},
                            )
                        if cog_indexing is not None:
                            # The task is done, so no further store calls are
                            # coming: stop accepting them. Work already on a
                            # thread -- including a hand-off waiting to
                            # release the lock -- still finishes.
                            cog_indexing.indexer.close()
                if cog_indexing is not None:
                    for source in cog_indexing.indexer.sources:
                        with suppress(Exception):
                            await source.aclose()
                user_directory_client.close()
                # Same reason as the line above: this granter owns an
                # `httpx.Client`, so its connection pool outlives the app
                # unless it is closed here. Raised in review of #183 --
                # a reload that leaks a pool each time is the kind of thing
                # that only shows up as slow attrition.
                _close_quietly(service_access_granter)
                # Before the pools: the usage store's background writer may be
                # holding one of their connections, and its close is bounded.
                usage_store.close()
                postgres_pools.close()

    app = FastAPI(
        root_path=config.server.root_path,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # Added first, so it sits *inside* the observability middleware: a request
    # rejected here still gets a request id, a log line, and a metrics sample.
    # CORS is added after it and therefore stays outside both credential
    # checks, so browser preflights are answered before either runs. It is no
    # longer the *outermost* layer on a web-enabled deployment —
    # WebSecurityHeadersMiddleware is added after CORS below, so that the
    # surface's headers reach responses CORS itself produces — and the comment
    # said otherwise for long enough that it is worth being precise: the
    # ordering CORS depends on is "outside the credential checks", which holds
    # either way.
    database_errors = _database_error_classes()

    def _authenticate_error_response(_request: Request, exc: Exception) -> Response | None:
        # This middleware is outside Starlette's ExceptionMiddleware, so the
        # handlers registered further down never see what it raises. Without
        # this, a membership lookup against an unreachable database would
        # answer 500 on every protected path while the routes that do the same
        # lookup answer 503.
        return _unavailable_response(exc, database_errors)

    def _authenticate(request: Request) -> object:
        """Authenticate at the level the requested path actually needs.

        The protection map has two states, public and authenticated, and
        "authenticated" has meant "resolves to a full membership context"
        everywhere until now. Invitation acceptance breaks that: its whole
        audience is logins with no membership, which membership-mode
        resolution refuses with ``no_organization``. This middleware runs
        *before* routing, so on a hardened deployment (``default_access:
        authenticated``) every invitee would be turned away here, before the
        route that exists for them was ever reached.

        The accept path therefore authenticates at identity level — same
        credentials, same verification, same 401s, no membership lookup — and
        every other path is unchanged. The exemption is from the *membership*
        requirement only, never from authentication, and the route's own
        dependency asks for the same thing, so nothing depends on the
        middleware having run.
        """

        if invitations.identity_only_path(request_path(request)):
            return get_caller_identity(request)
        return get_auth_context(request)

    # The same map, readable by a route that honors a `public` entry itself
    # (the Cog catalog's anonymous discovery, issue #85).
    app.state.path_rules = tuple(config.security.paths)
    app.add_middleware(
        PathProtectionMiddleware,
        rules=config.security.paths,
        default_access=config.security.default_access,
        authenticate=_authenticate,
        unauthorized_response=_unauthorized_response,
        authenticate_error_response=_authenticate_error_response,
    )

    if config.web.enabled:
        # Between path protection and observability, so that a session
        # refusal is *inside* the observability middleware and therefore gets
        # a request id, an access-log line, and a metrics sample. A denial is
        # exactly the event an operator needs to see, and the first
        # arrangement of this put it outside and made denials invisible.
        app.add_middleware(WebSessionGuardMiddleware, prefixes=WEB_SURFACE_PREFIXES)

    app.add_middleware(RequestObservabilityMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.security.cors.allowed_origins,
        allow_headers=config.security.cors.allowed_headers,
        allow_credentials=config.security.cors.allow_credentials,
        allow_methods=["*"],
    )

    if config.web.enabled:
        # Outermost, so the browser surface's security headers reach every
        # response on its paths — including ones no handler of that surface
        # produced: redirects, 405s, the path-protection middleware's own
        # refusals, the guard's own sign-in redirects, an exception escaping a
        # page, and whatever the mounted MCP catch-all answers for an
        # unmatched /web path (issue #86).
        app.add_middleware(WebSecurityHeadersMiddleware, prefixes=WEB_SURFACE_PREFIXES)

    @app.get("/", include_in_schema=False)
    async def home() -> HTMLResponse:
        # The hub landing-page tile points at this origin, so the root serves
        # a small directory of the browser-facing pages instead of jumping
        # straight to the API docs.
        return HTMLResponse(
            """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Collab Hub Frames</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
         Helvetica, Arial, sans-serif; margin: 4rem auto; max-width: 40rem;
         padding: 0 1rem; color: #1a1a2e; }
  h1 { font-size: 1.5rem; }
  ul { line-height: 2; }
  a { color: #3452d9; }
</style>
</head>
<body>
<h1>Collab Hub Frames</h1>
<p>Hub-side intelligence services for Frames and chat.</p>
<ul>
  <li><a href="./usage">Hub usage dashboard</a> — activity across this workspace</li>
  <li><a href="./docs">API documentation</a></li>
</ul>
</body>
</html>"""
        )

    @app.get("/docs", include_in_schema=False)
    async def protected_docs(_auth=Depends(get_auth_context)) -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=f"{app.root_path}/openapi.json",
            title=f"{app.title} - Swagger UI",
        )

    @app.get("/redoc", include_in_schema=False)
    async def protected_redoc(_auth=Depends(get_auth_context)) -> HTMLResponse:
        return get_redoc_html(
            openapi_url=f"{app.root_path}/openapi.json",
            title=f"{app.title} - ReDoc",
        )

    @app.get("/openapi.json", include_in_schema=False)
    async def protected_openapi(_auth=Depends(get_auth_context)) -> JSONResponse:
        return JSONResponse(app.openapi())

    @app.get("/health")
    async def health() -> Response:
        return Response(b"", status_code=status.HTTP_200_OK)

    @app.get("/health/db", include_in_schema=False)
    def health_db() -> JSONResponse:
        # Deliberately separate from /health, which stays a pure liveness
        # probe: a Postgres outage must surface here without making
        # Kubernetes restart otherwise-healthy pods. Sync `def` so the
        # blocking SELECT 1 round-trips run in the threadpool.
        if not postgres_pools.configured:
            return JSONResponse({"status": "not_configured"})
        checks = postgres_pools.check()
        healthy = all(entry["status"] == "ok" for entry in checks)
        return JSONResponse(
            {"status": "ok" if healthy else "unavailable", "pools": checks},
            status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return metrics_response()

    @app.exception_handler(HTTPException)
    async def frames_http_exception_handler(request: Request, exc: HTTPException):
        if not _api_path(request.url.path):
            return await http_exception_handler(request, exc)
        code = {
            status.HTTP_401_UNAUTHORIZED: error_codes.UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN: error_codes.FORBIDDEN,
            status.HTTP_404_NOT_FOUND: error_codes.NOT_FOUND,
        }.get(exc.status_code, error_codes.HTTP_ERROR)
        return frames.error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(NoOrganizationError)
    async def no_organization_handler(_request: Request, exc: NoOrganizationError):
        # Registered against the subclass so Starlette's MRO lookup prefers it
        # over the generic HTTPException handler above. Deliberately not gated
        # on _api_path(): the machine-readable code is the entire content of
        # this state, and the connector routes (which are not API paths and
        # keep FastAPI's {"detail": ...} shape for their own errors) are just
        # as authenticated as the rest.
        return frames.error_response(exc.status_code, error_codes.NO_ORGANIZATION, str(exc.detail))

    @app.exception_handler(OrgsUnavailableError)
    async def orgs_unavailable_handler(_request: Request, _exc: OrgsUnavailableError):
        # No organization backend means deny, never a quiet fall-through to
        # claims or defaults. make_app refuses to start membership resolution
        # without a backend, so reaching this at runtime means app state was
        # assembled some other way.
        return frames.error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            error_codes.ORGANIZATIONS_UNAVAILABLE,
            "Organization storage is not configured",
        )

    @app.exception_handler(RequestValidationError)
    async def frames_validation_exception_handler(request: Request, exc: RequestValidationError):
        # Pydantic v2 puts the *rejected value* in each error's `input`, so a
        # 422 normally reflects the request body back to the caller — and to
        # anything that logs response bodies. On the invitation accept route
        # that value is an almost-valid one-time secret (R3).
        #
        # Decided before the API-path branch, and never delegated to FastAPI's
        # default handler, which would echo the same value in its own
        # {"detail": [...]} shape. A deployment behind a proxy that does not
        # strip its prefix has a path this app's own API-path test does not
        # recognise, so "redact" has to be the outer decision rather than a
        # refinement of the enveloped branch.
        redact = invitations.redact_validation_details(request.url.path)
        if not redact and not _api_path(request.url.path):
            return await request_validation_exception_handler(request, exc)
        return frames.error_response(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            "Request validation failed",
            details=None if redact else jsonable_encoder(exc.errors()),
        )

    @app.exception_handler(ConcurrentFrameUpdateError)
    async def concurrent_frame_update_handler(_request: Request, exc: ConcurrentFrameUpdateError):
        return frames.error_response(
            status.HTTP_409_CONFLICT,
            error_codes.FRAME_UPDATE_CONFLICT,
            str(exc),
        )

    # Postgres unreachable, or the pool exhausted past its wait timeout, both
    # surface as psycopg errors (PoolTimeout subclasses OperationalError) — an
    # unavailable database, not a server bug.
    #
    # Registered whenever psycopg is importable rather than only when this
    # app's own pool registry happens to be populated. The old condition tied
    # an authorization-critical mapping to which store had been wired first: an
    # authenticated request that fails closed on a database error must answer
    # 503 regardless of *whose* pool raised. With no Postgres-backed store
    # configured, nothing can raise these and the handlers are inert.
    if database_errors:

        async def database_unavailable_handler(_request: Request, _exc: Exception) -> JSONResponse:
            return frames.error_response(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                error_codes.DATABASE_UNAVAILABLE,
                "The frames database is currently unavailable",
            )

        for database_error in database_errors:
            app.add_exception_handler(database_error, database_unavailable_handler)

    frames.register_exception_handlers(app)
    frame_groups.register_exception_handlers(app)
    user_directory.register_exception_handlers(app)
    usage.register_exception_handlers(app)
    invitations.register_exception_handlers(app)
    cogs.register_exception_handlers(app)
    app.include_router(frames.router, prefix="/v1")
    app.include_router(user_directory.router, prefix="/v1")
    app.include_router(frames.router, include_in_schema=False)
    app.include_router(frame_groups.router, prefix="/v1")
    app.include_router(frame_groups.router, include_in_schema=False)
    app.include_router(connectors.router, prefix="/v1")
    app.include_router(usage.router, prefix="/v1")
    app.include_router(usage.router, include_in_schema=False)
    app.include_router(usage.page_router)
    app.include_router(tasks.router, prefix="/v1")
    app.include_router(tasks.devices_router, prefix="/v1")
    app.include_router(tasks.notifications_router, prefix="/v1")
    app.include_router(tasks.runs_router, prefix="/v1")
    # The Cog catalog read API (#85). /v1 only: it post-dates the unprefixed
    # legacy mounts, so there is no old client to keep answering.
    app.include_router(cogs.router, prefix="/v1")
    if run_service is not None:
        app.include_router(runs_router.router, prefix="/v1")
    # Who is calling, and how the collab-hub CLI signs in. /auth/cli is public:
    # the hardened map lists it, since a client asks it before it holds a token.
    app.include_router(identity.router, prefix="/v1")

    if config.web.enabled:
        # The browser surface (issue #88): session sign-in and the page
        # scaffolding the invitation pages build on. Its routes authenticate
        # themselves with the web session cookie — the protection map must
        # leave /web public, which the precondition check above enforced.
        web_surface = build_web_surface(config)
        app.state.web_surface = web_surface
        web.register_exception_handlers(app)
        # The invitation-acceptance page (issue #90) rides on that surface as
        # two routes: the page itself, which must render for a browser with no
        # account yet, and its redemption POST, which must not. They go
        # through the two seams make_router exposes, so the public one still
        # costs the reviewed entry in PUBLIC_WEB_PATHS — make_router refuses
        # a public page route that is missing it.
        invite_public, invite_gated = invite.make_routers(
            memberships_enabled=org_source_resolves_membership(),
            require_verified_email=config.frames.invitations.require_verified_email,
        )
        # The operator invitation page (issue #91). Mounted only where
        # invitations can mean anything, for the same reason #89's API router
        # is: on a claims-sourced deployment the platform-role axis is
        # structurally None, so every operator would be refused, and an
        # invitation accepted there would write membership rows the
        # authentication choke point never reads. A page that is absent is a
        # truer answer than one that refuses everyone without saying why.
        page_routers = [invite_gated]
        if org_source_resolves_membership():
            if not org_source_is_single():
                # Under the single-organization source (issue #91) the page is
                # not mounted, by the same "absent is truer than broken" rule:
                # its one send is the org-creating invitation ("org_id=None is
                # not a parameter and never will be on this surface"), which
                # the invitation service refuses on a deployment that declares
                # exactly one organization. Operators keep the /v1 operator
                # routes for listing/revoking, and invitations *into* the
                # declared organization — how a single-org hub grants `owner`
                # — go through the owner page and API as usual.
                page_routers.append(admin.make_router())
            # The panel's JSON. Mounted on the outer condition rather than
            # beside the invitation page: everything it manages — models,
            # roles, connectors, usage, the audit log — is just as meaningful
            # on a single-organization hub. Only the org-creating invitation
            # page is particular to the multi-organization source.
            page_routers.append(admin_api.make_router())
            # The built panel, when this deployment ships one. Checked here
            # rather than inside the router so that a deployment without a
            # build mounts nothing at all.
            admin_ui_dist = _built_admin_ui(config)
            if admin_ui_dist is not None:
                page_routers.append(admin_ui.make_router(admin_ui_dist))
            # The owner invitation page (issue #142), same mounting rule and
            # for the same reason: on a claims-sourced deployment the org-role
            # axis is structurally None, so every owner would be refused, and
            # an invitation accepted there would write membership rows the
            # authentication choke point never reads.
            page_routers.append(org_invitations.make_router())
        app.include_router(
            web.make_router(
                web_surface,
                page_routers=page_routers,
                public_page_routers=[invite_public],
            )
        )
        # Operator authority is issue #87's `collab_platform_roles`, read
        # through `OrgStore.resolve_principal`. If that wiring is ever
        # missing, every operator is locked out — so say so at startup rather
        # than leaving it to be discovered as an unexplained refusal. The
        # request path fails loudly too (WebAuthorizationUnavailable), and
        # never grants: this is a signal, not the enforcement.
        # The other half of "can an operator ever be recognized here", and the
        # half that is invisible from inside the app. The session principal is
        # `user_from_claims`, which honours FRAMES_AUTH_IDENTITY_CLAIM; with
        # the pin off it resolves preferred_username, then email, then sub.
        # `collab_platform_roles.user_id` is specified as the OIDC `sub` (Gate
        # E), and bootstrapping an operator is a hand-inserted sub row. So on a
        # legacy deployment the row an operator inserts does not match the
        # principal the session carries, `require_operator` refuses, and it
        # renders as the ordinary "you do not hold this role" page — correct,
        # fail-closed, and about the hardest misconfiguration there is to
        # diagnose from a browser.
        #
        # Logged rather than refused, deliberately. The pin is an app-wide
        # setting governing every ACL principal in the Frames API, and
        # `frames.identity` documents that leaving it unset keeps an existing
        # deployment unaffected until it opts in; making the browser surface
        # demand it would force that migration as a side effect of enabling a
        # web page, and would take down sign-in, the acceptance page and the
        # owner pages — none of which depend on it — over a condition that
        # grants nobody anything. Same shape as the role-source signal below:
        # a loud startup line and an inspectable state attribute, with the
        # request path failing closed on its own.
        app.state.web_identity_pinned_to_sub = identity_pinned_to_sub()
        if not app.state.web_identity_pinned_to_sub:
            logging.getLogger("frames_server.web").warning(
                "web_identity_not_pinned_to_sub",
                extra={
                    "detail": (
                        "the web surface is enabled with FRAMES_AUTH_IDENTITY_CLAIM unset"
                        " (legacy precedence: preferred_username, email, sub), so the"
                        " session principal is a renameable string while"
                        " collab_platform_roles.user_id is the OIDC sub; an operator row"
                        " keyed on sub will not match and operator pages will refuse."
                        " Set FRAMES_AUTH_IDENTITY_CLAIM=sub, or key the role row on"
                        " whatever this deployment's precedence actually resolves"
                    )
                },
            )
        app.state.web_platform_role_source = platform_role_source_name(org_store)
        if app.state.web_platform_role_source is None:
            logging.getLogger("frames_server.web").error(
                "web_platform_role_source_missing",
                extra={
                    "org_store": type(org_store).__name__,
                    "detail": (
                        "the web surface is enabled but no platform-role source is available;"
                        " operator-only pages will answer 503 until OrgStore.resolve_principal"
                        " (issue #87) is wired in"
                    ),
                },
            )

    if org_source_resolves_membership():
        # Mounted only where it can mean anything. Invitations write
        # `collab_org_members`, which claims-sourced auth never reads, so a
        # claims-mode acceptance would report success and grant nothing —
        # and both authorization axes read `collab_` roles that are
        # structurally None there, so the management surface would 403
        # everyone anyway. Absent is a truer answer than broken.
        app.include_router(invitations.router, prefix="/v1")

    # #87 built this and left the wiring to its consumer. Decorators apply
    # bottom-up, so an authorization guard written *above* a route decorator
    # wraps an object the route no longer holds and enforces nothing — a
    # silently open privileged route. This turns that mistake into a pod that
    # will not start.
    #
    # The router is checked directly as well as through the app, because on a
    # claims-sourced deployment it is not mounted: a misordered decorator is a
    # code defect, and it must not be a defect that only the deployments which
    # happen to mount the router can discover. Runs after every mount —
    # including the web surface above — so the app-level pass covers the
    # whole route table.
    verify_protected_routes(invitations.router)
    verify_protected_routes(app)

    mcp_app.add_middleware(McpAuthMiddleware, authenticate=get_auth_context)
    app.mount("/", mcp_app)

    if config.web.enabled:
        # Everything this function registers has now been registered, so this
        # is the earliest complete picture. The lifespan repeats it for
        # anything added after make_app returns.
        verify_web_route_protection(app.routes)
        # And the other half of the same question. The precondition above ran
        # on config alone and could only name the /web paths it was written
        # with; this one reads the route table, so every prefix the surface
        # actually serves is checked against the protection map whether or not
        # anyone remembered to list it.
        enforce_web_surface_map_access(app.routes, config)

    return app


def _built_admin_ui(config) -> Path | None:
    """The panel's built directory, or ``None`` if this build has no panel.

    A configured path whose ``index.html`` is missing counts as no panel. That
    is the failure a broken Docker stage produces, and answering it with the
    pre-panel behaviour is both truthful and harmless; a startup refusal would
    take down sign-in and the invitation pages over a front-end build.
    """

    configured = config.web.admin_ui_dist
    if not configured:
        return None
    dist = Path(configured)
    if not (dist / "index.html").is_file():
        logger.warning("admin_ui_dist_missing_index", extra={"path": str(dist)})
        return None
    return dist
