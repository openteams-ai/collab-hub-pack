"""Page-facing authentication and authorization dependencies.

The web session asserts *identity only*; every authorization decision here is
resolved from the server's own stores on the request being authorized. That
split is what makes the stateless session cookie acceptable: revoking a role
locks its holder out on their next request, however long their session cookie
has left to live.

Failure shapes are page-shaped, not API-shaped. A missing or expired session
on a page is not an error to a person — it is "sign in first", so
:class:`WebAuthRequired` becomes a 303 to the sign-in route carrying the page
they were after. A role refusal is a real answer and renders the 403 page.
Neither subclasses ``HTTPException``: these must reach the handlers that
``routers.web.register_exception_handlers`` installs, not any of the generic
HTTP-exception machinery.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Iterable, Sequence
from urllib.parse import urlencode

from fastapi import Depends, Request
from fastapi.routing import APIRoute
from starlette.routing import Mount, WebSocketRoute

from ..frames.orgs import ROLE_OWNER, OrgStore, OrgsUnavailableError
from ..path_protection import request_path
from .forms import FORM_CONTENT_TYPE, MULTIPART_CONTENT_TYPE, form_fields, media_type
from .session import SESSION_COOKIE, WebSession, csrf_token_matches
from .surface import SIGNIN_PATH, WebSurface

logger = logging.getLogger("frames_server.web")

PLATFORM_ROLE_OPERATOR = "operator"

PLATFORM_ROLE_RESOLVER_STATE_ATTR = "platform_role_resolver"
"""Optional override installed on ``app.state``: ``Callable[[str], str | None]``
from subject to active platform role.

