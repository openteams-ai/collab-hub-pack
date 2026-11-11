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
from typing import TYPE_CHECKING
from urllib.parse import quote

from fastapi import Request, Response
from fastapi.responses import HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import get_route_path

from .surface import (
    ADMIN_PANEL_DOCUMENT,
    LANDING_PATH,
    ORG_INVITATIONS_PATH,
    THEME_PATH,
)
from .surface import STYLE_ASSET_PATH as STYLE_PATH

if TYPE_CHECKING:
    from .authz import ViewerRoles
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
    "default-src 'none'; style-src 'self'; img-src 'self'; font-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
"""No script source at all: the surface serves none, so none may run.

``img-src`` and ``font-src`` are ``'self'`` rather than ``'none'``: these pages
carry the product wordmark and are set in the product's typeface, both served
from this origin (the wordmark by the route beside the stylesheet, the font
files by the registration bundle). Widened to this origin only, and not to a
brand or font CDN -- a page that fetches its own chrome from a third party hands
that third party a view of who is opening it.

``form-action 'self'`` keeps a markup-injection bug from redirecting a POST
(and the CSRF token in it) off-origin. Documents only — assets get the plain
security headers.

**This is the default and it stays the default.** The two built bundles
differ (the admin panel, and the registration app that serves the
invitation-acceptance page, which cannot read its URL fragment without
script), and they differ *for their own paths only*, through
:func:`headers_for_path`. If you are here because the surface looks
inconsistent, read :data:`ADMIN_PANEL_CONTENT_SECURITY_POLICY`: the fix is
never to add a script source here.
"""

PAGE_HEADERS = {**SECURITY_HEADERS, "Content-Security-Policy": CONTENT_SECURITY_POLICY}

ADMIN_PANEL_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
"""The admin panel's policy: still ``default-src 'none'``, widened to ``'self'``.

The exception to the no-script rule, for a built bundle's document and its
files. It is stated as a full policy rather than as a diff against
:data:`CONTENT_SECURITY_POLICY`, so that reading this constant tells you
everything the panel is permitted to do.

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

REGISTRATION_APP_HEADERS = ADMIN_PANEL_HEADERS
"""The registration app answers with the panel's policy, by name.

It is the same kind of thing -- a bundle built by ``admin-ui``, served from
this origin, writing over ``fetch`` -- so it needs exactly what the panel needs
and nothing the panel does not. One policy for both means a directive loosened
for one is visibly loosened for the other; the name is separate so that a
reader of :func:`headers_for_path` sees which surface each branch is for.

This replaced a policy that pinned one inline script by its SHA-256 digest. A
bundle cannot be pinned that way: its scripts are files, so ``script-src`` has
to name their origin. What is kept is everything else -- no inline script, no
``eval``, no external origin, and ``connect-src 'self'`` so the page can talk
only to this hub.
"""


def headers_for_path(path: str) -> dict[str, str]:
    """The response headers this surface serves for *path*.

    A **path**-keyed decision, deliberately, not a flag a handler sets on its
    response. Same reasoning as the session guard's (see :mod:`.guard`): a
    per-response marker is authored by the same future page author the policy
    exists to constrain, so it would travel wherever someone copied it. A
    path cannot travel.

    Every path answers with :data:`PAGE_HEADERS` except the two built
    bundles, whose documents and files answer with the same headers and a CSP
    that lets them run their own script.
    """

    from .surface import on_admin_panel, on_registration_app

    if on_admin_panel(path):
        # Scoped to the panel's own document and bundle files, not to all of
        # ``/admin``: the policy below forbids form submission outright, which
        # the operator invitation page could not live under.
        return ADMIN_PANEL_HEADERS
    if on_registration_app(path):
        # The same shape and the same scoping: the document and its bundle
        # files, not all of ``/invite``.
        return REGISTRATION_APP_HEADERS
    return PAGE_HEADERS


STYLESHEET = """\
/* The server-rendered pages, in the same face and colours as the React
   bundles beside them (admin-ui/src/tokens.css is the other copy of these
   values; the two surfaces of one product must not disagree about what blue
   means). Fonts are the registration bundle's own files, served from this
   origin: no font host, and a hub with no internet egress still renders them.
   admin-ui/web-fonts.json lists the files named here, and the admin-ui build
   fails if the bundle does not contain each one.
   Nothing in this interface is capitalised for emphasis. */
