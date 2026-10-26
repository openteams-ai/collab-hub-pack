"""Shared page scaffolding for the browser surface.

This is a dedicated operational surface, not a product frontend: server-
rendered HTML, one stylesheet served as its own route, and **no JavaScript at
all** — every page works as plain documents and forms, so the CSP can forbid
script outright (``script-src`` is absent and ``default-src 'none'``). The
future pages (#90–#92) compose their bodies with :func:`render_page` and
inherit the layout, headers, and CSRF form field without re-deciding any of
this.

Every dynamic value is escaped with :func:`html.escape` at the point it is
interpolated. Pages of this surface handle no invitation secrets — the
acceptance page's fragment-only token handling is its own issue — but the
headers already establish what it needs: ``Referrer-Policy: no-referrer`` on
every response, ``Cache-Control: no-store``, ``X-Frame-Options: DENY`` and a
``frame-ancestors 'none'`` CSP against clickjacked admin forms.
"""

from __future__ import annotations

import html
import logging
from urllib.parse import quote

from fastapi.responses import HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import get_route_path

from .surface import STYLE_ASSET_PATH as STYLE_PATH
from .surface import WEB_LOGO_PATH as LOGO_PATH

logger = logging.getLogger("frames_server.web")

# STYLE_PATH is the name this module and `routers.web` have always used; it is
# now bound to the single definition in `web.surface` rather than a second
# spelling of the same literal. The two were independent constants, and only
# the surface one feeds PUBLIC_WEB_PATHS and the startup precondition — so
# editing this one alone would have moved the stylesheet's route without
# moving its public exemption, quietly making the stylesheet require a session
# and rendering every page of the surface unstyled. An import cannot drift.

SECURITY_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
"""On every response of the surface, redirects and assets included, so an
intermediary treats the whole surface alike."""

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; style-src 'self'; img-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
"""No script source at all: the surface serves none, so none may run.

``img-src`` is ``'self'`` rather than ``'none'``: these pages carry the product
wordmark, served from this origin by the route that serves the stylesheet.
Widened by exactly one source, and not to a brand CDN -- a page that fetches
its own chrome from a third party hands that third party a view of who is
opening it.

``form-action 'self'`` keeps a markup-injection bug from redirecting a POST
(and the CSRF token in it) off-origin. Documents only — assets get the plain
security headers.

**This is the default and it stays the default.** Exactly one page differs —
the invitation-acceptance page, which cannot read its URL fragment without
script — and it differs *for its own path only*, through
:func:`headers_for_path`, with a hash-pinned ``script-src``. If you are here
because the surface looks inconsistent, read :mod:`.acceptance`: the fix is
never to add a script source here.
"""

PAGE_HEADERS = {**SECURITY_HEADERS, "Content-Security-Policy": CONTENT_SECURITY_POLICY}

ADMIN_PANEL_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
"""The admin panel's policy: still ``default-src 'none'``, widened to ``'self'``.

The second exception to the no-script rule, and the first that is a whole
subtree rather than one page. It is stated as a full policy rather than as a
diff against :data:`CONTENT_SECURITY_POLICY`, so that reading this constant
tells you everything the panel is permitted to do.

What it does **not** contain is the point:

* no ``'unsafe-inline'`` and no ``'unsafe-eval'`` -- the panel is a built
  bundle, so every script and stylesheet is a file on this origin;
* no external origin anywhere, fonts included. The typeface is bundled into
  the build rather than fetched from a font CDN, which keeps this policy free
  of third-party hosts and lets a hub with no internet egress render normally;
* ``form-action 'none'``, because the panel submits no HTML forms at all -- it
  writes over ``fetch`` to same-origin JSON. The pages that do post forms keep
  ``form-action 'self'``; this one can afford to forbid it outright.

``connect-src 'self'`` is what lets the panel call its own API, and naming it
explicitly (rather than relying on a wider ``default-src``) means an attempt to
exfiltrate to another origin is refused by the browser.
"""

ADMIN_PANEL_HEADERS = {**SECURITY_HEADERS, "Content-Security-Policy": ADMIN_PANEL_CONTENT_SECURITY_POLICY}