A **fallback**, consulted only when the org store provides no
``resolve_principal``. It exists so a test need not stand up a store; it
deliberately does not outrank the server's own table, because a grant that
bypasses ``collab_platform_roles`` also bypasses its audit trail.
"""

PlatformRoleResolver = Callable[[str], str | None]


class WebAuthorizationUnavailable(Exception):
    """No platform-role source exists, so operator authority cannot be decided.

    Deliberately **not** a :class:`WebForbidden`. Issue #87 owns the
    ``collab_platform_roles`` table and lands ``OrgStore.resolve_principal``;
    if that wiring is ever missing, every operator is locked out — and a plain
    403 makes that indistinguishable from "you are not an operator", which is
    exactly the silent, permanent failure worth refusing to be quiet about.
    This renders a distinct 503 page and logs at error level, so a missed
    merge line announces itself the first time anyone opens an operator page
    rather than being mistaken for correct authorization.

    It is still fail-closed: no request is ever granted operator authority on
    this path.
    """


class WebAuthRequired(Exception):
    """No valid session; the browser should be sent to sign in.

    ``next_path`` is the app-relative path (with query) the person was after,
    replayed through the sign-in flow so they land where they were going.
    """

    def __init__(self, next_path: str) -> None:
        super().__init__("web session required")
        self.next_path = next_path


class WebForbidden(Exception):
    """Authenticated, but not allowed: renders the 403 page.

    The message is for logs and tests; the page body is fixed copy.
    """


def _surface(request: Request) -> WebSurface:
    surface = getattr(request.app.state, "web_surface", None)
    if surface is None:
        # The router is only mounted when make_app built a surface, so this is
        # app state assembled some other way — refuse rather than improvise.
        raise WebForbidden("web surface is not configured on this app")
    return surface


def get_web_session(request: Request) -> WebSession | None:
    """The request's verified web session, or ``None``.

    ``None`` for absent, tampered, foreign-purpose, and expired cookies alike
    — see ``SessionCodec.decode``.
    """

    value = request.cookies.get(SESSION_COOKIE)
    if not value:
        return None
    return _surface(request).codec.decode_session(value)


def _next_path(request: Request) -> str:
    path = request_path(request)
    query = str(request.url.query)
    if query:
        return f"{path}?{query}"
    return path


def require_web_session(request: Request) -> WebSession:
    """Dependency: a valid web session, or a redirect through sign-in."""

    session = get_web_session(request)
    if session is None:
        raise WebAuthRequired(_next_path(request))
    return session


def unwrap_dependency(call: object) -> object:
    """Strip ``functools.partial`` layers off a dependency.

    ``partial`` is unwrapped because it is a *structural* guarantee: calling
    ``partial(require_web_session)`` provably calls ``require_web_session``,
    so recognizing it avoids reporting a route that genuinely enforces the
    session.

    ``__wrapped__`` is deliberately **not** followed. It is a naming
    convention that ``functools.wraps`` sets, and it proves nothing about what
    the wrapper does: a dependency that returns ``None`` while wearing
    ``@wraps(require_web_session)`` was read as protected. The distinction is
    the whole lesson of this module — believe structure that constrains
    behavior, never a label that merely claims it.

    Bounded against a ``partial`` cycle, which cannot occur through the public
    API but costs one ``set`` to rule out.
    """

    seen: set[int] = set()
    while isinstance(call, functools.partial) and id(call) not in seen:
        seen.add(id(call))
        call = call.func
    return call


def route_runs_dependency(route: object, target: object) -> bool:
    """Whether *route* runs *target* before its endpoint.

    Walks the whole dependency tree, not just the top level: a page gated with
    ``Depends(require_operator)`` reaches the session check one level down, and
    a check that only looked at direct dependencies would call that route
    unprotected and be wrong in the safe-looking direction.
    """

    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return False
    pending = list(getattr(dependant, "dependencies", ()))
    seen: set[int] = set()
    while pending:
        dependency = pending.pop()
        if id(dependency) in seen:
            continue
        seen.add(id(dependency))
        if unwrap_dependency(getattr(dependency, "call", None)) is target:
            return True
        pending.extend(getattr(dependency, "dependencies", ()))
    return False


def route_enforces_session(route: object) -> bool:
    """Whether *route* runs :func:`require_web_session` before its endpoint."""

    return route_runs_dependency(route, require_web_session)


def route_enforces_csrf(route: object) -> bool:
    """Whether *route* runs :func:`require_csrf` before its endpoint."""

    return route_runs_dependency(route, require_csrf)


UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
"""The methods that need a CSRF token to be safe on a cookie-authenticated
surface. ``GET``/``HEAD``/``OPTIONS`` are excluded because they must not have
side effects; a page of this surface that gives one side effects has a bug the
token would only have hidden."""


def route_unsafe_methods(route: object) -> frozenset[str]:
    """The state-changing methods *route* answers."""

    methods = getattr(route, "methods", None) or ()
    return UNSAFE_METHODS & {str(method).upper() for method in methods}


def route_offence(route: object, prefixes: Sequence[str]) -> str | None:
    """Why *route* is not acceptable on the browser surface, or ``None``.

    Three different jobs, and they are not equally load-bearing.

    ``Mount`` and ``WebSocketRoute`` are **refused for real**: a mount's
    routing is opaque, and a WebSocket is outside the session model entirely
    (see :mod:`.guard`). Startup refusal is the enforcement for these.

    The **session** arm is a *lint*. Since
    :class:`~.guard.WebSessionGuardMiddleware` authenticates by path before
    any route runs, a page missing ``require_web_session`` is authenticated
    regardless; reporting it is a developer-experience service — the handler
    will not receive its typed session — not a gate. That is precisely why
    this may compare structure at all: fooling it gains an attacker nothing.

    The **CSRF** arm is *enforcement*, and it is here because nothing else
    does it. The guard inversion made a forgotten ``require_web_session``
    harmless; it did nothing for a forgotten ``require_csrf``, which stayed
    exactly as silent as it ever was — a page of #90–#92 adding a ``POST``
    and omitting the dependency was unprotected with nothing at startup
    noticing. ``SameSite=Lax`` bounds that but does not close it: the
    registrable domain is ``openteams.app``, so a request from any sibling
    subdomain is *same-site* and carries the cookie. ``__Host-`` prevents
    cookie **planting**, not cookie **sending**.

    This is enforcement against *forgetting*, not against an adversary — only
    someone who can register routes can trip it, and they could equally write
    a handler that ignores the token. That is why an in-route check is an
    acceptable answer, declared in
    :data:`~.surface.CSRF_ENFORCED_IN_ROUTE` and reviewed once, rather than
    something this function tries to detect. It cannot detect it: an
    endpoint that calls the check itself, or reimplements the same
    constant-time comparison, is indistinguishable from one that does neither
    without reading its body — and reading a body for a name is the
    label-not-structure mistake this module exists to avoid.

    The type test is an ``isinstance`` against the real
    :class:`fastapi.routing.APIRoute`, not a duck-typed ``.dependant``. A
    fabricated object carrying an invented dependency tree passed the old
    check; the documented rule was always "APIRoute only", and now the code
    says so.
    """

    from .surface import ALLOWED_WEB_MOUNTS, CSRF_ENFORCED_IN_ROUTE, PUBLIC_WEB_PATHS

    path = getattr(route, "path", None)
    if not isinstance(path, str) or not on_web_surface(path, prefixes):
        return None
    if isinstance(route, Mount):
        if path in ALLOWED_WEB_MOUNTS:
            return None
        return (
            f"{path} mounts a sub-application, whose routing cannot be inspected from"
            " outside; its paths are still session-guarded, but mounting one here is"
            " not reviewed"
        )
    if isinstance(route, WebSocketRoute):
        return f"{path} is a WebSocket route, which this surface does not serve"
    if not isinstance(route, APIRoute):
        return f"{path} is a {type(route).__name__}, which this surface does not serve"
    if path in PUBLIC_WEB_PATHS:
        return None
    if not route_enforces_session(route):
        return f"{path} requires no web session and is not in PUBLIC_WEB_PATHS"
    unsafe = route_unsafe_methods(route)
    if unsafe and path not in CSRF_ENFORCED_IN_ROUTE and not route_enforces_csrf(route):
        return (
            f"{path} answers {', '.join(sorted(unsafe))} with no require_csrf in its"
            " dependency tree and is not in CSRF_ENFORCED_IN_ROUTE"
        )
    return None


def on_web_surface(path: str, prefixes: Sequence[str]) -> bool:
    """Whether *path* belongs to one of the browser surface's prefixes."""

    for prefix in prefixes:
        prefix = prefix.rstrip("/")
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def offending_web_routes(
    routes: Iterable[object], *, prefixes: Sequence[str] | None = None
) -> list[tuple[object, str]]:
    """Every registered route the surface may not serve, with the reason.

    The guard that matters, because it is the one that catches misuse it did
    not participate in. ``session_gated_router`` and ``make_router``'s
    ``page_routers`` make the right thing easy, but neither can stop a caller
    from registering an ``APIRouter``, mounting a sub-application, or adding a
    WebSocket on the app directly — all three produced a live, anonymous page
    or socket. This inspects the *result*: whatever is registered, however it
    got there.

    Returns route objects alongside the reason so a caller can report which
    object offended; nothing on the request path consults this — the guard
    authenticates by path, not by route.
    """

    from .surface import WEB_SURFACE_PREFIXES

    if prefixes is None:
        prefixes = WEB_SURFACE_PREFIXES
    offenders = []
    for route in routes:
        reason = route_offence(route, prefixes)
        if reason is not None:
            offenders.append((route, reason))
    return offenders


