"""Routes for the browser web surface (issue #88): session sign-in and scaffolding.

Mounted by ``make_app`` only when a confidential web client is configured
(``web.client_id``). All routes live under ``/web`` and are **public at the
protection-map level** — the map's credential check is the API one (IdToken
cookie / bearer token), which a browser mid-sign-in cannot pass — and enforce
their own session, CSRF, and role checks in-route, the same way the API
routers enforce theirs with dependencies. ``enforce_web_surface_preconditions``
refuses startup if the map does not actually leave the prefix public.

The pages this issue ships are deliberately minimal: sign-in, an overview page
proving a session works, sign-out. The invitation pages (#90–#92) mount their
own routers on the dependencies and layout this surface exports.
"""

from __future__ import annotations

import logging
import posixpath
import secrets
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, Depends, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from ..frames.auth import display_identity_from_claims, user_from_claims
from ..path_protection import request_path
from ..web import oidc
from ..web.authz import (
    WebAuthorizationUnavailable,
    WebAuthRequired,
    WebForbidden,
    get_web_session,
    require_csrf,
    require_web_session,
    signin_redirect_target,
)
from ..web.data_statement import data_statement_page
from ..web.forms import FormRefused
from ..web.pages import (
    SECURITY_HEADERS,
    STYLE_PATH,
    STYLESHEET,
    authorization_unavailable_page,
    body_refused_page,
    escape,
    forbidden_page,
    page_response,
    render_page,
    sign_in_failed_page,
    signed_out_page,
)
from ..web.privacy_statement import privacy_statement_page
from ..web.request_limits import connection_close_headers
from ..web.session import (
    TRANSIENT_COOKIE,
    TRANSIENT_LIFETIME_SECONDS,
    TRANSIENT_PURPOSE,
    WebSession,
    clear_session_cookie,
    clear_transient_cookie,
    set_session_cookie,
    set_transient_cookie,
)
from ..web.surface import (
    ADMIN_INVITATIONS_PATH,
    CALLBACK_PATH,
    DATA_STATEMENT_PATH,
    LANDING_PATH,
    ORG_INVITATIONS_PATH,
    PRIVACY_PATH,
    PUBLIC_WEB_PATHS,
    SIGNED_OUT_PATH,
    SIGNIN_PATH,
    SIGNOUT_PATH,
    TERMS_PATH,
    WEB_LOGO_PATH,
    WebSurface,
    answers_json,
    clamped_session_lifetime,
)
from ..web.terms_of_service import terms_of_service_page

logger = logging.getLogger("frames_server.web")

LOGO_BYTES = (Path(__file__).parent.parent / "web" / "collab-logo.png").read_bytes()
"""The wordmark, read once at import.

At import rather than per request, and from the package rather than from a
configured path: it is chrome, it is the same for every deployment, and a
missing file should stop the process at startup where somebody is watching
rather than render a broken image on the sign-in page.
"""

MAX_NEXT_LENGTH = 2000


FLOW_PATHS = (SIGNIN_PATH, CALLBACK_PATH)
"""Routes that are steps *through* the sign-in flow, never destinations of it.

A ``next`` naming one of these is refused. The sign-in route is the one that
matters: ``/web/signin?renew=1&next=/web/signin?renew=1`` sent a browser
around silent SSO forever, because ``renew`` deliberately skips the
already-signed-in shortcut and so nothing else broke the cycle. It terminates
only when the person closes the tab.

The callback is listed with it because it is the same category and costs one
entry — landing there without a transient cookie renders the fixed
sign-in-failed page, which terminates but is nobody's intended destination.
"""


MAX_NEXT_DECODE_PASSES = 8
"""How many times :func:`_fully_decoded` will unquote before giving up.

Bounded so a pathological input cannot spin. Reaching the bound is not a
failure: the loop returns whatever it has decoded so far, which is then
normalized and compared like anything else, and an input needing more than
eight passes to reveal a flow route is far past anything a browser would
follow.
"""