def headers_for_path(path: str) -> dict[str, str]:
    """The response headers this surface serves for *path*.

    A **path**-keyed decision, deliberately, not a flag a handler sets on its
    response. Same reasoning as the session guard's (see :mod:`.guard`): a
    per-response marker is authored by the same future page author the policy
    exists to constrain, so it would travel wherever someone copied it. A
    path cannot travel.

    Every path answers with :data:`PAGE_HEADERS` except the acceptance page,
    which answers with the same headers and a CSP whose only addition is one
    SHA-256 script digest and ``connect-src 'self'``.
    """

    from .surface import on_admin_panel

    if on_admin_panel(path):
        # Scoped to the panel's own document and bundle files, not to all of
        # ``/admin``: the policy below forbids form submission outright, which
        # the operator invitation page could not live under.
        return ADMIN_PANEL_HEADERS
    return _script_page_headers().get(path, PAGE_HEADERS)


def _script_page_headers() -> dict[str, dict[str, str]]:
    """The path → headers exceptions. One entry, and it is reviewed.

    Imported inside the function because :mod:`.acceptance` composes its page
    with :func:`render_page` from this module; a module-level import would be
    a cycle. Same pattern the authorization lint uses for ``surface``.
    """

    from .acceptance import ACCEPT_PAGE_PATH, ACCEPTANCE_PAGE_HEADERS

    return {ACCEPT_PAGE_PATH: ACCEPTANCE_PAGE_HEADERS}

STYLESHEET = """\
:root { color-scheme: light dark; }
/* The acceptance page ships every outcome as a hidden section and reveals
   one. A later rule that set `display` on `section` would defeat the `hidden`
   attribute and show all of them at once, so this pins it. */
[hidden] { display: none !important; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
       Helvetica, Arial, sans-serif; margin: 4rem auto; max-width: 40rem;
       padding: 0 1.25rem; color: #1a1a2e; line-height: 1.55; }
h1 { font-size: 1.35rem; margin: 0 0 1rem; }
p { margin: 0 0 0.9rem; }
a { color: #3452d9; }
.brand { margin-bottom: 2rem; }
.brand img { height: 2rem; width: auto; display: block; }
.identity { color: #666; font-size: 0.85rem; margin-top: 3rem;
            border-top: 1px solid #ddd; padding-top: 1rem; }
.identity form { display: inline; }
button { font: inherit; background: #3452d9; color: #fff; border: 0;
         border-radius: 6px; padding: 0.5rem 1rem; cursor: pointer; }
button.link { background: none; color: #3452d9; padding: 0;
              text-decoration: underline; }
dl { margin: 0 0 1rem; }
dt { color: #666; font-size: 0.8rem; margin-top: 0.75rem; }
dd { margin: 0.15rem 0 0; }
h2 { font-size: 1.05rem; margin: 2rem 0 0.75rem; }
label { display: block; font-size: 0.8rem; color: #666; margin-bottom: 0.25rem; }
input[type="email"], input[type="text"] { font: inherit; width: 100%; box-sizing: border-box;
       padding: 0.5rem; border: 1px solid #bbb; border-radius: 6px;
       margin-bottom: 0.75rem; background: transparent; color: inherit; }
.notice { border-left: 3px solid #3452d9; padding-left: 0.75rem; }
table { border-collapse: collapse; width: 100%; font-size: 0.85rem; }
th { text-align: left; color: #666; font-weight: 600; }
th, td { border-bottom: 1px solid #ddd; padding: 0.4rem 0.5rem 0.4rem 0; }
form.inline { display: inline; }
@media (prefers-color-scheme: dark) {
  /* The only wordmark that ships is navy and vanishes on a dark background;
     flattening and inverting gives a white mark of the same shape. The panel
     and the desktop client do the same. */
  .brand img { filter: brightness(0) invert(1); }
  body { color: #e8e8f0; }
  .brand, .identity, dt, label, th { color: #9a9aa8; }
  .identity { border-top-color: #3a3a48; }
  a, button.link { color: #96a9ff; }
  input[type="email"], input[type="text"] { border-color: #4a4a58; }
  th, td { border-bottom-color: #3a3a48; }
}
"""