def unprotected_web_routes(
    routes: Iterable[object], *, prefixes: Sequence[str] | None = None
) -> list[str]:
    """The offending paths, sorted — the readable form of the check."""

    return sorted({getattr(route, "path", "") for route, _ in offending_web_routes(routes, prefixes=prefixes)})


def stray_page_routes(
    routes: Iterable[object], *, prefixes: Sequence[str] | None = None
) -> list[str]:
    """Browser-page routes registered outside every guarded prefix.

    A page is identified by the thing that makes it one: it depends on
    ``require_web_session`` (directly, or through ``require_operator`` and
    friends). Such a route under an unlisted prefix is the one arrangement the
    guard cannot protect — the guard keys on
    :data:`~.surface.WEB_SURFACE_PREFIXES`, so a page at ``/reports`` would be
    reachable without a session no matter what its dependencies say, because
    the middleware would never look at it.

    Now that the guard is the *only* boundary, that has to be loud. Using
    dependency structure to spot it is fine here for the same reason the lint
    is: nobody gains anything by hiding a page from a check that would
    otherwise protect it.
    """

    from .surface import WEB_SURFACE_PREFIXES

    if prefixes is None:
        prefixes = WEB_SURFACE_PREFIXES
    stray = []
    for route in routes:
        path = getattr(route, "path", None)
        if not isinstance(path, str) or not isinstance(route, APIRoute):
            continue
        if on_web_surface(path, prefixes):
            continue
        if route_enforces_session(route):
            stray.append(path)
    return sorted(set(stray))