@font-face { font-family: "Inter"; font-style: normal; font-weight: 400; font-display: swap;
  src: url(../invite/assets/inter-latin-400-normal.woff2) format("woff2");
  unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304,
    U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD; }
@font-face { font-family: "Inter"; font-style: normal; font-weight: 500; font-display: swap;
  src: url(../invite/assets/inter-latin-500-normal.woff2) format("woff2");
  unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304,
    U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD; }
@font-face { font-family: "Inter"; font-style: normal; font-weight: 600; font-display: swap;
  src: url(../invite/assets/inter-latin-600-normal.woff2) format("woff2");
  unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304,
    U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD; }

:root {
  color-scheme: light dark;
  --font-sans: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --font-mono: "IBM Plex Mono", ui-monospace, Menlo, monospace;
  --ink: #1a1a2e; --ink-soft: #666; --accent: #3452d9; --line: #ddd;
  --warn: #8a2f2f; --page: #fff; --on-accent: #fff; --tint: rgba(52, 82, 217, 0.06);
}
/* The system says dark and nobody has chosen otherwise. */
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ink: #e8e8f0; --ink-soft: #9a9aa8; --accent: #96a9ff; --line: #3a3a48;
    --warn: #ff9b9b; --page: #111116; --on-accent: #14161c; --tint: rgba(150, 169, 255, 0.08);
  }
  /* The only wordmark that ships is navy and vanishes on a dark background;
     flattening and inverting gives a white mark of the same shape. The panel
     and the desktop client do the same. */
  :root:not([data-theme="light"]) .brand img,
  :root:not([data-theme="light"]) .wordmark { filter: brightness(0) invert(1); }
}
/* An explicit choice, recorded by the switch in the header, outranks the
   system in both directions. The same cookie the admin panel reads. */
:root[data-theme="dark"] {
  color-scheme: dark;
  --ink: #e8e8f0; --ink-soft: #9a9aa8; --accent: #96a9ff; --line: #3a3a48;
  --warn: #ff9b9b; --page: #111116; --on-accent: #14161c; --tint: rgba(150, 169, 255, 0.08);
}
:root[data-theme="dark"] .brand img, :root[data-theme="dark"] .wordmark { filter: brightness(0) invert(1); }
:root[data-theme="light"] { color-scheme: light; }
:root[data-theme="light"] .brand img, :root[data-theme="light"] .wordmark { filter: none; }

* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
       font-family: var(--font-sans); font-size: 15px; line-height: 1.55; }
h1 { font-size: 1.35rem; font-weight: 600; line-height: 1.3; margin: 0 0 0.75rem; }
h2 { font-size: 1.05rem; font-weight: 600; margin: 2rem 0 0.75rem; }
p { margin: 0 0 1rem; }
a { color: var(--accent); text-underline-offset: 0.15em; }
a:focus-visible, button:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.mono { font-family: var(--font-mono); }

/* A page with nobody signed in (the signed-out notice, the public documents)
   stands alone: one readable column with the same margin on every side. */
.standalone { max-width: 38rem; margin: 0 auto; padding: 4rem 1.5rem; font-size: 16px; line-height: 1.6; }
.standalone h1 { font-size: 1.5rem; margin-bottom: 1.25rem; }
.standalone .brand { margin-bottom: 3rem; }
.brand img, .wordmark { height: 2.25rem; width: auto; display: block; }

/* The signed-in frame: the admin panel's header, side navigation and main
   column (admin-ui/src/styles.css, sections.css), so the two surfaces read as
   one product. */
.app { min-height: 100vh; display: flex; flex-direction: column; }
.header { display: flex; align-items: center; justify-content: space-between; gap: 1.5rem;
          padding: 1rem 1.75rem; border-bottom: 1px solid var(--line); }
.header-brand { display: flex; align-items: center; gap: 0.85rem; min-width: 0; }
.header-surface { font-size: 0.9rem; color: var(--ink-soft); padding-left: 0.85rem;
                  border-left: 1px solid var(--line); }
.header-identity { display: flex; align-items: center; gap: 0.75rem; font-size: 0.9rem; min-width: 0; }
.header-identity .who { color: var(--ink); overflow-wrap: anywhere; }
.avatar { flex: none; display: grid; place-items: center; width: 2rem; height: 2rem; border-radius: 50%;
          background: color-mix(in srgb, var(--accent) 18%, transparent); color: var(--accent);
          font-size: 0.75rem; font-weight: 600; user-select: none; }