_ERROR_DOCUMENT = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="referrer" content="no-referrer">
<title>Something went wrong</title></head>
<body><h1>Something went wrong</h1>
<p>The page could not be produced. Please try again, and tell an
administrator if it keeps happening.</p></body>
</html>
"""
"""Deliberately not built with :func:`render_page`: this is the response for
"a page raised", so it must not itself depend on the layout, the stylesheet
route, or anything else that could be what failed."""


class WebSecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply the surface's security headers to **every** response it owns.

    Per-response headers were not enough, and the gap was not theoretical: a
    redirect, an asset, a 405 from an unsupported method, and any unmatched
    ``/web/*`` path (the router's own 404) all produced responses that
    the route handlers never touched, and so answered without
    ``Referrer-Policy``, without ``no-store``, and without a CSP.

    Running as middleware makes the claim structural: the headers follow the
    *path*, not the handler, so a response nobody in this package wrote still
    carries them. Added outermost in ``make_app`` so it also covers the
    credential refusals raised by the path-protection middleware.

    Headers already set by a handler are overwritten rather than merged, which
    is safe because the values are the same constants either way — and it
    means a future handler cannot weaken the policy by accident.
    """

    def __init__(self, app, *, prefixes=("/web",)) -> None:
        super().__init__(app)
        self.prefixes = tuple(prefix.rstrip("/") for prefix in prefixes)

    def _applies(self, request) -> bool:
        # Same path function the router and the session guard use. A
        # hand-rolled root_path strip disagreed with Starlette's segment-aware
        # one, and the disagreement was a live bypass in the guard; there is
        # no reason to keep a second copy of it here to rot the same way.
        path = get_route_path(request.scope)
        return any(path == prefix or path.startswith(prefix + "/") for prefix in self.prefixes)

    async def dispatch(self, request, call_next):
        applies = self._applies(request)
        try:
            response = await call_next(request)
        except Exception:
            if not applies:
                raise
            # ``ServerErrorMiddleware`` is *outside* this one, so an exception
            # that escapes here becomes a 500 built above us and never passes
            # back through — which is how a failing page answered with no CSP,
            # no Referrer-Policy, and no no-store. Answering it here keeps the
            # surface's headers on its worst-case response. The traceback is
            # logged, never rendered: this is a browser surface.
            #
            # ``PAGE_HEADERS`` unconditionally, including on the acceptance
            # path: this document carries no script, so the strictest policy
            # is the correct one and a failing page must not be the thing
            # that hands out a script budget.
            logger.exception("web_unhandled_error", extra={"path": request.url.path})
            return HTMLResponse(
                _ERROR_DOCUMENT,
                status_code=500,
                headers=PAGE_HEADERS,
            )
        if applies:
            for name, value in headers_for_path(get_route_path(request.scope)).items():
                response.headers[name] = value
        return response