def stale_csrf_exemptions(
    routes: Iterable[object], *, prefixes: Sequence[str] | None = None
) -> list[str]:
    """Declared CSRF exemptions that a mounted route contradicts.

    An entry in :data:`~.surface.CSRF_ENFORCED_IN_ROUTE` is a reviewed claim:
    *this path answers a state-changing method and enforces CSRF by other
    means*. The claim can rot in three ways once the route it names is
    mounted, and each is reported here.

    Judged **per path**, over every route registered at it, because the
    registry is keyed by path and one path may be several routes — a page that
    serves its form on ``GET`` and takes the submission on ``POST`` is two
    ``APIRoute`` objects sharing a path, and judging them separately reported
    the ``GET`` as needing no exemption while the ``POST`` quietly relied on
    one:

    * the route is not on the browser surface at all, so the exemption was
      never doing anything;
    * **no** route at that path answers a state-changing method, so there was
      nothing to exempt;
    * **every** state-changing route at that path already carries
      ``require_csrf`` as a dependency, so the exemption is now redundant — the interesting case, because a route that
      *gains* the dependency leaves behind an entry claiming an in-route check
      that is no longer there, and the registry stops describing reality
      without anything failing.

    Only paths that are **actually mounted** are judged. An entry naming a
    path with no route is left alone, and that is a decision rather than an
    omission: :func:`~..core.make_app` mounts the operator router only when
    ``org_source_resolves_membership()``, so on a claims-sourced deployment #91's
    ``/admin`` routes are legitimately absent while its entries are correctly
    present. Failing on absence would refuse to start every claims-mode
    deployment. It would also refuse a repository mid-stack, where an entry
    lands one PR ahead of its route — which is exactly the arrangement this
    branch is in.

    That does leave a typo inert, and it is worth being clear that the cost is
    tidiness rather than safety: a misspelled entry does not exempt anything,
    so the route it was meant to cover is still caught by
    :func:`route_offence` and still fails the rollout, loudly, naming the real
    path. The unsafe direction is already closed. What this closes is the
    registry quietly ceasing to mean what it says.
    """

    from .surface import CSRF_ENFORCED_IN_ROUTE, WEB_SURFACE_PREFIXES

    if prefixes is None:
        prefixes = WEB_SURFACE_PREFIXES

    # Grouped by path, because the registry is keyed by path while a path may
    # be several routes. #91's `/admin/invitations` is the first entry where
    # that matters: it answers `GET` (the form) and `POST` (the submission) as
    # two `APIRoute` objects, and judging each alone reported the `GET` as
    # "answers no state-changing method" and refused the rollout — an exemption
    # that was doing exactly its job. A form and its own submission sharing one
    # path is the ordinary shape for a server-rendered page, so this would have
    # caught the next such page too.
    by_path: dict[str, list[APIRoute]] = {}
    for route in routes:
        path = getattr(route, "path", None)
        if not isinstance(path, str) or path not in CSRF_ENFORCED_IN_ROUTE:
            continue
        if not isinstance(route, APIRoute):
            continue
        by_path.setdefault(path, []).append(route)

    stale = []
    for path, path_routes in by_path.items():
        if not on_web_surface(path, prefixes):
            stale.append(f"{path} is not under any guarded prefix, so exempting it does nothing")
            continue
        unsafe = [route for route in path_routes if route_unsafe_methods(route)]
        if not unsafe:
            stale.append(f"{path} answers no state-changing method, so it needs no exemption")
        elif all(route_enforces_csrf(route) for route in unsafe):
            # `all`, not `any`: while one state-changing route at this path
            # still checks in-route, the entry is describing that route
            # truthfully and removing it would fail the rollout.
            stale.append(
                f"{path} already declares Depends(require_csrf), so the exemption is"
                " redundant and now misdescribes the route"
            )
    return sorted(set(stale))