.shell { display: grid; grid-template-columns: 15rem 1fr; flex: 1; }
.nav { border-right: 1px solid var(--line); padding: 1.5rem 1rem; display: flex; flex-direction: column; gap: 1.75rem; }
.nav ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.125rem; }
.navlink { display: flex; align-items: center; gap: 0.6rem; padding: 0.4rem 0.6rem; border-radius: 6px;
           color: var(--ink); text-decoration: none; font-size: 0.9rem; }
.navlink:hover { background: color-mix(in srgb, var(--ink) 7%, transparent); }
.nav svg { flex: none; color: var(--ink-soft); }
.navlink:hover svg { color: var(--ink); }
.navlink.current { background: color-mix(in srgb, var(--accent) 12%, transparent);
                   color: var(--accent); font-weight: 600; }
.navlink.current svg { color: var(--accent); }
.main { padding: 2.5rem 2.5rem 4rem; max-width: 64rem; }
.main p { color: var(--ink-soft); max-width: 44rem; }
.main form { max-width: 34rem; }


/* The one action a page is asking for. */
button { font: inherit; font-weight: 500; background: var(--accent); color: var(--on-accent); border: 0;
         border-radius: 6px; padding: 0.45rem 0.9rem; cursor: pointer; white-space: nowrap; }
button:disabled { background: color-mix(in srgb, var(--ink) 12%, transparent);
                  color: var(--ink-soft); cursor: default; }
button.link { font-weight: 400; background: none; color: var(--accent); padding: 0;
              text-decoration: underline; text-underline-offset: 0.15em; white-space: normal; }
/* Icon-only controls carry their own accessible name, since there is no text
   beside them to borrow one from. */
button.icon-button { display: flex; align-items: center; justify-content: center; width: 2rem; height: 2rem;
                     padding: 0; border: 1px solid var(--line); border-radius: 6px; background: none;
                     color: var(--ink-soft); }
button.icon-button:hover { color: var(--ink); border-color: var(--ink-soft); }
form.inline { display: inline; }
/* With no theme chosen both switches are in the page and the system decides
   which shows: the one that leads away from what the system is showing. */
:root:not([data-theme]) form.theme-switch.to-light { display: none; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme]) form.theme-switch.to-dark { display: none; }
  :root:not([data-theme]) form.theme-switch.to-light { display: inline; }
}

dl { margin: 0 0 1rem; }
dt { color: var(--ink-soft); font-size: 0.85rem; margin-top: 0.9rem; }
dd { margin: 0.15rem 0 0; }
label { display: block; font-size: 0.8rem; color: var(--ink-soft); margin-bottom: 0.35rem; }
input[type="email"], input[type="text"] { font: inherit; width: 100%; padding: 0.5rem 0.75rem;
       border: 1px solid var(--line); border-radius: 6px; margin-bottom: 0.9rem;
       background: transparent; color: inherit; }

/* What a page has to say about the last thing that happened. */
.notice { border-left: 3px solid var(--accent); background: var(--tint); border-radius: 0 8px 8px 0;
          padding: 0.85rem 1rem; margin: 0 0 1.25rem; max-width: 44rem; }
.notice p:last-child { margin-bottom: 0; }

table { border-collapse: collapse; width: 100%; margin: 1.25rem 0 2rem; font-size: 0.9rem; }
th, td { text-align: left; padding: 0.5rem 0.6rem 0.5rem 0; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: 0.8rem; color: var(--ink-soft); font-weight: 600; }
.empty { color: var(--ink-soft); }

/* Who is signed in, and the way out, on a standalone page. */
.identity { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.5rem 1rem;
            margin-top: 3.5rem; padding-top: 1.25rem; border-top: 1px solid var(--line);
            color: var(--ink-soft); font-size: 0.9rem; }
.identity strong { color: var(--ink); font-weight: 500; overflow-wrap: anywhere; }
.identity form { display: inline; }
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
            # ``PAGE_HEADERS`` unconditionally, including on the paths that
            # serve a bundle: this document carries no script, so the strictest
            # policy is the correct one and a failing page must not be the
            # thing that hands out a script budget.
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


