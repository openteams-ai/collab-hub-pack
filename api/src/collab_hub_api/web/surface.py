"""Configuration resolution and startup preconditions for the web surface.

The surface is enabled by configuring the confidential client's id
(``web.client_id``); everything else it needs must then be present and
coherent, and every way it can be present-but-broken is refused at startup.
Each of these misconfigurations has the same symptom otherwise — a person
completes a Keycloak sign-in and *then* hits a dead end — so they would all be
discovered by an operator's browser rather than by the rollout. Same
fail-fast contract as the identity and org-source settings (``make_app``).
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from fastapi import Request
from fastapi.routing import APIRoute

from ..config import WEB_SESSION_LIFETIME_CEILING_SECONDS, BaseConfig
from ..frames.org_source import (
    ORG_SOURCE_ENV,
    ORG_SOURCE_MEMBERSHIP,
    ORG_SOURCE_SINGLE,
    org_source_resolves_membership,
)
from ..path_protection import resolve_access
from .oidc import invalid_realm_url_reason, realm_endpoints
from .session import SessionCodec

BEARER_ISSUER_ENV = "FRAMES_BEARER_ISSUER"
"""Fallback source for the realm URL, read at startup: the web client lives in
the same realm the bearer verifier already trusts, so a deployment that names
that realm once should not have to name it twice."""

LANDING_PATH = "/web"
SIGNIN_PATH = "/web/signin"
CALLBACK_PATH = "/web/oidc/callback"
SIGNOUT_PATH = "/web/signout"
SIGNED_OUT_PATH = "/web/signed-out"

WEB_PATH_PREFIX = "/web"
"""Every route of the shared surface lives under this prefix. The pages built
on it (#90–#92) have their own paths (``/invite/accept``, ``/admin/*``,
``/org/*``) and register their own protection-map entries."""

STYLE_ASSET_PATH = f"{WEB_PATH_PREFIX}/app.css"

WEB_LOGO_PATH = f"{WEB_PATH_PREFIX}/collab-logo.png"
"""The product wordmark, served to the server-rendered pages.

Public for the same reason the stylesheet is: the sign-in page and the
invitation acceptance page are reachable before anybody has a session, and a
page whose chrome 401s renders as a broken image to the very people least able
to explain it. It carries no information -- it is the same file for everyone.
"""

WEB_SURFACE_PREFIXES = ("/web", "/admin", "/org", "/invite")
"""Every path prefix the browser surface owns, from the issue #88 route table.

``/admin`` and ``/org`` are listed before the pages that will live there exist
(#91, #92) on purpose: the guard that refuses an unprotected page has to be in
place *before* the pages arrive, or its first real test is the one it fails.
An unregistered prefix costs nothing.

``/invite`` (#90) is listed and its **page path is exempted in**
:data:`PUBLIC_WEB_PATHS` — not left off this tuple. The distinction matters
and an earlier draft of this docstring had it wrong. Being off the tuple does
not make a path anonymous in a reviewed way; it makes it *invisible* to the
guard **and** to :class:`~.pages.WebSecurityHeadersMiddleware`, so the
acceptance page would have shipped without ``Referrer-Policy: no-referrer``,
without ``no-store``, and without a CSP — on the one page in the system that
handles a live invitation secret. Listing the prefix and naming the single
public path is the arrangement where the exemption is one reviewed line and
everything else under ``/invite`` is still guarded: notably
``/invite/accept/redeem``, which requires a session.

Listing a prefix before its pages exist has one consequence worth writing
down rather than leaving for the next reader to re-derive. The protection map
must leave these prefixes public (see
:func:`enforce_web_surface_preconditions`), so while ``/admin`` and ``/org``
carry no routes, a request to one of them matches nothing in this app and
answers the router's own 404 once the session guard, which authenticates by
path before routing, has let it through. That is fine, and specifically it is
not an authentication hole: a 404 serves nothing. Map-public means "the *web*
surface's own session flow decides this path", never "unauthenticated".
"""

ALLOWED_WEB_MOUNTS: frozenset[str] = frozenset()
"""Mount paths exempt from the startup refusal. Deliberately empty.

A ``Mount`` hands routing to a sub-application whose routes cannot be
inspected from outside, so a mount under a guarded prefix is refused at
startup rather than waved through.

An entry here suppresses that refusal **only**. It is not an authentication
bypass: :class:`~.guard.WebSessionGuardMiddleware` authenticates by path, so
every request into a mounted child app still needs a valid session. What the
allowlist buys is permission to mount at all, which is a design decision worth
reviewing on its own.
"""

# The browser surface serves no WebSockets, and there is deliberately no flag
# to change that.
#
# It is server-rendered documents and forms with no JavaScript, so a socket
# under one of its prefixes has no legitimate use — and sockets sit outside
# the session model this surface is built on: the guard authenticates HTTP
# requests by path, and a socket handshake is not that. A boolean here would
# have been a footgun rather than a feature: flipping it would permit every
# socket on the surface with no session check anywhere, which is the opposite
# of what whoever flipped it would expect.
#
# Adding WebSocket support therefore means writing socket session enforcement
# and revisiting `route_offence` and the guard together — not editing a
# constant.

ACCEPT_PAGE_PATH = "/invite/accept"
"""The invitation-acceptance page (#90). Named here as well as in
:mod:`.acceptance` so that this module — which every path-based control reads
— does not have to import the page module to describe its own exemptions."""

ACCEPT_REDEEM_PATH = "/invite/accept/redeem"
"""The acceptance page's POST endpoint. **Not** public: it requires a
session, and the guard enforces that because ``/invite`` is a guarded prefix
and this path is absent from :data:`PUBLIC_WEB_PATHS`."""

ADMIN_INVITATIONS_PATH = "/admin/invitations"
"""The operator invitation page (#91): ``GET`` renders it, ``POST`` issues.

Defined here rather than in :mod:`.admin` — and imported from here by that
module — because every path-keyed control in this package reads *this* file,
and two spellings of one path is how a control silently stops covering the
page it names. (:data:`ACCEPT_PAGE_PATH` predates that reasoning and keeps its
second definition in :mod:`.acceptance`; new pages should not add one.)

Emphatically **not** in :data:`PUBLIC_WEB_PATHS`: it requires a session, which
the guard enforces because ``/admin`` is a guarded prefix, and operator
authority on top of that, which the router's dependency enforces.
"""

ADMIN_INVITATIONS_REVOKE_PATH = "/admin/invitations/revoke"
"""The operator page's revoke ``POST``. Same session and operator gating."""

ORG_INVITATIONS_PATH = "/web/org/invitations"
"""The owner invitation page (#142): ``GET`` renders it, ``POST`` issues.

Under ``/web``, **not** at the reserved top-level ``/org`` prefix, by the
routing decision recorded on openteams-ai/collab-hub-pack#142. The public
Collab Hub fronts browser paths with a gateway auth wall that exempts only the
prefixes named in ``nebariapp.routing.publicRoutes`` (``/web``, ``/invite``
and ``/admin`` were added by an internal issue after the
wall silently broke the operator page's POSTs), so a page at a fresh top-level
prefix ships straight into that wall: forms silently dead on a page that
renders one-time redemption links. Nesting under the already-exempt ``/web``
needs zero deployment coordination, here and on every future install — and
``/web`` is the authenticated-user surface, which is what an org owner is.

The rule this encodes for every future browser path: **nest under an
already-exempt prefix, or land the ``publicRoutes`` addition in the same
change** — never as a post-deploy discovery.

Requires a session (the guard enforces it: ``/web`` is a guarded prefix and
this path is not in :data:`PUBLIC_WEB_PATHS`) and org ownership on top of
that, which the router's dependency enforces.
"""

ORG_INVITATIONS_REVOKE_PATH = "/web/org/invitations/revoke"
"""The owner page's revoke ``POST``. Same session and owner gating."""

ORG_INVITATIONS_NAME_PATH = "/web/org/invitations/name"
"""The owner page's first-invite naming ``POST`` (#44, the flow #92 specified).

Every organization starts with the neutral placeholder name, and the owner
page refuses to issue an invitation while it stands — the invitee's email
would otherwise name "Unnamed organization". This route gives the organization
its name, once, recorded as ``org.rename``. Same session and owner gating; a
sub-path of the page rather than a page of its own, so it inherits the
``/web`` nesting decision recorded on :data:`ORG_INVITATIONS_PATH`.
"""

DATA_STATEMENT_PATH = "/web/data-statement"
"""The data statement page (#146): what is stored, who can see it, how to ask
for deletion. The copy itself lives in :mod:`.data_statement`.

Nested under the already-exempt ``/web`` prefix, per the rule recorded on
:data:`ORG_INVITATIONS_PATH` — no gateway ``publicRoutes`` coordination.

In :data:`PUBLIC_WEB_PATHS` **by design**: its audience includes people
deciding whether to accept an invitation, who by definition have no account
here yet — requiring a session would mean committing to an account before
being allowed to read what the deployment stores about you, which inverts the
point of the page. It is anonymous *safely* for the same reasons the
acceptance page is: a static document, rendered from constants, reading
nothing from the request, with nothing to act on.
"""

TERMS_PATH = "/web/terms"
"""The Terms of Service (#95). The copy lives in :mod:`.terms_of_service`.

In :data:`PUBLIC_WEB_PATHS` for the same reason as
:data:`DATA_STATEMENT_PATH`. In addition, the deployment's Keycloak
terms-acceptance step links here, so the audience is people who have not
accepted yet. Keycloak will not issue them a token until they do, and this
surface's session is minted from that token. Requiring a session here
would mean the document could only be read by someone who had already agreed
to it. Anonymous *safely* on the same grounds as the data statement: a static
document rendered from module constants, reading nothing from the request,
with nothing to act on.
"""

PRIVACY_PATH = "/web/privacy"
"""The Privacy Statement (#95). The copy lives in :mod:`.privacy_statement`.

Public on exactly the argument recorded on :data:`TERMS_PATH`; the two
documents are linked from the same consent line and are reached by the same
people at the same moment.
"""

ORG_INVITATIONS_PATHS = (
    ORG_INVITATIONS_PATH,
    ORG_INVITATIONS_REVOKE_PATH,
    ORG_INVITATIONS_NAME_PATH,
)
"""Every path the owner invitation page serves. Read by
:data:`CSRF_ENFORCED_IN_ROUTE`, same as :data:`ADMIN_PATHS`."""

ADMIN_PANEL_DOCUMENT = "/admin/"
"""The document the single-page panel boots from.

With the trailing slash, which is the canonical form: the bundle's asset and
API URLs are document-relative, so ``/admin`` (which redirects here) would
resolve them against ``/``. See :mod:`..routers.admin_ui`.
"""

ADMIN_PANEL_REDIRECT = "/admin"
"""The slashless form, which redirects to the canonical one."""

ADMIN_PANEL_ASSETS_PREFIX = "/admin/assets/"
"""Where the panel's hashed bundle files live.

A prefix rather than an enumerated tuple because the names carry content
hashes and change on every build, so nothing in this repository can list them.

Deliberately **not** all of ``/admin``. The panel's policy forbids
``form-action`` outright, which is correct for a surface that submits no HTML
forms and wrong for :data:`ADMIN_INVITATIONS_PATH`, which submits two. Scoping
the policy to the two things the panel actually serves keeps that page's forms
working and keeps each path under the strictest policy it can bear.
"""

ADMIN_API_PREFIX = "/admin/api"
"""Where the admin panel's JSON lives, and the one part of this surface whose
refusals are JSON rather than pages.

Under ``/admin`` rather than ``/v1`` on purpose. The rule this surface keeps is
not "JSON belongs to the API"; it is that browser credentials do not work on
the machine API and machine credentials do not sign a browser in. A panel
running in a browser is a browser client, so it authenticates the way every
other browser client here does -- which is also what puts it behind
:class:`~.guard.WebSessionGuardMiddleware`, a control that authenticates by
path *before* routing and therefore cannot be defeated by a route that forgets
a dependency. ``/v1`` has no such middleware; it relies on each route carrying
its own, which is the arrangement that module was rewritten to stop depending
on.

A prefix rather than an enumerated tuple, unlike :data:`ADMIN_PATHS`, because
the refusal shape has to be decided for paths that do not exist: a JSON client
asking for a mistyped admin endpoint must be told so in JSON, not handed a
sign-in redirect it will follow into an HTML page with status 200.
"""

ADMIN_PATHS = (ADMIN_INVITATIONS_PATH, ADMIN_INVITATIONS_REVOKE_PATH)
"""Every ``/admin`` path this surface serves.

Read by :data:`CSRF_ENFORCED_IN_ROUTE`, so that registry names these paths once
rather than spelling them a second time. It is deliberately **not** consulted by
:func:`enforce_web_surface_preconditions`: these routes mount only in membership
mode, and the map coverage they need is derived from the route table by
:func:`enforce_web_surface_map_access`, which cannot demand a prefix for pages a
deployment does not serve.
"""

PUBLIC_WEB_PATHS = frozenset(
    {
        SIGNIN_PATH,
        CALLBACK_PATH,
        SIGNED_OUT_PATH,
        STYLE_ASSET_PATH,
        WEB_LOGO_PATH,
        ACCEPT_PAGE_PATH,
        DATA_STATEMENT_PATH,
        TERMS_PATH,
        PRIVACY_PATH,
    }
)
"""The **only** routes of this surface that may be served without a session.

Each earns it: sign-in and the callback are how a session comes into
existence, the signed-out confirmation is shown to a browser that no longer
has one, and the stylesheet is a startup constant containing nothing. Every
other route under a guarded prefix — including every route a later issue
adds — inherits the router's session dependency and is authenticated by
default. Enforced twice on purpose: by the router default, and by a test that
walks the app's registered routes so a future router that bypasses the
default is caught rather than trusted.

:data:`ACCEPT_PAGE_PATH` is the one that needs arguing for. It is anonymous
**by design**: its whole audience is people who do not have an account on
this deployment yet, and requiring a session to see it would bounce every
invitee to a sign-in page with no explanation of what they are signing in
for. It is also anonymous **safely**, because the page is a static document —
it holds no invitation state, reads nothing from the request, and the token
it exists to handle never reaches the server on this request at all (it is in
the URL fragment). Everything that *acts* on an invitation is
:data:`ACCEPT_REDEEM_PATH`, which is not in this set.

:data:`DATA_STATEMENT_PATH` earns anonymity by the same two-part argument —
its audience is deciding whether to create an account at all, and the page is
a constant document with nothing to act on. The full reasoning is on the
constant.

:data:`TERMS_PATH` and :data:`PRIVACY_PATH` (#95) earn it a degree more
plainly than either. They are linked from the Keycloak terms-acceptance step,
which runs *before* Keycloak issues a token — and this surface's session is
minted from that token. Gating them on a session would therefore mean the
documents could only be read by someone who had already accepted them, which
is not a stricter policy but an incoherent one.
"""

CSRF_ENFORCED_IN_ROUTE: frozenset[str] = frozenset(
    {
        ACCEPT_REDEEM_PATH,
        # #91's operator invitation page. These were string literals when #123
        # shipped the registry ahead of the routes, with a note to switch them
        # the moment the constants existed; #91 defines them above, so they are
        # switched here. That is the whole point of the note: a path spelled
        # twice is a path that can drift, and the literals were a bridge across
        # two changes rather than a preference.
        *ADMIN_PATHS,
        # #142's owner invitation page: the same shape as #91's entries, for
        # the same reason — its POST handlers answer a CSRF refusal by
        # re-rendering their own page, through the same predicate, which a
        # dependency walk cannot see.
        *ORG_INVITATIONS_PATHS,
    }
)
"""Paths whose state-changing methods check CSRF **inside the handler**.

Every ``POST`` *this* issue ships (``/web/signout``) carries
``Depends(require_csrf)``, which is the arrangement
:func:`~.authz.route_offence` can see and therefore the one to prefer. The set
exists because "prefer" is not "require", and two of the pages built on this
surface have a real reason not to use the dependency.

``/invite/accept/redeem`` (#90) is listed **here rather than in #90**, and the
ordering is the reason: #90 is approved and on the release path, this change
is not, so the two must not each carry half of a constraint that only holds
when both are present. Merging them as written — an empty set here against a
route there that omits the dependency — makes the API refuse to start. Landing
#90 first and carrying the entry here puts the whole reconciliation on the
change that introduced the check.

That route calls ``require_csrf`` by hand, and the reason has changed once.
Originally the ordering was the size defense: the dependency's form fallback
was ``request.form()``, an unbounded read, and the route's content-type gate
had to provably run first to make it unreachable — relying on FastAPI's
dependency-solving order for that would be trusting the arrangement of a
structure rather than the code that runs. #119 bounded the fallback inside
``require_csrf`` itself (it reads through ``web.forms`` under
``MAX_FORM_BYTES``), so that constraint has relaxed: a future route may take
the dependency without arranging anything first. What keeps the redeem
route's call in-route now is its contract, not its safety — it answers a CSRF
refusal in JSON by catching ``WebForbidden``, where the declared dependency
would answer with the surface's HTML 403 page that its script cannot parse.

#91's ``/admin/invitations`` and ``/admin/invitations/revoke`` are here too,
and they are the entries that changed their mind. Both are ``@router.post``
handlers calling ``_csrf_ok()``, which runs
:func:`~.session.csrf_token_matches` — the same constant-time comparison
against the same secret — as a **predicate**, because they answer a refusal by
re-rendering their own page rather than the surface's fixed 403. A dependency
walk cannot see that any more than it can see the redeem route's call.

The earlier reading left them to #91, on the rule that an exemption for a
route nobody can point at is a claim that cannot be checked. That rule is
sound and loses to a better one here: getting this count wrong costs a
**second broken build** rather than a wrong answer, because the check and the
routes land in different changes. Carrying all three removes the coordination
dependency on #91 remembering, and it is the same argument that put ``/admin``
in the chart's default protection map. The entries are inert until those
routes mount — :func:`~.authz.stale_csrf_exemptions` judges only paths that
are actually mounted, precisely so an entry may lead its route.

Both patterns are defensible; neither is visible to a dependency-tree walk,
and none can be made visible — an endpoint that calls the comparison is
indistinguishable from one that does not without reading its body, and reading
a body for a *name* is the label-not-structure mistake this module exists to
avoid. So the choice is between a check that refuses correct code and a check
with a reviewed exemption list, and this is the second one. An entry here is a
claim that the route enforces CSRF by other means, exactly as reviewable as
:data:`PUBLIC_WEB_PATHS` — one line, in a diff, with a name attached.
"""


def clamped_session_lifetime(seconds: int) -> int:
    """Bound a session lifetime by the ceiling. **The mint path calls this.**

    A module-level function taking the raw number, rather than a method on
    ``WebSurface``: an instance method is an instance attribute, and an
    instance attribute can be shadowed. The mint path reads the validated
    field and clamps it here, so the only way to widen the window is to edit
    this function.
    """

    return max(1, min(seconds, WEB_SESSION_LIFETIME_CEILING_SECONDS))


@dataclass(frozen=True, slots=True)
class WebSurface:
    """Everything the routes need, resolved once at startup.

    The session-lifetime ceiling is enforced **here**, not only in
    ``WebConfig``: a validation constraint binds the parse path, and this
    object is what the routes actually mint cookies from. Constructing one
    directly — a test, a future caller, anything that does not go through
    ``Config.parse`` — bypassed the ceiling entirely and produced seven-day
    ``exp`` and ``Max-Age`` values. An invariant belongs where the value is
    used.

    ``slots=True`` for the same reason one step further out: without it,
    ``object.__setattr__(surface, "session_max_age", lambda: 604800)`` shadows
    the method on the instance and the mint path calls the impostor. Slots
    make an undeclared attribute unassignable, so the accessor cannot be
    replaced — and the mint path does not call through it anyway (see
    :func:`clamped_session_lifetime`).
    """

    client_id: str
    client_secret: str
    scope: str
    issuer_url: str
    authorize_endpoint: str
    token_endpoint: str
    jwks_url: str
    codec: SessionCodec
    session_lifetime_seconds: int
    public_base_url: str

    def __post_init__(self) -> None:
        if self.session_lifetime_seconds <= 0:
            raise ValueError("web session lifetime must be positive")
        if self.session_lifetime_seconds > WEB_SESSION_LIFETIME_CEILING_SECONDS:
            raise ValueError(
                f"web session lifetime {self.session_lifetime_seconds}s exceeds the"
                f" {WEB_SESSION_LIFETIME_CEILING_SECONDS}s ceiling: the session cookie is"
                " stateless and cannot be revoked before it expires, so this bound is the"
                " documented exposure window and may be lowered, never raised"
            )

    def session_max_age(self) -> int:
        """Convenience view of :func:`clamped_session_lifetime` for this surface.

        Mint sites deliberately do **not** call this — they call the module
        function on the field directly, so the bound cannot be reached through
        anything overridable. This exists for callers that want the number.
        """

        return clamped_session_lifetime(self.session_lifetime_seconds)

    def redirect_uri(self, request: Request) -> str:
        """The callback URL this deployment registered with Keycloak.

        ``web.public_base_url`` is authoritative when set — behind a proxy
        whose forwarded headers are not trusted, a request-derived base URL
        comes out ``http://`` and Keycloak refuses the mismatch. Otherwise the
        request's own base URL (which already includes any root path) is used;
        a forged Host header here cannot redirect the flow anywhere Keycloak
        has not been told about, because Keycloak validates ``redirect_uri``
        against the client's registered list.
        """

        base = self.public_base_url or str(request.base_url)
        return f"{base.rstrip('/')}{CALLBACK_PATH}"

    def now(self) -> int:
        return int(time.time())


def build_web_surface(config: BaseConfig) -> WebSurface:
    """Construct the surface from validated configuration.

    Call :func:`enforce_web_surface_preconditions` first; this function
    assumes the configuration already passed it.
    """

    web = config.web
    issuer = _resolve_issuer_url(web.issuer_url)
    authorize_endpoint, token_endpoint, jwks_url = realm_endpoints(issuer)
    return WebSurface(
        client_id=web.client_id.strip(),
        client_secret=web.client_secret,
        scope=web.scope,
        issuer_url=issuer.rstrip("/"),
        authorize_endpoint=authorize_endpoint,
        token_endpoint=token_endpoint,
        jwks_url=jwks_url,
        codec=SessionCodec(web.session_secret),
        session_lifetime_seconds=web.session_lifetime_seconds,
        public_base_url=web.public_base_url.strip(),
    )


def _resolve_issuer_url(configured: str) -> str:
    return (configured or os.environ.get(BEARER_ISSUER_ENV, "")).strip()


def enforce_web_surface_preconditions(config: BaseConfig) -> None:
    """Fail the rollout on a web-surface configuration that cannot work.

    The shape rules (secret present, session secret length, ``openid`` scope)
    live on the pydantic model so they hold however the config was built; the
    checks here are the ones a model validator cannot make — environment
    fallbacks and cross-section coherence with the protection map.
    """

    web = config.web
    if not web.enabled:
        return

    issuer = _resolve_issuer_url(web.issuer_url)
    if not issuer:
        raise RuntimeError(
            "web.client_id requires an OIDC realm URL: set COLLAB_HUB_API__WEB__ISSUER_URL"
            f" or the bearer issuer ({BEARER_ISSUER_ENV}). Without it the web surface cannot"
            " start a sign-in."
        )
    reason = invalid_realm_url_reason(issuer)
    if reason:
        raise RuntimeError(
            f"The web surface's OIDC realm URL is unusable ({reason}), got {issuer!r}:"
            " set COLLAB_HUB_API__WEB__ISSUER_URL to the plain Keycloak realm URL,"
            " e.g. https://auth.example.com/realms/nebari."
        )
    # One realm, not two. The web client is required to live in the realm the
    # bearer verifier already enforces: identities are compared across the two
    # surfaces (ACLs, roles, audit rows key on the same subject), and two
    # issuers make a cross-issuer `sub` collision an account-takeover path —
    # the hardening note on issue #83.
    bearer_issuer = os.environ.get(BEARER_ISSUER_ENV, "").strip()
    if bearer_issuer and issuer.rstrip("/") != bearer_issuer.rstrip("/"):
        raise RuntimeError(
            "web.issuer_url must name the same realm as the bearer issuer"
            f" ({BEARER_ISSUER_ENV}), got {issuer!r} and {bearer_issuer!r}: the web"
            " session and the API would otherwise trust identities from two different"
            " realms under one subject namespace."
        )
    if web.public_base_url:
        base_reason = invalid_realm_url_reason(web.public_base_url)
        if base_reason:
            raise RuntimeError(
                f"web.public_base_url is unusable ({base_reason}), got"
                f" {web.public_base_url!r}: set it to this deployment's external"
                " origin, e.g. https://frames.example.com."
            )
    elif org_source_resolves_membership():
        # The invitation pages (#91, #142) mount on a membership-resolving
        # deployment, and the surface builds absolute URLs — the OIDC
        # `redirect_uri` among them — for every operator and owner who signs
        # in. Deriving those from the request means taking them from a `Host`
        # the caller chooses: behind a proxy that does not forward its scheme
        # the result is `http://`, which Keycloak refuses, and a forged header
        # is an origin nobody configured. So the origin is **configuration**,
        # and no syntactic check can substitute — every attacker-chosen host is
        # a syntactically valid one.
        #
        # This requirement was introduced for the rendered redemption link
        # (#91's dated amendment, since lapsed: the display and its module are
        # deleted). It stands on its own after that, and is deliberately not
        # relaxed along with the display — a deployment that reaches an
        # operator page has an external origin, and saying so once at boot is
        # cheaper than every URL the surface builds guessing.
        #
        # Refused at startup rather than at first use: a deployment discovering
        # this when an operator signs in has already been running.
        raise RuntimeError(
            "web.public_base_url is required when the browser surface is enabled on a"
            f" membership-resolving deployment ({ORG_SOURCE_ENV}={ORG_SOURCE_MEMBERSHIP}"
            f" or {ORG_SOURCE_SINGLE}):"
            " the surface builds absolute URLs (the OIDC redirect_uri among them) and must"
            " not take their origin from a request's Host header. Set"
            " COLLAB_HUB_API__WEB__PUBLIC_BASE_URL to this deployment's external origin,"
            " e.g. https://collab.example.com."
        )

    # The protection map authenticates with the *API* credential check
    # (IdToken cookie / bearer) before routing, which a browser mid-sign-in
    # cannot pass — the web routes authenticate themselves with the session
    # cookie instead. A map that does not leave the /web prefix public would
    # therefore 401 the sign-in flow itself, on every request, for every
    # operator. Refuse the rollout with the exact entry to add.
    #
    # This list is a *floor*, not the whole check, and it runs here because
    # config is all that exists yet. The complete check is
    # `enforce_web_surface_map_access`, which make_app runs against the route
    # table once every router is mounted: that one cannot be forgotten by a
    # page author, because it derives the paths from the routes themselves
    # rather than from anyone remembering to extend a tuple. What this list
    # buys is the earlier, clearer failure — before any store is opened — for
    # the flow paths whose absence breaks sign-in for everybody.
    for path in (
        LANDING_PATH,
        SIGNIN_PATH,
        CALLBACK_PATH,
        SIGNOUT_PATH,
        SIGNED_OUT_PATH,
        STYLE_ASSET_PATH,
        WEB_LOGO_PATH,
        # The acceptance page and its redemption endpoint (#90). Both must be
        # map-public for the same reason the rest of the surface is: the map's
        # `authenticated` level runs the API credential check, and an invitee
        # holds a browser session cookie, not a bearer token. The redemption
        # endpoint is *not* thereby unauthenticated — the session guard and its
        # own dependencies enforce a session and CSRF on it.
        ACCEPT_PAGE_PATH,
        ACCEPT_REDEEM_PATH,
        # #91's `/admin` paths are deliberately **not** here, and this is the
        # floor's own rule rather than an exception to it. They are mounted
        # only when `org_source_resolves_membership()`, so demanding them
        # unconditionally would refuse every claims-mode rollout over paths
        # that deployment does not serve — and the route-derived check names
        # them precisely, and only, where they exist.
    ):
        if resolve_access(path, config.security.paths, config.security.default_access) != "public":
            prefix = "/" + path.lstrip("/").split("/", 1)[0]
            raise RuntimeError(
                f"The per-path protection map does not leave {path} public, so the web"
                " surface could never be reached (sign-in for the operator pages,"
                " acceptance for an invitee). Add"
                f" {{path: {prefix}, match: prefix, access: public}} to security.paths —"
                " the web routes enforce their own session, CSRF and role checks."
            )


def blocked_web_route_paths(
    routes: Iterable[object], config: BaseConfig, *, prefixes: Sequence[str] | None = None
) -> list[str]:
    """Surface routes the protection map would refuse before routing, sorted."""

    from .authz import on_web_surface

    if prefixes is None:
        prefixes = WEB_SURFACE_PREFIXES
    blocked = set()
    for route in routes:
        path = getattr(route, "path", None)
        if not isinstance(path, str) or not isinstance(route, APIRoute):
            continue
        if not on_web_surface(path, prefixes):
            continue
        if resolve_access(path, config.security.paths, config.security.default_access) != "public":
            blocked.add(path)
    return sorted(blocked)


def enforce_web_surface_map_access(
    routes: Iterable[object], config: BaseConfig, *, prefixes: Sequence[str] | None = None
) -> None:
    """Refuse to serve a surface route the protection map would 401.

    The check :func:`enforce_web_surface_preconditions` cannot make, because
    at config-parse time no routes exist yet. It runs from ``make_app`` once
    every router is mounted, and again from the lifespan for anything
    registered after that — the same two moments, and the same reasoning, as
    :func:`~.authz.verify_web_route_protection`.

    The failure it removes is a genuinely confusing one, and it is a *pair* of
    controls disagreeing rather than either being wrong. ``PathProtectionMiddleware``
    is added first in ``make_app`` and therefore runs **innermost**, after
    :class:`~.guard.WebSessionGuardMiddleware`. So on a hardened map a page at
    an unlisted prefix passes the web-session guard — the person is signed in,
    the guard is satisfied — and is then refused by the API credential check,
    which a browser holding a web session cookie cannot possibly satisfy. Two
    correct components, one unreachable page, and nothing in either component's
    logs saying the map was the problem.

    Deriving the paths from the registered routes is the point. The tuple in
    :func:`enforce_web_surface_preconditions` covered the six ``/web`` paths
    while :data:`WEB_SURFACE_PREFIXES` already promised three prefixes, so the
    documented map and the enforced one could drift for ``/admin`` and
    ``/org`` — the one thing that check exists to prevent. A route table
    cannot drift from itself.

    It also means a prefix with **no** routes requires nothing of the map,
    which is the right answer and not merely the convenient one: demanding
    that a deployment open ``/org`` before anything serves ``/org`` would be
    asking operators to widen a map for paths that do not exist.
    """

    blocked = blocked_web_route_paths(routes, config, prefixes=prefixes)
    if not blocked:
        return
    prefix = "/" + blocked[0].lstrip("/").split("/", 1)[0]
    raise RuntimeError(
        "The per-path protection map does not leave these browser-surface routes"
        f" public: {', '.join(blocked)}. The map's `authenticated` level runs the API"
        " credential check before routing, which a browser holding a web session"
        " cookie cannot pass, so these pages would be 401'd after passing the session"
        f" guard. Add {{path: {prefix}, match: prefix, access: public}} to"
        " security.paths — the web routes enforce their own session, CSRF and role"
        " checks in-route."
    )


def on_admin_panel(path: str) -> bool:
    """Whether *path* is the panel's document or one of its bundle files."""

    return (
        path in (ADMIN_PANEL_DOCUMENT, ADMIN_PANEL_REDIRECT)
        or path.startswith(ADMIN_PANEL_ASSETS_PREFIX)
    )


def answers_json(path: str) -> bool:
    """Whether a refusal on *path* should be JSON rather than a page.

    True for the admin API and nothing else. The comparison is prefix-based and
    segment-aware -- ``/admin/apiary`` is a page path, not an API one -- so a
    future page whose name begins with the same letters does not silently start
    refusing in JSON.
    """

    return path == ADMIN_API_PREFIX or path.startswith(f"{ADMIN_API_PREFIX}/")