def verify_csrf_exemptions_are_live(
    routes: Iterable[object], *, prefixes: Sequence[str] | None = None
) -> None:
    """Refuse a rollout whose CSRF exemptions no longer describe their routes."""

    stale = stale_csrf_exemptions(routes, prefixes=prefixes)
    if not stale:
        return
    raise RuntimeError(
        "web.surface.CSRF_ENFORCED_IN_ROUTE has entries their routes contradict:"
        f" {'; '.join(stale)}. Each entry is a reviewed claim that the route enforces"
        " CSRF in-route; remove the ones that are no longer true, so the set keeps"
        " naming exactly the routes a dependency walk cannot see."
    )


def verify_web_route_protection(
    routes: Iterable[object], *, prefixes: Sequence[str] | None = None
) -> None:
    """Refuse to serve a surface with an unprotected route. Raises on offenders.

    **This is a rollout gate, not a runtime control**, and the distinction is
    worth being exact about because this module contains one of each.

    It runs twice — once in ``make_app`` when every router it mounts is
    registered, once in the lifespan for anything a caller added afterwards —
    and then never again. A route registered after the server starts
    accepting traffic is never seen by it. So for CSRF, whose only other
    enforcement is the per-route dependency this walks for, a post-boot
    registration with no token check is genuinely unprotected and nothing here
    will say so.

    That gap is accepted rather than closed, and the reasoning is the same one
    that decided the session guard the other way. The session check became
    request-time (:class:`~.guard.WebSessionGuardMiddleware`) because route
    *structure* was being trusted as an authentication boundary and structure
    is authored by exactly the person the boundary defends against. CSRF is
    not that shape: the attacker is a cross-origin page, and a cross-origin
    page cannot register a route. The only actor who can is code already
    running in this process, and a route added post-boot is not a deployment
    path — ``make_app`` registers everything a pod serves before the lifespan
    opens.

    A request-time CSRF middleware would close it, and was considered. It
    would re-derive on every mutating request what the dependency already
    decided, to defend against a caller who by construction can do worse
    things than skip a token. That is cost with no attacker on the other side
    of it.

    So: what is enforced at runtime is the session (the guard) and CSRF (the
    route's own ``require_csrf``, or its declared in-route equivalent). What
    is enforced at rollout is that those are *present*. Do not read a passing
    startup as a claim about routes that did not exist when it ran.
    """

    routes = list(routes)
    verify_csrf_exemptions_are_live(routes, prefixes=prefixes)

    stray = stray_page_routes(routes, prefixes=prefixes)
    if stray:
        raise RuntimeError(
            f"These routes require a web session but sit outside every guarded prefix:"
            f" {', '.join(stray)}. WebSessionGuardMiddleware keys on"
            " web.surface.WEB_SURFACE_PREFIXES, so it would never see them and they would"
            " be reachable without a session. Add the prefix to WEB_SURFACE_PREFIXES."
        )

    offenders = offending_web_routes(routes, prefixes=prefixes)
    if not offenders:
        return
    reasons = "; ".join(sorted(reason for _, reason in offenders))
    raise RuntimeError(
        f"The browser surface has routes it may not serve: {reasons}. Build page"
        " routers with routers.web.session_gated_router() (or pass them to"
        " make_router as page_routers) and gate every state-changing method with"
        " Depends(web.authz.require_csrf); if a route genuinely must be anonymous,"
        " add it to web.surface.PUBLIC_WEB_PATHS, if it checks CSRF in-route, add"
        " it to web.surface.CSRF_ENFORCED_IN_ROUTE, and if a mount genuinely"
        " enforces its own session, add it to web.surface.ALLOWED_WEB_MOUNTS —"
        " all three are exemptions that get reviewed."
    )


