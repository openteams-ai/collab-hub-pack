"""The browser surface's security boundary: the guard authenticates, itself.

Three rounds of review taught this module its shape, and the lesson is worth
stating because it is easy to re-learn the hard way. The guard used to *prove*
that each route enforced a session, by inspecting the route's structure — its
type, its dependency tree, the identity of the callables in it. Every round,
that inspection was defeated by forging whatever it inspected: an
``APIRouter`` registered outside the construction path, a ``Mount`` hiding a
child app, a ``WebSocketRoute`` that HTTP middleware never sees, a dependency
wearing ``@wraps(require_web_session)`` while enforcing nothing, a duck-typed
object with a fabricated ``dependant``.

That game does not converge. The marker being inspected is written by exactly
the person the guard protects against — a future page author who does the
wrong thing — so each fix only moves the forgery one level down.

So the boundary is inverted. **This middleware validates the session cookie
itself**, for every request under a guarded prefix that is not explicitly
public, before the request reaches any route. It consults no route, no
dependency, and no marker. A page that forgets its dependency is therefore
authenticated anyway; a forged marker buys nothing, because nothing on the
request path reads one.

What the pieces are now:

* **This middleware — the control.** Path in, session out. Raw ASGI, because
  ``BaseHTTPMiddleware`` is never invoked for ``websocket`` scopes and a
  socket under a guarded prefix must still be refused.
* **``require_web_session`` — a convenience.** It still runs at the route and
  still gives handlers a typed :class:`~.session.WebSession`, but it is no
  longer what stands between an anonymous caller and a page.
* **``verify_web_route_protection`` — a lint.** It still shouts at startup
  about routes missing the dependency, because a missing dependency is a bug
  worth fixing. Its failure can no longer mean anonymous access, so there is
  nothing to gain by fooling it.

The startup refusals for ``Mount`` and ``WebSocketRoute`` stay real refusals:
a mounted sub-app's own paths *are* covered by this middleware, but its
routing is opaque, and sockets are outside the model entirely.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from starlette.requests import Request
from starlette.routing import get_route_path
from starlette.types import ASGIApp, Receive, Scope, Send

from ..frames.observability import REQUEST_COUNT, UNMATCHED_PATH_LABEL, access_logger
from .authz import on_web_surface, signin_redirect_target
from .pages import SECURITY_HEADERS, authorization_unavailable_page, page_response
from .session import SESSION_COOKIE
from .surface import PUBLIC_WEB_PATHS, WEB_SURFACE_PREFIXES, answers_json

logger = logging.getLogger("frames_server.web")

WEBSOCKET_POLICY_VIOLATION = 1008


class WebSessionGuardMiddleware:
    """Require a valid web session for every non-public browser-surface path."""

    def __init__(self, app: ASGIApp, *, prefixes=WEB_SURFACE_PREFIXES) -> None:
        self.app = app
        self.prefixes = tuple(prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = _relative_path(scope)
        if not on_web_surface(path, self.prefixes):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            # The surface serves no WebSockets — see the module note in
            # ``surface``. Refusing by policy rather than by route inspection
            # also covers a socket registered after startup verification ran.
            await _deny_websocket(scope, receive, send, path)
            return

        if _is_public(path):
            await self.app(scope, receive, send)
            return

        session = _session_or_none(scope, path)
        if session is _UNAVAILABLE:
            if answers_json(path):
                await _deny_json(scope, send, status_code=503, error="authorization is unavailable")
                return
            await _deny_http(scope, send)
            return
        if session is None:
            if answers_json(path):
                # A redirect would be followed by `fetch`, and the panel would
                # receive the sign-in page with status 200 -- an unauthenticated
                # caller being told, in effect, that everything is fine.
                await _deny_json(scope, send, status_code=401, error="authentication required")
                return
            await _redirect_to_signin(scope, send, path)
            return

        await self.app(scope, receive, send)


_UNAVAILABLE = object()
"""Sentinel: the session could not be *decided*, as against decided-absent."""


def _is_public(path: str) -> bool:
    """Whether *path* is one of the four routes that need no session.

    A trailing-slash variant of a public path counts, so that the router's own
    canonical-slash redirect happens instead of the guard intercepting it. It
    is not a hole: only the four public paths get this treatment, and the
    redirect the router then issues points at the same public path. Without
    it, ``/web/signin/`` answered with a sign-in redirect to ``/web/signin`` —
    which terminates, but sending someone to sign in *from* the sign-in route
    is the shape of a loop and should not be in the code at all.
    """

    if path in PUBLIC_WEB_PATHS:
        return True
    return len(path) > 1 and path.endswith("/") and path[:-1] in PUBLIC_WEB_PATHS


def _session_or_none(scope: Scope, path: str):
    """Decode the request's session, or :data:`_UNAVAILABLE` if undecidable.

    The undecidable case is its own answer rather than an exception, so the
    caller can serve the documented 503 instead of letting the failure escape
    into outer error handling as a 500 that renders no page of this surface.
    Both are refusals; only one matches what the docs promise, and only one is
    certain to carry no internals.
    """

    surface = getattr(getattr(scope.get("app"), "state", None), "web_surface", None)
    codec = getattr(surface, "codec", None)
    if codec is None:
        logger.error("web_guard_surface_unavailable", extra={"path": path})
        return _UNAVAILABLE
    try:
        cookie = Request(scope).cookies.get(SESSION_COOKIE)
        if not cookie:
            return None
        return codec.decode_session(cookie)
    except Exception:
        # decode_session answers None for every malformed cookie, so reaching
        # here means the codec itself is broken. Never a pass-through.
        logger.exception("web_guard_session_decode_failed", extra={"path": path})
        return _UNAVAILABLE


def _relative_path(scope: Scope) -> str:
    """The path the router will route, byte-identical to what it uses.

    Emphatically **not** hand-rolled. The previous version stripped
    ``root_path`` with a raw ``startswith``, while Starlette's stripping is
    segment-aware; with ``root_path="/"`` a request for ``/web/secret``
    reduced to ``web/secret``, which does not match the guarded prefix — so
    the guard skipped the request while the router happily dispatched it to
    the handler. An anonymous 200, produced entirely by two functions
    disagreeing about what "the path" means.

    Calling :func:`starlette.routing.get_route_path` is the fix, and the rule
    it embodies is the general one: a path-based control must derive its path
    from the same function the router does, not from a reimplementation that
    looks equivalent.
    """

    return get_route_path(scope)


def _next_target(scope: Scope, path: str) -> str:
    query = scope.get("query_string", b"").decode("latin-1")
    return f"{path}?{query}" if query else path


REFUSAL_HEADERS = {**SECURITY_HEADERS, "Connection": "close"}
"""Headers for a refusal this middleware issues **without reading the body**.