def render_page(
    *,
    title: str,
    body: str,
    root_path: str = "",
    identity_label: str | None = None,
    csrf_token: str | None = None,
) -> str:
    """Render one page of the surface into a complete document.

    ``body`` is trusted page markup composed by a route in this codebase —
    every request- or store-derived value must already be escaped by the
    caller (:func:`escape` is the one to use). ``title`` and
    ``identity_label`` are escaped here because they are routinely dynamic.

    When ``identity_label`` and ``csrf_token`` are given, the layout appends
    the signed-in footer with the sign-out form; the CSRF token rides a
    hidden field of that form, which is the pattern every future POST form on
    this surface follows.
    """

    footer = ""
    if identity_label is not None and csrf_token is not None:
        footer = (
            '<div class="identity">Signed in as '
            f"<strong>{html.escape(identity_label)}</strong> · "
            f'<form method="post" action="{html.escape(root_path)}/web/signout">'
            f'<input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">'
            '<button type="submit" class="link">Sign out</button>'
            "</form></div>"
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta name="robots" content="noindex, nofollow">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="{html.escape(root_path)}{STYLE_PATH}">
</head>
<body>
<div class="brand"><img src="{html.escape(root_path)}{LOGO_PATH}" alt="OpenTeams Collab"></div>
<main>
{body}
{footer}
</main>
</body>
</html>
"""


def escape(value: str | None) -> str:
    """The escape every route uses for request- or store-derived text."""

    return html.escape(value or "")


def page_response(document: str, *, status_code: int = 200, path: str | None = None) -> HTMLResponse:
    """One page of the surface, with the headers its *path* is entitled to.

    ``path`` is the surface path the document is being served for. The
    security-header middleware overwrites these headers anyway — it is the
    control — but a handler that names its own path answers correctly even in
    a test that calls it directly, and the two can never disagree because
    both read :func:`headers_for_path`.
    """

    headers = PAGE_HEADERS if path is None else headers_for_path(path)
    return HTMLResponse(document, status_code=status_code, headers=headers)


def forbidden_page(*, root_path: str = "") -> str:
    return render_page(
        title="You don't have access to this page",
        body=(
            "<h1>You don't have access to this page</h1>"
            "<p>Your account is signed in, but it does not hold the role this"
            " page requires. If you believe it should, contact your"
            " organization owner or a platform operator.</p>"
            f'<p><a href="{html.escape(root_path)}/web">Back to overview</a></p>'
        ),
        root_path=root_path,
    )


def body_refused_page(*, root_path: str = "", status_code: int) -> str:
    """The surface's page for a request body it declined to read (issue #119).

    Rendered by the app-level handler for :class:`~.forms.FormRefused` when
    the refusal escapes ``require_csrf``'s bounded form fallback — that is,
    from any route that took the dependency rather than parsing its own form.
    The invitation pages never reach it: they parse their own forms and answer
    a refusal with their own page and its "back" link. This one cannot know
    which form the body came from, so its copy is generic and its link is the
    overview.

    One page for both statuses, with the heading naming which, the same shape
    as :func:`~.forms.refused_form_page`. Neither says anything about the
    submitted content — there is nothing useful to quote, and an echoed body
    is a body in a response.
    """

    if status_code == 415:
        heading = "That request could not be read"
        detail = (
            "This page accepts an ordinary form submission and nothing else."
            " Use the form on the page you came from rather than posting to"
            " it directly."
        )
    else:
        heading = "That request was too large"
        detail = (
            "The form you sent is larger than this page will read, so nothing"
            " was changed."
        )
    return render_page(
        title=heading,
        body=(
            f"<h1>{html.escape(heading)}</h1>"
            f"<p>{html.escape(detail)}</p>"
            f'<p><a href="{html.escape(root_path)}/web">Back to overview</a></p>'
        ),
        root_path=root_path,
    )


def sign_in_failed_page(*, root_path: str = "") -> str:
    return render_page(
        title="Sign-in did not complete",
        body=(
            "<h1>Sign-in did not complete</h1>"
            "<p>We could not finish signing you in. Nothing about your account"
            " has changed.</p>"
            f'<p><a href="{html.escape(root_path)}/web/signin">Try signing in again</a></p>'
        ),
        root_path=root_path,
    )


def authorization_unavailable_page(*, root_path: str = "") -> str:
    return render_page(
        title="This page is temporarily unavailable",
        body=(
            "<h1>This page is temporarily unavailable</h1>"
            "<p>The service cannot check what you are allowed to do right now,"
            " so it has not let the request through. This is a problem on our"
            " side, not with your account.</p>"
            "<p>Please try again shortly, and tell an administrator if it"
            " keeps happening.</p>"
            f'<p><a href="{html.escape(root_path)}/web">Back to overview</a></p>'
        ),
        root_path=root_path,
    )


def signed_out_page(*, root_path: str = "", next_path: str | None = None) -> str:
    """The page after sign-out. *next_path*, already sanitized by the caller,
    rides on the sign-in link so signing in again returns to where the person
    left, such as the admin panel."""

    signin = f"{root_path}/web/signin"
    if next_path:
        signin += f"?next={quote(next_path, safe='')}"
    return render_page(
        title="Signed out",
        body=(
            "<h1>Signed out</h1>"
            "<p>Your session on this browser has ended.</p>"
            f'<p><a href="{html.escape(signin)}">Sign in again</a></p>'
        ),
        root_path=root_path,
    )