def platform_role_source_name(org_store: object) -> str | None:
    """Name the platform-role source a store provides, or ``None``.

    Called once at startup so a deployment that cannot decide operator
    authority says so in its logs, and so the state is inspectable
    (``app.state.web_platform_role_source``) without issuing a request.
    """

    if getattr(org_store, "resolve_principal", None) is not None:
        return f"{type(org_store).__name__}.resolve_principal"
    return None


def resolve_platform_role(request: Request, user: str) -> str | None:
    """The subject's **active** platform role, or ``None`` if it holds none.

    Two sources, and the order is a security decision:

    1. the org store's ``resolve_principal`` — what issue #87 shipped: one
       call answering membership and platform role together, with a revoked
       grant already collapsed to ``None``. **This is canonical and wins.**
    2. the ``app.state`` override
       (:data:`PLATFORM_ROLE_RESOLVER_STATE_ATTR`), consulted *only* when the
       store offers no answer.

    The override used to win, which meant a stray assignment on ``app.state``
    silently outranked the server's own table — a way to grant operator
    authority that never touches ``collab_platform_roles`` and leaves no audit
    row. Demoting it costs tests nothing (they run without a
    ``resolve_principal`` store) and removes that path entirely.

    With neither source, :class:`WebAuthorizationUnavailable` is raised — not
    a quiet ``None``. Anything the store raises (an unreachable database)
    propagates untouched: "cannot answer" must never be read as "not an
    operator".

    The return value is normalized to ``str | None``. A source that answers
    with something else is a bug in that source, and a non-string is
    **refused, never compared**: an object whose ``__eq__`` returns True for
    ``"operator"`` was enough to obtain operator access when the comparison
    was made directly against whatever came back.
    """

    role = _platform_role_from_source(request, user)
    if role is None or isinstance(role, str):
        return role
    logger.error(
        "web_platform_role_not_a_string",
        extra={"user": user, "type": type(role).__name__},
    )
    return None


def _platform_role_from_source(request: Request, user: str):
    store = getattr(request.app.state, "org_store", None)
    resolve_principal = getattr(store, "resolve_principal", None)
    if resolve_principal is not None:
        return resolve_principal(user).platform_role
    resolver: PlatformRoleResolver | None = getattr(
        request.app.state, PLATFORM_ROLE_RESOLVER_STATE_ATTR, None
    )
    if resolver is not None:
        return resolver(user)
    raise WebAuthorizationUnavailable(
        "no platform-role source is configured: the organization store does not provide"
        " resolve_principal() and no platform_role_resolver override is installed"
    )


def require_operator(
    request: Request, session: WebSession = Depends(require_web_session)
) -> WebSession:
    """Dependency for pages requiring ``platform_role = 'operator'``.

    Resolved per request — a revoked operator is refused on their next
    request, session cookie or not. The comparison is ``str``-typed and exact:
    :func:`resolve_platform_role` has already refused anything that is not a
    string, so no foreign ``__eq__`` participates in this decision.
    """

    role = resolve_platform_role(request, session.user)
    if not isinstance(role, str) or role != PLATFORM_ROLE_OPERATOR:
        raise WebForbidden(f"user {session.user!r} does not hold the operator platform role")
    return session