``Connection: close`` is the addition, and it is here rather than in
``SECURITY_HEADERS`` because it belongs to refusals specifically: a page this
surface actually serves has consumed its request and should keep its
connection.

The guard answers before routing and never touches the request body — for
every refusal, in every shape. Answering an HTTP/1.1 request whose body has
not reached end-of-message leaves the server working through what the client
is still sending before it can begin the next cycle, so the connection is held.
That was measured at about ten seconds against uvicorn on the acceptance
page's own redemption endpoint, and it is the same here, one layer earlier:
an unauthenticated ``POST /invite/accept/redeem`` carrying a body is refused
by this middleware and never reaches the handler that closes.

Unconditional rather than keyed on the method, deliberately. The invariant
that makes it correct — "this code never consumes a body" — is a property of
the middleware and holds for every request it sees, while "does this method
have a body" is a second thing to get right, on attacker-supplied input, for
no measurable gain: the cost of closing is one connection setup on a refusal,
and refusals are not the hot path.
"""


async def _redirect_to_signin(scope: Scope, send: Send, path: str) -> None:
    """Send an unauthenticated browser to sign in, returning here afterwards.

    Deliberately the same 303 that ``require_web_session`` produces: for the
    ordinary case — a person opening a page before signing in — this is not an
    error, and the guard taking over the check must not change what they see.
    """

    from starlette.responses import RedirectResponse

    root_path = (scope.get("root_path") or "").rstrip("/")
    response = RedirectResponse(
        signin_redirect_target(root_path, _next_target(scope, path)),
        status_code=303,
        headers=dict(REFUSAL_HEADERS),
    )
    await response(scope, _empty_receive, send)


async def _deny_json(scope: Scope, send: Send, *, status_code: int, error: str) -> None:
    """Refuse an admin-API request in the shape its caller can read.

    The body carries a fixed reason word and nothing else: the panel decides
    what to show from the status, and a refusal is not the place to describe
    the deployment's authorization wiring to an unauthenticated caller.
    """

    from starlette.responses import JSONResponse

    response = JSONResponse(
        {"error": error},
        status_code=status_code,
        headers=dict(REFUSAL_HEADERS),
    )
    await response(scope, _empty_receive, send)


async def _deny_http(scope: Scope, send: Send) -> None:
    response = page_response(
        authorization_unavailable_page(root_path=(scope.get("root_path") or "").rstrip("/")),
        status_code=503,
    )
    # Same reasoning as the sign-in redirect: this is a refusal issued without
    # reading the body. `page_response` is shared with the handlers that *do*
    # consume their request, so the header is added here rather than there.
    response.headers["Connection"] = "close"
    await response(scope, _empty_receive, send)


async def _deny_websocket(scope: Scope, receive: Receive, send: Send, path: str) -> None:
    """Refuse the handshake without accepting it, observably.

    The ``websocket.connect`` message is consumed first: a server that has not
    seen the client's connect event may treat an immediate close as a protocol
    error rather than delivering the refusal.

    The access log line and metrics sample are emitted here rather than
    inherited, because ``RequestObservabilityMiddleware`` is a
    ``BaseHTTPMiddleware`` and never runs for a ``websocket`` scope — so a
    socket refusal would otherwise be the one denial an operator could not
    see. Same logger, same metric, same request-id header handling as an HTTP
    request, so the two kinds of denial are countable together.

    Nothing to add for :data:`REFUSAL_HEADERS` here, and it was checked rather
    than assumed: a handshake carries no body, and this refusal closes the
    connection outright rather than answering on it.
    """

    request_id = uuid4().hex
    client_request_id = _client_request_id(scope)
    fields = {"request_id": request_id, "path": path}
    if client_request_id is not None:
        fields["client_request_id"] = client_request_id
    logger.warning("web_websocket_refused", extra=fields)
    # The metric label is a constant, never the requested path: a socket
    # refusal is pre-routing, so there is no route template, and labelling by
    # raw path let an unauthenticated client mint one series per path it
    # invented. See UNMATCHED_PATH_LABEL.
    REQUEST_COUNT.labels(
        method="WEBSOCKET",
        path=UNMATCHED_PATH_LABEL,
        status=str(WEBSOCKET_POLICY_VIOLATION),
    ).inc()
    access_logger.info(
        "request",
        extra={**fields, "method": "WEBSOCKET", "status_code": WEBSOCKET_POLICY_VIOLATION, "duration_ms": 0.0},
    )
    message = await receive()
    if message["type"] == "websocket.connect":
        await send({"type": "websocket.close", "code": WEBSOCKET_POLICY_VIOLATION})


CLIENT_REQUEST_ID_MAX_LENGTH = 128
_CLIENT_REQUEST_ID_ALLOWED = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


def _client_request_id(scope: Scope) -> str | None:
    """The caller's ``X-Request-ID``, as an explicitly untrusted field.

    The correlation id this surface *records* is always server-generated. A
    socket refusal is reachable without credentials, so honouring an inbound
    id would let anyone stamp their own requests with a known id and forge
    correlation — making a victim's trail and an attacker's indistinguishable
    in the very records an operator would use to investigate.

    The client's value is still worth keeping when a real proxy set it, so it
    is recorded under a separate name that says whose it is, bounded in length
    and restricted to an id-shaped alphabet so it cannot bloat or reshape a
    log line.
    """

    for name, value in scope.get("headers", ()):
        if name != b"x-request-id":
            continue
        candidate = value.decode("latin-1", "replace")
        if not candidate or len(candidate) > CLIENT_REQUEST_ID_MAX_LENGTH:
            return None
        if not _CLIENT_REQUEST_ID_ALLOWED.issuperset(candidate):
            return None
        return candidate
    return None


async def _empty_receive() -> dict:
    return {"type": "http.disconnect"}


__all__ = ["WebSessionGuardMiddleware"]
