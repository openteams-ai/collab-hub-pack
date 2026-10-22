"""Per-path request protection driven by the configured protection map.

Route-level dependencies protect the API routers, but they cannot protect what
they do not decorate: ``/`` and ``/metrics`` were reachable without credentials
and relied on the Nebari gateway (``enforceAtGateway``) to keep browsers out.
Behind an ordinary Ingress there is no gateway, so that reliance turns into a
public metrics endpoint.

The fix is a middleware that consults a protection map — a list of path rules
supplied as configuration — before the request is routed, and a default that
applies to every path no rule matches. The map is data so the browser surface
coming in issue #88 can add ``/invite/accept`` as public, or an operator can
open ``/metrics`` to an in-cluster scraper, without touching this module.

Matching is longest-match with exact rules winning over prefix rules, so a
broad ``/`` prefix rule can be narrowed by a more specific entry. Among rules
of equal specificity the last one listed wins, which lets an operator append an
override to the built-in map rather than restate it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import get_route_path

from .config import PathAccess, PathRule

API_PATH_PREFIXES = (
    "/v1/frames",
    "/v1/active-frames",
    "/v1/frame-groups",
    "/v1/user-directory",
    "/v1/usage",
    "/frames",
    "/active-frames",
    "/frame-groups",
    "/usage",
    "/v1/tasks",
    "/v1/task-devices",
    "/v1/task-notifications",
    "/v1/runs",
    "/v1/task-runs",
    # The Cog catalog read API (issue #85): its 404 and 503 states carry
    # machine-readable codes (`cog_not_found`, `cog_catalog_unavailable`).
    "/v1/cogs",
    # The invitation surface (issue #89). Its terminal states are carried
    # entirely by the error envelope's machine-readable `code` — the
    # acceptance page renders "the link expired" and "sign in with the other
    # address" differently — so it must never fall back to the plain
    # `{"detail": ...}` shape.
    "/v1/invitations",
    "/v1/operator/invitations",
    "/v1/orgs",
)


def api_path(path: str) -> bool:
    """Whether a path belongs to the machine API and uses its error envelope."""

    return path.startswith(API_PATH_PREFIXES)


def request_path(request: Request) -> str:
    """The request path relative to the app, byte-identical to what the router routes.

    When the app is served under a prefix (``server.root_path``), a proxy that
    does not strip the prefix leaves it on ``scope["path"]``. Rules are written
    against the app's own paths, so the prefix has to come off before matching
    — otherwise a prefixed deployment matches nothing and every path falls to
    the default.

    That strip is :func:`starlette.routing.get_route_path`, the function the
    router itself calls, and deliberately not a reimplementation of it. The
    previous version used a raw ``startswith``, which is not segment-aware:
    with ``root_path="/"`` a request for ``/metrics`` reduced to ``metrics``,
    matched no rule and fell to the default, while the router still served
    ``/metrics``. Under ``default_access="public"`` that turned an
    ``authenticated`` entry into an anonymous 200. A path-based control must
    derive its path from the same function the router does (``web/guard.py``
    records the same rule for the browser surface).
    """

    return get_route_path(request.scope)


def _matches(rule: PathRule, path: str) -> bool:
    if rule.match == "exact":
        return path == rule.path
    if rule.path == "/":
        return True
    base = rule.path.rstrip("/")
    # Segment-aware so a /v1 rule covers /v1 and /v1/frames but not /v1beta.
    return path == base or path.startswith(base + "/")


def _specificity(rule: PathRule) -> tuple[int, int]:
    return (1 if rule.match == "exact" else 0, len(rule.path.rstrip("/")))


def winning_rule(path: str, rules: Iterable[PathRule]) -> PathRule | None:
    """The rule that decides ``path``, or ``None`` when no rule matches it (the default applies)."""

    best: PathRule | None = None
    for rule in rules:
        if not _matches(rule, path):
            continue
        # >= so a later rule of equal specificity wins, letting operators
        # append an override instead of rewriting the built-in map.
        if best is None or _specificity(rule) >= _specificity(best):
            best = rule
    return best


def resolve_access(path: str, rules: Iterable[PathRule], default_access: PathAccess) -> PathAccess:
    """Return the access level configured for ``path``."""

    best = winning_rule(path, rules)
    return best.access if best is not None else default_access


def default_unauthorized_response(request: Request, exc: HTTPException) -> Response:
    """Render a refused credential check in the shape the requested surface uses.

    An exception carrying its own ``error_code`` always gets the machine
    readable envelope, whatever surface it was requested on: a code exists
    precisely so the client can tell that state apart from a plain refusal, and
    dropping it on the page surfaces would make the answer depend on which URL
    happened to be asked for.
    """

    code = getattr(exc, "error_code", None)
    if code or api_path(request_path(request)):
        return JSONResponse(
            {"error": {"code": code or "unauthorized", "message": str(exc.detail)}},
            status_code=exc.status_code,
        )
    return JSONResponse({"detail": str(exc.detail)}, status_code=exc.status_code)


class PathProtectionMiddleware(BaseHTTPMiddleware):
    """Enforce the protection map before a request reaches its route.

    Two things about *where* this runs shape the code below. It sits outside
    Starlette's ``ExceptionMiddleware``, so the app's registered exception
    handlers never see anything raised here — every failure mode has to be
    turned into a response in this class or it becomes a 500. And it is an
    async middleware calling a synchronous ``authenticate``, which a route
    dependency would have had dispatched to a threadpool for free.
    """

    def __init__(
        self,
        app,
        *,
        rules: Sequence[PathRule],
        default_access: PathAccess = "authenticated",
        authenticate: Callable[[Request], object],
        unauthorized_response: Callable[[Request, HTTPException], Response] = default_unauthorized_response,
        authenticate_error_response: Callable[[Request, Exception], Response | None] | None = None,
    ) -> None:
        super().__init__(app)
        self.rules = list(rules)
        self.default_access = default_access
        self.authenticate = authenticate
        self.unauthorized_response = unauthorized_response
        # Maps a non-HTTP failure of the credential check — an unavailable
        # backing store, say — onto a response. Returning None (the default)
        # re-raises, so a genuine bug still surfaces as a 500 rather than being
        # laundered into a plausible-looking error.
        self.authenticate_error_response = authenticate_error_response

    async def dispatch(self, request: Request, call_next):
        if resolve_access(request_path(request), self.rules, self.default_access) == "public":
            return await call_next(request)
        try:
            # Threadpool because `authenticate` is synchronous and may block:
            # resolving the caller's organization is a database round trip, and
            # when that database is unreachable it blocks for the pool's wait
            # timeout. Called inline, one such request would stall the event
            # loop — and therefore every other request on this worker — instead
            # of each one independently answering 503.
            await run_in_threadpool(self.authenticate, request)
        except HTTPException as exc:
            return self.unauthorized_response(request, exc)
        except Exception as exc:
            response = self.authenticate_error_response(request, exc) if self.authenticate_error_response else None
            if response is None:
                raise
            return response
        return await call_next(request)