def _fully_decoded(value: str) -> str:
    """Percent-decode until the value stops changing.

    One pass is not enough, and the reasoning that said it was — that a
    doubly-encoded value must fail the leading-slash rule — was simply wrong.
    ``next=/%252e%252e/web/signin%3Frenew%3D1`` arrives from the query string
    already decoded once, as ``/%2e%2e/web/signin?renew=1``: it begins with a
    slash, so the shape rule admits it, and a browser then normalizes
    ``%2e%2e`` to ``..`` and requests ``/web/signin?renew=1``. The loop was
    back.

    Percent-encoded dot segments are normalized by the URL standard itself
    (``%2e`` counts as ``.`` when identifying single- and double-dot
    segments), so this is the browser's own behaviour rather than a defensive
    guess. Decoding *everything* rather than only dot segments is deliberately
    broader than the standard: this function only ever decides a **refusal**,
    and the cost of over-refusing is that someone lands on the overview page
    instead of a page they asked for.
    """

    for _ in range(MAX_NEXT_DECODE_PASSES):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    return value


def _targets_flow_route(value: str) -> bool:
    """Whether *value* points at one of :data:`FLOW_PATHS`, however spelled.

    Decoded first, then normalized, then compared — in that order, because
    each step can reveal work for the next: decoding turns ``%2e%2e`` into
    ``..``, and normalizing turns ``/a/../web/signin`` into ``/web/signin``.
    A browser does both before it sends, so a comparison that does neither is
    comparing against a string no server will ever be asked for.
    """

    path = posixpath.normpath(_fully_decoded(value).split("?", 1)[0].split("#", 1)[0])
    return any(path == flow or path.startswith(flow + "/") for flow in FLOW_PATHS)


def sanitize_next_path(value: str | None) -> str:
    """An app-relative path a post-sign-in redirect may safely target.

    Anything else — absolute URLs, scheme-relative ``//host`` forms,
    backslash variants browsers normalize into them, control characters, and
    the flow's own routes — falls back to the overview page. This is the whole
    open-redirect surface of the flow, so the rule is an allowlist of shape,
    not a denylist of known-bad prefixes.
    """

    if not value or len(value) > MAX_NEXT_LENGTH:
        return LANDING_PATH
    if not value.startswith("/") or value.startswith("//"):
        return LANDING_PATH
    if "\\" in value or ":" in value.split("?", 1)[0]:
        return LANDING_PATH
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return LANDING_PATH
    if _targets_flow_route(value):
        return LANDING_PATH
    return value


def _root_path(request: Request) -> str:
    return (request.scope.get("root_path") or "").rstrip("/")


def _redirect(url: str, *, status_code: int = status.HTTP_303_SEE_OTHER) -> RedirectResponse:
    return RedirectResponse(url, status_code=status_code, headers=dict(SECURITY_HEADERS))