_ICON_LAYOUT_DASHBOARD = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect'
    ' width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/></svg>'
)
_ICON_MAIL = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="m22 7-8.991 5.727a2 2 0 0 1-2.009 0L2 7"/><rect x="2" y="4" width="20" height="16" '
    'rx="2"/></svg>'
)
_ICON_SHIELD = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 '
    '4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/></svg>'
)
_ICON_LOG_OUT = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="m16 17 5-5-5-5"/><path d="M21 12H9"/><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/></svg>'
)
"""The icons the navigation shares with the admin panel (lucide, the package the
panel draws from), inlined because these pages run no script. Decorative: every
entry is labelled in words beside it."""

_ICON_MOON = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M20.985 12.486a9 9 0 1 1-9.473-9.472c.405-.022.617.46.402.803a6 6 0 0 0 8.268 '
    '8.268c.344-.215.825-.004.803.401"/></svg>'
)
_ICON_SUN = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 '
    '1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 '
    '17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/></svg>'
)


class _NoRoles:
    """What the frame assumes about a viewer nobody described: no roles, no organization."""

    operator = False
    owner = False
    organization = None


_NO_ROLES = _NoRoles()

NEEDS_OPERATOR = "operator"
NEEDS_OWNER = "owner"

NAVIGATION: tuple[tuple[str, str, str, str | None], ...] = (
    (LANDING_PATH, "Overview", _ICON_LAYOUT_DASHBOARD, None),
    (ORG_INVITATIONS_PATH, "Invitations", _ICON_MAIL, NEEDS_OWNER),
    (ADMIN_PANEL_DOCUMENT, "Admin panel", _ICON_SHIELD, NEEDS_OPERATOR),
)
"""Where the signed-in frame can take you: app-relative path, label, icon, and
the role that opens it (``None`` for everyone).

The frame offers only the entries this person's roles open. The admin panel is
the hub administrators' tool, and an organization member shown a way into it
would be shown a refusal. The roles come from :func:`~.authz.viewer_roles`,
which answers "none" rather than failing when a source is down, so the landing
page still renders while an operator works out what is wrong; the pages
themselves keep their own gates.
"""

THEMES = ("light", "dark")
THEME_COOKIE = "collab-theme"
"""The chosen theme, shared with the admin panel, which reads and writes the
same cookie. Readable by script on purpose (the panel has to); it holds one of
two words."""


def preferred_theme(request: Request) -> str | None:
    """The theme this browser chose, or ``None`` to follow the system."""

    value = request.cookies.get(THEME_COOKIE)
    return value if value in THEMES else None


def set_theme_cookie(response: Response, theme: str) -> None:
    """Record a choice for a year, for every path of this origin."""

    response.set_cookie(THEME_COOKIE, theme, max_age=365 * 86400, path="/", secure=True, httponly=False, samesite="lax")


def offered(navigation, *, operator: bool, owner: bool):
    """The entries of *navigation* these roles open."""

    allowed = {None, NEEDS_OPERATOR if operator else "", NEEDS_OWNER if owner else ""}
    return [entry for entry in navigation if entry[3] in allowed]


def initials(name: str | None, email: str | None) -> str:
    """Two letters for the header circle, the way the panel picks them.

    First and last name, never the middle; failing a name, the start of the
    address. Never empty: an unlabelled blank reads as a rendering fault.
    """

    parts = (name or "").split()
    if parts:
        pair = parts[0][:1] + (parts[-1][:1] if len(parts) > 1 else "")
        if pair:
            return pair.upper()
    address = (email or "").strip()
    return address[:2].upper() if address else "?"