def _org_store(request: Request) -> OrgStore:
    store = getattr(request.app.state, "org_store", None)
    if store is None:
        raise OrgsUnavailableError("Organization storage is not available on this app")
    return store


def require_org_owner(
    request: Request, session: WebSession = Depends(require_web_session)
) -> WebSession:
    """Dependency for pages requiring org ``role = 'owner'``.

    Membership is read live from the org store (a plain ``def`` dependency,
    so the blocking lookup runs in the threadpool). A store outage propagates
    as ``OrgsUnavailableError``/database errors — an unavailable answer, never
    a quiet refusal and never a quiet grant.
    """

    membership = _org_store(request).get_membership(session.user)
    if membership is None or not membership.is_active or membership.role != ROLE_OWNER:
        raise WebForbidden(f"user {session.user!r} is not an active organization owner")
    return session


async def require_csrf(
    request: Request, session: WebSession = Depends(require_web_session)
) -> WebSession:
    """Dependency: every POST of this surface passes or it does not run.

    The token is read from the ``X-CSRF-Token`` header or the ``csrf_token``
    form field (the pattern the layout's forms use), and compared in constant
    time against the secret inside the signed, HttpOnly session cookie.
    SameSite=Lax already keeps cross-site POSTs from carrying the cookie at
    all; the token is the defense that does not depend on cookie semantics.

    The form fallback is **bounded** (issue #119). It used to be
    ``request.form()``, which buffers the body to completion with no cap of
    its own — so any route that relied on this dependency could be made to
    buffer an arbitrarily large body by an authenticated caller, and #90's
    redemption endpoint had to gate its content type *before* the check to
    make the branch unreachable. The read now goes through
    :func:`~.forms.form_fields`: the body is refused above
    :data:`~.forms.MAX_FORM_BYTES` by **counting what arrives** (never from
    ``Content-Length``, which a chunked request does not carry), and
    ``multipart/form-data`` is refused outright rather than parsed — its
    parsing cost is not bounded by the byte count alone, and no form of this
    surface submits multipart. Every caller inherits the bound; no route has
    to make the fallback unreachable to be safe from it.

    The failure shapes are deliberately distinct. A refused *body* — oversize,
    or a form shape the surface does not read — raises
    :class:`~.forms.FormRefused` before the body is consumed, which
    ``routers.web.register_exception_handlers`` answers with the carried
    status (413/415) and ``Connection: close`` (see
    :mod:`.request_limits` for why a refusal issued before the read must
    close). A missing or wrong *token* — including a body of some other
    content type presenting no header, which this fallback never reads —
    stays :class:`WebForbidden` and the surface's 403 page: not being too
    large does not make a request authorized, and being too large says
    nothing about its token.
    """

    presented = request.headers.get("x-csrf-token", "")
    if not presented:
        if media_type(request) in (FORM_CONTENT_TYPE, MULTIPART_CONTENT_TYPE):
            # form_fields re-checks the content type and refuses multipart
            # (415) before reading anything; the tuple here only decides that
            # the request *claims a form*, so a JSON or absent body -- or a
            # type that merely begins with a form type (#71) -- still falls
            # through to the plain 403 below, unread.
            fields = await form_fields(request)
            presented = fields.get("csrf_token", "")
    if not csrf_token_matches(session, presented):
        raise WebForbidden(f"missing or invalid CSRF token for user {session.user!r}")
    return session


def signin_redirect_target(root_path: str, next_path: str) -> str:
    """Where a :class:`WebAuthRequired` sends the browser."""

    return f"{root_path}{SIGNIN_PATH}?{urlencode({'next': next_path})}"