def register_exception_handlers(app: FastAPI) -> None:
    """Install the page-shaped outcomes for the web dependencies.

    On the app rather than the router because Starlette resolves exception
    handlers app-wide; the exceptions are raised only by this surface's
    dependencies, so the handlers fire only for it.
    """

    @app.exception_handler(WebAuthRequired)
    async def _auth_required(request: Request, exc: WebAuthRequired) -> Response:
        if answers_json(request_path(request)):
            return JSONResponse({"error": "authentication required"}, status_code=status.HTTP_401_UNAUTHORIZED)
        return _redirect(signin_redirect_target(_root_path(request), exc.next_path))

    @app.exception_handler(WebForbidden)
    async def _forbidden(request: Request, exc: WebForbidden) -> Response:
        logger.info("web_forbidden", extra={"reason": str(exc)})
        if answers_json(request_path(request)):
            # The panel is a JSON client; a 403 page would reach it as an
            # unparseable body and it would have to guess from the status.
            return JSONResponse({"error": "forbidden"}, status_code=status.HTTP_403_FORBIDDEN)
        return page_response(
            forbidden_page(root_path=_root_path(request)),
            status_code=status.HTTP_403_FORBIDDEN,
        )

    @app.exception_handler(FormRefused)
    async def _form_refused(request: Request, exc: FormRefused) -> Response:
        # Raised by require_csrf's bounded form fallback (issue #119) for a
        # body the surface will not read: oversize (413), or a form shape it
        # does not parse (415). The invitation pages catch their own — they
        # answer with their page — so reaching this handler means the refusal
        # came from a route that took the dependency.
        #
        # FormRefused is raised before the body is consumed, by construction
        # (see web.forms), so `body_consumed=False` is not a guess: answering
        # without the close would leave the server stalled on whatever the
        # caller is still sending — the exhaustion the cap prevents, moved
        # from memory to connections.
        logger.info("web_form_refused", extra={"status_code": exc.status_code})
        response = page_response(
            body_refused_page(root_path=_root_path(request), status_code=exc.status_code),
            status_code=exc.status_code,
        )
        response.headers.update(connection_close_headers(body_consumed=False))
        return response

    @app.exception_handler(WebAuthorizationUnavailable)
    async def _authorization_unavailable(request: Request, exc: WebAuthorizationUnavailable) -> Response:
        # Error level, and a 503 rather than a 403: this is a deployment that
        # cannot decide operator authority at all, which would otherwise look
        # exactly like a correct refusal and lock every operator out silently.
        logger.error("web_authorization_unavailable", extra={"reason": str(exc)})
        return page_response(
            authorization_unavailable_page(root_path=_root_path(request)),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


SAFE_PUBLIC_METHODS = frozenset({"GET", "HEAD"})
"""The only methods an anonymous route of this surface may answer.

``PUBLIC_WEB_PATHS`` is a set of **paths**, because that is what the session
guard can key on — it runs before routing and has no route to ask about
methods. So a path in that set is anonymous for every method, and a
state-changing handler registered on one would be anonymous too.

That is not hypothetical: a ``POST /invite/accept`` passed the first version
of this check, which looked only at the path. Same shape as the bypasses that
reshaped the guard in #88 — a check keyed on one attribute of an object the
author controls — and the same answer: constrain what the seam will accept
rather than trusting what it is handed.
"""


def _refuse_unless_safely_public(route: object) -> None:
    """Refuse a public page route that is not a safe, allowlisted ``APIRoute``.

    Three conditions, and each closes a different way of getting anonymity for
    something that should not have it:

    * an **actual** :class:`~fastapi.routing.APIRoute` — an ``isinstance``,
      not a duck-typed ``.path``, because a fabricated object satisfied the
      structural version of this check in #88's review;
    * a path already in :data:`~..web.surface.PUBLIC_WEB_PATHS`, so choosing
      this argument buys placement and never anonymity — anonymity still costs
      the reviewed line in the allowlist;
    * a method set within :data:`SAFE_PUBLIC_METHODS`, so an anonymous path
      cannot also carry a handler that changes something.
    """

    if not isinstance(route, APIRoute):
        raise RuntimeError(
            f"{type(route).__name__} was passed to make_router as a public page route."
            " Only an APIRoute may be registered there: a Mount's routing is opaque, a"
            " WebSocketRoute is outside the session model, and anything else cannot be"
            " checked at all."
        )
    if route.path not in PUBLIC_WEB_PATHS:
        raise RuntimeError(
            f"{route.path!r} was passed to make_router as a public page route but is not in"
            " web.surface.PUBLIC_WEB_PATHS. Anonymous access to this surface is granted"
            " in exactly one place, and it is that set — WebSessionGuardMiddleware reads"
            " it and would authenticate this route anyway, so mounting it here without"
            " the entry produces a route that answers a sign-in redirect forever."
        )
    methods = {method.upper() for method in (route.methods or ())}
    if not methods or not methods <= SAFE_PUBLIC_METHODS:
        offending = ", ".join(sorted(methods - SAFE_PUBLIC_METHODS)) or "none"
        raise RuntimeError(
            f"{route.path!r} was passed to make_router as a public page route but answers"
            f" {offending}. A path in PUBLIC_WEB_PATHS is anonymous for *every* method —"
            " the guard runs before routing and cannot see methods — so a handler that"
            " changes something must live at its own path outside the allowlist, the way"
            " /invite/accept/redeem does."
        )


def session_gated_router(**kwargs) -> APIRouter:
    """An ``APIRouter`` whose every route requires a web session.

    **This is how a page router of this surface is created** — #90–#92 build
    theirs with this rather than a bare ``APIRouter``, and hand it to
    :func:`make_router` as a ``page_router``. Writing the dependency once,
    here, is what makes "authenticated by default" a property of the surface
    instead of a rule each author has to remember; a route that genuinely must
    be public is added to :data:`PUBLIC_WEB_PATHS` deliberately, in review.
    """

    dependencies = [Depends(require_web_session), *kwargs.pop("dependencies", [])]
    kwargs.setdefault("include_in_schema", False)
    return APIRouter(dependencies=dependencies, **kwargs)


def make_router(
    surface: WebSurface,
    *,
    page_routers: Sequence[APIRouter] = (),
    public_page_routers: Sequence[APIRouter] = (),
) -> APIRouter:
    """Build the surface's routes bound to this deployment's settings.

    Two routers, and the split is the security boundary rather than a
    filing convenience. ``router`` carries ``require_web_session`` as a
    **router-level dependency**, so a route added to it is authenticated
    whether or not its author remembered to say so; ``public_router`` holds
    the four routes of :data:`PUBLIC_WEB_PATHS` that must work without a
    session, and adding a route to it is a visible, reviewable act.

    ``page_routers`` is the seam for #90–#92: a page router passed here is
    included **inside** the gated router, so it inherits the session
    requirement even if it was built as a bare ``APIRouter`` — belt to
    :func:`session_gated_router`'s braces.

    ``public_page_routers`` is the narrow companion seam, and it checks
    itself — see :func:`_refuse_unless_safely_public`, which requires a real
    ``APIRoute``, an allowlisted path, and a safe method. So "public" cannot
    be obtained by choosing the other argument: it still costs the reviewed
    line in :data:`~..web.surface.PUBLIC_WEB_PATHS`, and a state-changing
    handler cannot ride an anonymous path at all. The invitation-acceptance
    page (#90) is its one caller: its audience has no account yet, so the page
    must render for an anonymous browser.

    The protection map cannot supply this default: its ``authenticated`` level
    runs the *API* credential check, which a browser mid-sign-in cannot pass,
    so ``/web`` is necessarily map-public. The fail-closed default has to live
    here, and a test walks the app's registered routes to confirm it held.
    """

    router = APIRouter(include_in_schema=False, dependencies=[Depends(require_web_session)])
    public_router = APIRouter(include_in_schema=False)

    def _failed_sign_in(request: Request, reason: str) -> Response:
        # One fixed page for every way a sign-in can fail. The reason goes to
        # the log; nothing derived from the request or the IdP response is
        # rendered, so the page cannot become a reflection surface.
        logger.warning("web_sign_in_failed", extra={"reason": reason})
        response = page_response(
            sign_in_failed_page(root_path=_root_path(request)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
        clear_transient_cookie(response)
        return response

    @public_router.get(SIGNIN_PATH)
    async def signin(request: Request) -> Response:
        """Start the authorization-code flow (or skip it, if already signed in).

        The state, nonce, PKCE verifier, and sanitized ``next`` target ride a
        short-lived signed transient cookie — server-side values a browser
        round trip cannot alter, bound to the browser that started the flow.

        ``?renew=1`` runs the flow even when a valid session already exists.
        The acceptance page (#90) needs it: the session's verified-address
        claims go stale as an authorization input long before the session
        expires, and the only way this surface can obtain current claims is to
        ask the IdP for a new ID token. Without it that page would bounce
        between itself and a sign-in route that answered "already signed in".

        The parameter can only ever cause **more** authentication, never less
        — it skips a shortcut, not a check — and the flow it starts is the
        same state/nonce/PKCE-protected one, so there is nothing to gain by
        setting it on someone else's behalf.

        ``?register=1`` starts the same flow on Keycloak's registration form
        (``prompt=create``) instead of its login form. The acceptance page
        (#144) offers it as the primary path for invitees, who by definition
        have no account yet. Same safety argument as ``renew``: it selects
        which IdP screen appears first and skips nothing — an existing
        session still short-circuits it, and the code exchange, state, nonce
        and PKCE are identical.
        """

        next_path = sanitize_next_path(request.query_params.get("next"))
        renew = request.query_params.get("renew") == "1"
        register = request.query_params.get("register") == "1"
        if not renew and get_web_session(request) is not None:
            return _redirect(f"{_root_path(request)}{next_path}")
        state = oidc.generate_state()
        nonce = oidc.generate_nonce()
        verifier = oidc.generate_pkce_verifier()
        now = surface.now()
        transient = surface.codec.encode(
            TRANSIENT_PURPOSE,
            {
                "state": state,
                "nonce": nonce,
                "verifier": verifier,
                "next": next_path,
                "iat": now,
                "exp": now + TRANSIENT_LIFETIME_SECONDS,
            },
        )
        response = _redirect(
            oidc.authorization_url(
                authorize_endpoint=surface.authorize_endpoint,
                client_id=surface.client_id,
                redirect_uri=surface.redirect_uri(request),
                scope=surface.scope,
                state=state,
                nonce=nonce,
                code_challenge=oidc.pkce_challenge(verifier),
                prompt="create" if register else None,
            )
        )
        set_transient_cookie(response, transient)
        return response

    @public_router.get(CALLBACK_PATH)
    async def callback(request: Request) -> Response:
        """Finish the flow: validate state, exchange the code, mint the session.

        Every failure renders the same fixed page and clears the transient
        cookie, so a failed attempt cannot be resumed — starting over mints
        fresh state and verifier. The authorization code appears in this
        request's query string by protocol design; it is single-use, expires
        in seconds, and is unredeemable without the client secret *and* the
        PKCE verifier, neither of which a log line carries.
        """

        transient_value = request.cookies.get(TRANSIENT_COOKIE)
        transient = surface.codec.decode(TRANSIENT_PURPOSE, transient_value) if transient_value else None
        if transient is None:
            return _failed_sign_in(request, "missing or invalid transient state cookie")
        if request.query_params.get("error"):
            # The IdP refused (person cancelled, consent denied, ...). Never
            # echo the error text; it is attacker-writable via the URL.
            return _failed_sign_in(request, "authorization server returned an error")
        state = request.query_params.get("state", "")
        expected_state = transient.get("state", "")
        # Bytes, not str: compare_digest raises TypeError on non-ASCII
        # strings, and `state` is attacker-writable via the URL — a mismatch
        # must be a refusal, never an exception.
        if (
            not state
            or not isinstance(expected_state, str)
            or not secrets.compare_digest(state.encode(), expected_state.encode())
        ):
            return _failed_sign_in(request, "state mismatch")
        code = request.query_params.get("code")
        if not code:
            return _failed_sign_in(request, "no authorization code in callback")
        try:
            token_response = await oidc.exchange_code(
                token_endpoint=surface.token_endpoint,
                client_id=surface.client_id,
                client_secret=surface.client_secret,
                code=code,
                redirect_uri=surface.redirect_uri(request),
                code_verifier=str(transient.get("verifier", "")),
            )
            claims = oidc.verify_id_token(
                token_response["id_token"],
                jwks_url=surface.jwks_url,
                issuer=surface.issuer_url,
                client_id=surface.client_id,
                nonce=str(transient.get("nonce", "")),
            )
        except oidc.OidcFlowError as exc:
            return _failed_sign_in(request, str(exc))

        user = user_from_claims(claims)
        if not user:
            return _failed_sign_in(request, "id_token carries no acceptable identity claim")
        display = display_identity_from_claims(claims)
        # The one moment this surface can see the identity provider's answer.
        # The session that follows carries identity and nothing else, and every
        # authorization decision is re-resolved from this server's own stores
        # on the request being authorized -- so if the groups claim is not read
        # here, it is never read on this axis at all. Failing to reconcile must
        # not fail the sign-in: the fallback is the role the table already
        # holds, which is the behaviour every deployment had before this.
        # On a worker thread: the Postgres sync makes blocking database calls,
        # and on the event loop they would stall every other request.
        await run_in_threadpool(_reconcile_platform_role, request, user=user, claims=claims, display=display)
        now = surface.now()
        # One source for both the signed `exp` and the cookie's Max-Age, and
        # it clamps the validated field through a module function rather than
        # an instance method — an instance method can be shadowed on the
        # instance, and the mint path must not be reachable that way.
        lifetime = clamped_session_lifetime(surface.session_lifetime_seconds)
        session = WebSession(
            user=user,
            name=display.name,
            email=display.email,
            # Recorded at sign-in from the ID token's own claim, because the
            # browser never holds that token afterwards and the acceptance
            # page (#90) has to know whether the address was vouched for. The
            # claims reader has already reduced anything that is not boolean
            # true to False.
            email_verified=display.email_verified,
            csrf=secrets.token_urlsafe(32),
            issued_at=now,
            expires_at=now + lifetime,
        )
        next_path = sanitize_next_path(str(transient.get("next", "")))
        response = _redirect(f"{_root_path(request)}{next_path}")
        clear_transient_cookie(response)
        set_session_cookie(
            response,
            surface.codec.encode_session(session),
            max_age=lifetime,
        )
        return response

    @router.get(LANDING_PATH)
    async def overview(request: Request, session: WebSession = Depends(require_web_session)) -> Response:
        """The signed-in overview: proves the session and hosts the sign-out form.

        The operator invitation page (#91) is linked unconditionally rather
        than only for operators. Resolving the platform role here would make
        the *overview* fail on a deployment with no role source — the one page
        that must keep working so an operator can at least see they are signed
        in — and a non-operator following the link gets the surface's 403 page,
        which is a real answer and says what to do about it. The owner
        invitation page (#142) is linked the same way for the same reasons:
        resolving membership here would add a store read to the page that must
        never fail, and the link's worst case is the same honest 403.
        """

        identity = session.name or session.email or session.user
        rows = f"<dt>Signed in as</dt><dd>{escape(identity)}</dd>"
        if session.email and session.email != identity:
            rows += f"<dt>Email</dt><dd>{escape(session.email)}</dd>"
        root = escape(_root_path(request))
        body = (
            "<h1>Collab operations</h1>"
            "<p>This is the operations surface for this Collab deployment.</p>"
            f'<p><a href="{root}{ADMIN_INVITATIONS_PATH}">Invitations</a>'
            " — invite someone to this deployment, and revoke an invitation."
            " Platform operators only.</p>"
            f'<p><a href="{root}{ORG_INVITATIONS_PATH}">Your organization\'s'
            " invitations</a> — invite someone into your organization, and"
            " revoke an invitation. Organization owners only.</p>"
            f"<dl>{rows}</dl>"
        )
        return page_response(
            render_page(
                title="Collab operations",
                body=body,
                root_path=_root_path(request),
                identity_label=identity,
                csrf_token=session.csrf,
            )
        )

    @router.post(SIGNOUT_PATH)
    async def signout(request: Request, _session: WebSession = Depends(require_csrf)) -> Response:
        """End the session on this browser.

        The cookie is stateless, so this deletes the browser's copy; it cannot
        recall copies made before this moment (see ``web.session``). Authority
        does not ride the cookie — roles are checked per request — so the
        exposure of a pre-sign-out copy is identity, bounded by ``exp``.
        """

        response = _redirect(f"{_root_path(request)}{SIGNED_OUT_PATH}")
        clear_session_cookie(response)
        return response

    @public_router.get(SIGNED_OUT_PATH)
    async def signed_out(request: Request) -> Response:
        # The same allowlist the sign-in redirect applies, so the link can only
        # ever carry a target sign-in would accept. The overview is sign-in's
        # own default, so it is left off the link.
        next_path = sanitize_next_path(request.query_params.get("next"))
        return page_response(
            signed_out_page(
                root_path=_root_path(request),
                next_path=None if next_path == LANDING_PATH else next_path,
            )
        )

    @public_router.get(DATA_STATEMENT_PATH)
    async def data_statement(request: Request) -> Response:
        """The data statement (#146). Anonymous by design — its audience is
        deciding whether to create an account at all; the argument lives on
        the path's PUBLIC_WEB_PATHS entry."""

        return page_response(
            data_statement_page(root_path=_root_path(request)),
            path=DATA_STATEMENT_PATH,
        )

    @public_router.get(TERMS_PATH)
    async def terms_of_service(request: Request) -> Response:
        """The Terms of Service (#95) is anonymous by design. The Keycloak
        acceptance step links here *before* it issues a token so people who
        have yet to accept can read the text. The argument lives on the path's
        PUBLIC_WEB_PATHS entry."""

        return page_response(
            terms_of_service_page(root_path=_root_path(request)),
            path=TERMS_PATH,
        )

    @public_router.get(PRIVACY_PATH)
    async def privacy_statement(request: Request) -> Response:
        """The Privacy Statement (#95). Anonymous on the same argument as the
        Terms of Service; the two are linked from the same consent line."""

        return page_response(
            privacy_statement_page(root_path=_root_path(request)),
            path=PRIVACY_PATH,
        )

    @public_router.get(WEB_LOGO_PATH)
    async def wordmark() -> Response:
        """The product wordmark, read from the package at import time.

        Read once into memory rather than per request: it is a small, fixed
        file that never changes for the life of the process, and serving it
        from bytes keeps this route off the filesystem entirely.
        """

        return Response(
            LOGO_BYTES,
            media_type="image/png",
            headers=dict(SECURITY_HEADERS),
        )

    @public_router.get(STYLE_PATH)
    async def stylesheet() -> Response:
        return Response(
            STYLESHEET,
            media_type="text/css; charset=utf-8",
            headers=dict(SECURITY_HEADERS),
        )

    for page_router in page_routers:
        router.include_router(page_router)

    for page_router in public_page_routers:
        for route in page_router.routes:
            _refuse_unless_safely_public(route)
        public_router.include_router(page_router)

    # The public routes first: FastAPI matches in registration order, and the
    # session-gated router must never shadow the route that mints a session.
    public_router.include_router(router)
    return public_router


def _reconcile_platform_role(request: Request, *, user: str, claims: dict, display) -> None:
    """Sync this subject's platform role from the ID token's groups claim.

    Read off ``request.app.state`` rather than a module global, for the same
    reason every store on this surface is: the mounted MCP app has its own
    state object and nothing here may hold process-wide state. An app whose
    state has no sync does nothing, which is what a deployment that never
    opted in should do.

    Errors are logged and swallowed. That is a deliberate asymmetry with the
    rest of this surface, where an authorization input that cannot be resolved
    fails the request closed: here the request being authorized has not started
    yet, the pre-existing table row is a perfectly good answer, and refusing
    the sign-in would turn a database blip into "nobody can log in" on a
    deployment whose roles were already correct. It fails *closed* in the sense
    that matters -- a failed reconcile grants nothing it would not otherwise
    have granted.
    """

    sync = getattr(request.app.state, "platform_role_sync", None)
    if sync is None or not getattr(sync, "configured", False):
        return
    groups = claims.get("groups")
    if not isinstance(groups, list):
        # An absent or malformed claim is not "in no groups": a realm that has
        # not been given a groups mapper would otherwise revoke every synced
        # operator the first time they signed in after this shipped.
        logger.warning("platform_role_sync_no_groups_claim", extra={"user": user})
        return
    try:
        sync.reconcile(user_id=user, claim_groups=[g for g in groups if isinstance(g, str)], display=display)
    except Exception:
        logger.exception("platform_role_sync_failed", extra={"user": user})