def _shell(
    *,
    body: str,
    root_path: str,
    identity_label: str,
    identity_email: str | None,
    csrf_token: str,
    current_path: str | None,
    roles: ViewerRoles,
    theme: str | None,
    account_url: str | None,
) -> str:
    """The signed-in frame: header, side navigation, main column.

    The header names the organization the person belongs to once it has a
    name, and the surface ("Operations") otherwise, so an owner always sees
    whose pages these are without the pages having to say so.
    """

    root = html.escape(root_path)
    entries = ""
    for path, label, icon, _needs in offered(NAVIGATION, operator=roles.operator, owner=roles.owner):
        current = ' current" aria-current="page' if path == current_path else ""
        entries += f'<li><a class="navlink{current}" href="{root}{path}">{icon}{html.escape(label)}</a></li>'

    # The switch offers the other theme. With no choice recorded the page
    # follows the system, which the server cannot see, so both switches are
    # rendered and the stylesheet shows the one that applies (see
    # `.theme-switch`). Once a choice is recorded there is one.
    def switch_to(other: str) -> str:
        icon = _ICON_SUN if other == "light" else _ICON_MOON
        return (
            f'<form class="inline theme-switch to-{other}" method="post" action="{root}{THEME_PATH}">'
            f'<input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">'
            f'<input type="hidden" name="theme" value="{other}">'
            f'<input type="hidden" name="next" value="{html.escape(current_path or LANDING_PATH)}">'
            f'<button type="submit" class="icon-button" title="Switch to {other} theme"'
            f' aria-label="Switch to {other} theme">{icon}</button></form>'
        )

    # Same-tab navigation, like every other link here; the page-level
    # no-referrer meta already keeps this origin out of the Referer.
    account = f'<a href="{html.escape(account_url)}">Account</a>' if account_url else ""
    if theme in THEMES:
        switch = switch_to("light" if theme == "dark" else "dark")
    else:
        switch = switch_to("dark") + switch_to("light")
    return (
        '<div class="app">'
        '<header class="header">'
        f'<div class="header-brand"><img class="wordmark" src="{root}{LOGO_PATH}" alt="OpenTeams Collab">'
        f'<span class="header-surface">{html.escape(roles.organization or "Operations")}</span></div>'
        '<div class="header-identity">'
        f'<span class="avatar" aria-hidden="true">{html.escape(initials(identity_label, identity_email))}</span>'
        f'<span class="who">{html.escape(identity_email or identity_label)}</span>'
        f"{account}{switch}"
        f'<form class="inline" method="post" action="{root}/web/signout">'
        f'<input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">'
        '<button type="submit" class="icon-button" title="Sign out" aria-label="Sign out">'
        f"{_ICON_LOG_OUT}</button></form>"
        "</div></header>"
        '<div class="shell">'
        f'<nav class="nav" aria-label="Sections"><ul>{entries}</ul></nav>'
        f'<main class="main">{body}</main>'
        "</div></div>"
    )


def render_page(
    *,
    title: str,
    body: str,
    root_path: str = "",
    identity_label: str | None = None,
    identity_email: str | None = None,
    csrf_token: str | None = None,
    current_path: str | None = None,
    roles: ViewerRoles | None = None,
    theme: str | None = None,
    account_url: str | None = None,
) -> str:
    """Render one page of the surface into a complete document.

    ``body`` is trusted page markup composed by a route in this codebase --
    every request- or store-derived value must already be escaped by the
    caller (:func:`escape` is the one to use). ``title``, ``identity_label``
    and ``identity_email`` are escaped here because they are routinely dynamic.

    When ``identity_label`` and ``csrf_token`` are given the page is a
    signed-in one and gets the admin panel's frame: the header names the
    person and holds the sign-out form (whose hidden field carries the CSRF
    token, the pattern every POST form on this surface follows), and the side
    navigation lists the destinations ``operator`` and ``owner`` open, with
    ``current_path`` marked. Otherwise the page stands alone in one centred
    column. ``theme`` is the browser's recorded choice, if any; without one the
    page follows the system.

    ``account_url`` is the deployment's external account page
    (``web.account_page_url``, issue #275). It is rendered only in that
    signed-in header and only when set; routes read it from configuration
    through :func:`~.surface.account_page_url`, never from the request. This
    is the one place the link exists, so a page cannot grow a second copy
    with a different rule.
    """

    if identity_label is not None and csrf_token is not None:
        content = _shell(
            body=body,
            root_path=root_path,
            identity_label=identity_label,
            identity_email=identity_email,
            csrf_token=csrf_token,
            current_path=current_path,
            roles=roles if roles is not None else _NO_ROLES,
            theme=theme,
            account_url=account_url,
        )
    else:
        content = (
            '<div class="standalone">'
            f'<div class="brand"><img src="{html.escape(root_path)}{LOGO_PATH}" alt="OpenTeams Collab"></div>'
            f"<main>{body}</main></div>"
        )
    chosen = f' data-theme="{theme}"' if theme in THEMES else ""
    return f"""<!DOCTYPE html>
<html lang="en"{chosen}>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta name="robots" content="noindex, nofollow">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="{html.escape(root_path)}{STYLE_PATH}">
</head>
<body>
{content}
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
        detail = "The form you sent is larger than this page will read, so nothing was changed."
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
