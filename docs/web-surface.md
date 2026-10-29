# The browser web surface

The server-side web interface for humans: operators and organization owners
drive privileged actions from server-rendered pages, not raw SQL and not the
desktop app (implementation plan Gate E). This document covers the **shared
surface** — the OIDC browser session, cookies, CSRF posture, page scaffolding,
and authorization helpers ([#88]) — and the pages built on it: the
invitation acceptance page ([#90], `/invite/accept`), the operator
invitation page ([#91], `/admin/invitations`), the owner invitation page
([#142], `/web/org/invitations`), the data statement ([#146],
`/web/data-statement`), and the canonical Terms of Service and Privacy
Statement ([#95], `/web/terms` and `/web/privacy`).

[#88]: https://github.com/openteams-ai/collab-hub-pack/issues/88
[#90]: https://github.com/openteams-ai/collab-hub-pack/issues/90
[#91]: https://github.com/openteams-ai/collab-hub-pack/issues/91
[#142]: https://github.com/openteams-ai/collab-hub-pack/issues/142
[#44]: https://github.com/openteams-ai/collab-hub-pack/issues/188
[#146]: https://github.com/openteams-ai/collab-hub-pack/issues/146
[#95]: https://github.com/openteams-ai/collab-hub-pack/issues/95

## Two auth axes, deliberately

The API authenticates machines: bearer tokens from the desktop's public
`apollo-desktop` client, or the Nebari gateway's `IdToken-*` cookie. The web
surface authenticates people in browsers, and it does **not** reuse that path:

- A browser must never be asked to hold a bearer token, and a server-rendered
  page must not depend on a gateway that a standalone deployment does not have.
- The web session cookie is not API credentials: presenting it to `/v1/*`
  answers 401, and presenting an `IdToken-*` cookie or bearer token to a web
  page does not sign the browser in. The two axes never substitute for each
  other, in either direction.

The surface runs the OIDC **authorization-code flow with a confidential
client**, exchanges the code server-side (client secret + PKCE), verifies the
ID token, and then issues its own session cookie. Tokens from the IdP are used
once, during sign-in, and are never stored or sent to the browser.

## The Keycloak client

A second client in the **same** realm the bearer verifier trusts
(`keycloak.<hub-host>`, realm `nebari`):

| Setting | Value |
|---|---|
| Client ID | `collab-web` (any name; it becomes `web.client_id`) |
| Client authentication | **On** (confidential — this is the point) |
| Flow | Standard flow (authorization code) only |
| Valid redirect URI | `https://<host>/web/oidc/callback` — exact, no wildcard |

Creating the client is cluster configuration, not a change to this repo
(tracked in the deployed-runtime lane). The app refuses at startup to trust a
web realm different from the bearer realm: one subject namespace, one issuer.

## Configuration

Everything under `COLLAB_HUB_API__WEB__*`; setting `CLIENT_ID` enables the
surface, and every present-but-broken combination fails the rollout rather
than dead-ending a signed-in operator.

| Env | Meaning |
|---|---|
| `COLLAB_HUB_API__WEB__CLIENT_ID` | The confidential client's id. Empty (default) = surface off, no routes mounted. |
| `COLLAB_HUB_API__WEB__CLIENT_SECRET` | The client's secret. Required when enabled. |
| `COLLAB_HUB_API__WEB__ISSUER_URL` | The plain realm URL (`https://auth.example.com/realms/nebari`). Falls back to `FRAMES_BEARER_ISSUER`, and must equal it when both are set. **Must be `https://`** unless the host is loopback (`localhost`, `127.0.0.1`, `[::1]`) — the client secret is POSTed to this realm's token endpoint and its JWKS verifies every ID token, so plain http off-loopback is refused at startup. |
| `COLLAB_HUB_API__WEB__SESSION_SECRET` | Signs every session cookie. ≥ 32 characters and ≥ 16 distinct characters (so `"a" * 32` and 32 spaces are refused), identical on every replica. Generate with `python -c 'import secrets; print(secrets.token_urlsafe(32))'`. |
| `COLLAB_HUB_API__WEB__SESSION_LIFETIME_SECONDS` | Absolute session lifetime. Default **and hard ceiling** 8 hours; may be lowered, never raised (the stateless-session risk argument rests on this number). Enforced on `WebSurface` (which is slotted, so the accessor cannot be shadowed on an instance) and clamped at mint time through a module-level function on the validated field, not only in config validation. No sliding renewal. |
| `COLLAB_HUB_API__WEB__SCOPE` | Default `openid email profile`; must include `openid`. |
| `COLLAB_HUB_API__WEB__PUBLIC_BASE_URL` | External origin the surface builds its absolute URLs from, the OIDC redirect URI among them. **Required** when the surface is enabled on a membership-resolving deployment — startup refuses without it, because those origins must never be derived from a forgeable request `Host` (see below). Also what makes the redirect URI correct behind a proxy whose forwarded headers are not trusted. Same https/loopback rule as the issuer. |
| `COLLAB_HUB_API__WEB__ADMIN_UI_DIST` | Directory holding the built admin panel. The image sets it to `/var/collab-hub-api/admin-ui-dist`. Empty, or a directory with no `index.html`, mounts no panel routes (the second case logs `admin_ui_dist_missing_index`). The panel and its `/admin/api/*` endpoints mount only on a membership-resolving deployment (`orgSource` `membership` or `single`). |
| `COLLAB_HUB_API__WEB__ADMIN_GROUP` | Identity-provider group whose members hold the operator role, reconciled at each sign-in from the ID token's `groups` claim. Matches with or without a leading slash. Empty (default) turns sync off. Needs a groups mapper on the client so the ID token carries the claim; see [frames-operations.md](frames-operations.md#where-operator-rows-come-from-source-and-sign-in-sync). |

The realm's JWKS endpoint is derived from the issuer URL — there is no
separate JWKS setting to point somewhere else.

### Deploying it

There is no `web.*` value in the chart yet, so a deployment supplies the
settings through `api.deployment.extraEnv`, which is rendered verbatim and
therefore accepts `valueFrom`. The client secret must arrive that way — never
as a chart value, where it would land in a rendered manifest:

```yaml
api:
  deployment:
    extraEnv:
      - name: COLLAB_HUB_API__WEB__CLIENT_ID
        value: collab-web
      - name: COLLAB_HUB_API__WEB__CLIENT_SECRET
        valueFrom:
          secretKeyRef:
            name: collab-web-oidc-client
            key: client-secret
      - name: COLLAB_HUB_API__WEB__SESSION_SECRET
        valueFrom:
          secretKeyRef:
            name: nebari-nexus-web-session
            key: session-secret
      - name: COLLAB_HUB_API__WEB__PUBLIC_BASE_URL
        value: https://frames.example.com
```

`WEB__ISSUER_URL` is deliberately omitted: it falls back to
`frames.auth.bearer.issuer`, and the two must name the same realm anyway, so
naming it once is both less to configure and less to get wrong.

The redirect URI registered on the Keycloak client must be exactly
`<public_base_url>/web/oidc/callback`. Keycloak compares it verbatim, so a
client registered against any other path fails at the *first* sign-in with
`Invalid parameter: redirect_uri` — before the surface's own code runs.

### The identity pin, and why operator pages depend on it

One more setting has to line up with the deployment, and it is not a `web.*`
setting at all — which is exactly why it is easy to miss.

| | Resolves to | Set by |
|---|---|---|
| The **session principal** | `user_from_claims(claims)` | `FRAMES_AUTH_IDENTITY_CLAIM` |
| — pinned (`=sub`) | the verified `sub`, or no identity at all | opt-in |
| — legacy (unset, the **default**) | first of `preferred_username`, `email`, `sub` | — |
| The **operator row** | `collab_platform_roles.user_id` | Gate E: the OIDC `sub`, hand-inserted at bootstrap |

The web session takes its principal from the same `user_from_claims` the API
does, so the two surfaces agree with each other. What they need not agree with
is the *bootstrap procedure*: on a legacy deployment the principal is
`preferred_username`, while the row an operator inserts per Gate E is keyed on
`sub`. They do not match, `require_operator` refuses, and — because it refuses
correctly, fail-closed — it renders as the ordinary "you do not hold this
role" page. That is close to the hardest misconfiguration in this surface to
diagnose from a browser: nothing is broken, nothing is logged as an error, and
the page is telling the truth.

The principled version of the same point: on a legacy deployment operator
authority binds to a **renameable** string, which is the [#83] identity-skew
class this surface is otherwise careful about.

**Set `FRAMES_AUTH_IDENTITY_CLAIM=sub`** on any deployment that will use the
operator pages, and key the bootstrap row on the `sub`. If the pin must stay
off, key the row on whatever that deployment's precedence actually resolves —
accepting that the value is renameable.

This is **not** a startup refusal, deliberately. The pin governs every ACL
principal in the Frames API, not just this surface, and `frames.identity`
documents that leaving it unset keeps an existing deployment unaffected until
it opts in; making the browser surface demand it would force that migration as
a side effect of enabling a web page. It would also take down sign-in, the
acceptance page and the owner pages — none of which depend on it — over a
condition that grants nobody anything and that the request path already fails
closed on. Instead, enabling the surface without the pin logs
`web_identity_not_pinned_to_sub` at warning level and records
`app.state.web_identity_pinned_to_sub`, the same "loud signal, enforcement
elsewhere" shape as `web_platform_role_source_missing`.

## Session and CSRF posture

- **Cookie:** `__Host-nexus_web_session` — `HttpOnly`, `Secure`, `SameSite=Lax`,
  `Path=/`. The `__Host-` prefix makes the browser refuse it from any other
  host or an insecure origin. There is no insecure-cookie switch; browsers
  treat `localhost` as a secure context, so local development works as is.
- **Contents:** a signed (HMAC-SHA256) assertion of identity only — subject,
  display name/email, issue/expiry times, and the session's CSRF secret.
  **No roles.** Authorization is resolved from the server's own stores on
  every request, so revoking a role locks its holder out immediately, however
  long their cookie has left.
- **Sign-out** deletes the browser's cookie. Because the cookie is stateless,
  a copy captured *before* sign-out remains cryptographically valid until its
  `exp` — that copy asserts identity only (see above), and the short absolute
  lifetime bounds it. If per-session server-side revocation becomes a
  requirement, that is a session store, and a deliberate follow-up.
- **CSRF:** every POST on the surface requires the session's CSRF token, as
  the `csrf_token` form field (the layout renders it into every form) or the
  `X-CSRF-Token` header, compared in constant time. The token lives inside the
  HttpOnly cookie payload, so page script cannot read it; `SameSite=Lax`
  independently keeps cross-site POSTs from carrying the cookie at all.
  When the header is absent, `require_csrf` reads the form under the shared
  byte cap (`web.forms`, 4 KiB, counted rather than trusted from
  `Content-Length`) and accepts only `application/x-www-form-urlencoded` —
  an oversize body is refused with a 413 and `Connection: close` without
  being buffered, and `multipart/form-data` with a 415, both distinct from
  the 403 a wrong token earns ([#119]).
- **Sign-in flow integrity:** state, nonce, and the PKCE verifier ride a
  short-lived signed transient cookie (`__Host-nexus_web_oidc`, 10 minutes),
  bound to the browser that started the flow. The callback validates state,
  verifies the ID token — signature, issuer, **audience = this client's id**,
  `azp` (required whenever `aud` names more than one audience, per OIDC Core
  3.1.3.7, and never contradicting when present), `typ: ID`, nonce — and
  clears the transient cookie, so a callback URL replay finds no flow to
  finish. A validly signed same-realm token minted for
  another client (a desktop token) never mints a web session; this is the
  regression [#83] describes, kept out of the new surface by construction and
  by test.
- **Headers:** every response on a guarded path carries
  `Referrer-Policy: no-referrer`, `Cache-Control: no-store`,
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and a CSP with
  `default-src 'none'`, `frame-ancestors 'none'`, `form-action 'self'`, and
  **no script source**. The server-rendered pages serve no JavaScript. Two
  path-scoped exceptions carry their own policy: the acceptance page, described
  under [Invitation acceptance](#invitation-acceptance-inviteaccept), and the
  admin panel at `/admin`, `/admin/` and `/admin/assets/*`, which serves a built
  single-page bundle under `ADMIN_PANEL_CONTENT_SECURITY_POLICY`
  (`web/pages.py`):
  `default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`.
  `web.surface.on_admin_panel()` decides which paths get it.
  They are applied by middleware keyed on the path rather than by each handler,
  so responses no handler of this surface produced — redirects, a 405, an
  unmatched `/web/*` path answered by the router's own 404, and an
  exception escaping a page (which `ServerErrorMiddleware` would otherwise
  answer above this middleware, bare) — carry them too.

[#67]: https://github.com/openteams-ai/collab-hub-pack/issues/67
[#83]: https://github.com/openteams-ai/collab-hub-pack/issues/83

## The per-path protection map

The map's `authenticated` level runs the **API** credential check before
routing, which a browser mid-sign-in cannot pass. The web routes therefore
enforce their own session, CSRF, and role checks in-route (exactly as the API
routers enforce theirs with dependencies), and the map's job is to leave the
prefix reachable:

```yaml
security:
  paths:
    - path: /web
      match: prefix
      access: public
    - path: /invite
      match: prefix
      access: public
```

**The chart ships this**, along with `/invite` and `/admin`, in
`helm/collab-hub/values.yaml`'s default `security.paths`. That is deliberate
and it is the same release as the startup check below: a hardened install
(`api.ingress.enabled`, or `security.enforce: true`) that enabled the web
surface would otherwise be refused at startup on defaults it never edited.
Unhardened installs are unaffected either way — the chart passes `PATHS="[]"`
and `DEFAULT_ACCESS="public"` when `security.enforce` resolves false.

`/admin` is listed **ahead of its routes** ([#91]), which is the one entry that
needs arguing for, since the check itself asks nothing of a routeless prefix.
The alternative rule — each page ships its own entry — leaves `release/public`
transiently broken between two merges: the operator page lands, the check finds
`/admin` resolving to `authenticated`, and every hardened install refuses to
start until the values change catches up. An early prefix is the cheaper
mistake and costs nothing while unused. `/org` stays out, because it is
routeless with no change adding routes to it — there is no merge for an early
entry to be early *for*. The owner invitation page ([#142]) deliberately did
**not** claim it: it mounts under `/web/org/…`, nesting under the
already-exempt `/web` prefix, by the routing decision recorded on that issue —
the public Collab Hub's gateway wall exempts only the prefixes named in
`nebariapp.routing.publicRoutes`, so a page at a fresh top-level prefix ships
straight into the wall (an internal issue was that failure,
live). The rule for every future browser path: nest under an already-exempt
prefix, or land the `publicRoutes` addition in the same change.

On a hardened deployment (`security.enforce`), the app **refuses to start** if
the map does not leave the `/web` and `/invite` routes public — the error names
the exact entry to add — so the documented map and the enforced one cannot
drift apart. A gateway-mode install (empty map, `defaultAccess: public`) needs
no entry.

`/invite` needs the same treatment as `/web` and for a stronger reason: an
invitee has no API credential of any kind, so the map's `authenticated` level
would turn away the entire population the page exists for. Being map-public is
not being unauthenticated — `/invite/accept/redeem` still requires a session
and a CSRF token, enforced by the guard and by its own dependencies.

That refusal is made in two places, because one of them cannot see enough:

| Check | When | Covers |
|---|---|---|
| `enforce_web_surface_preconditions` | config parse, before any store opens | the seven `/web` flow paths and the two `/invite/accept` paths, named literally |
| `enforce_web_surface_map_access` | `make_app` after every router is mounted, and again at boot | **every registered route** under any prefix in `WEB_SURFACE_PREFIXES` |

The first is a floor: it fails early and clearly for the paths whose absence
breaks sign-in for everybody. The second is the one that cannot be forgotten,
and it exists because the first could only name what its author wrote down.
`WEB_SURFACE_PREFIXES` promised `/web`, `/admin` and `/org` while the literal
list covered `/web` alone, so for two of the three prefixes the documented map
and the enforced one *could* drift — the exact failure this check exists to
prevent, pre-loaded for the pages that were about to arrive there.

The failure it removes is two correct components disagreeing.
`PathProtectionMiddleware` is added first in `make_app` and therefore runs
**innermost**, after `WebSessionGuardMiddleware`. So on a hardened map a page
at an unlisted prefix passes the session guard — the person really is signed
in — and is then refused by the *API* credential check, which a browser
holding a web session cookie cannot satisfy. Neither component logs anything
suggesting the map is the problem. Deriving the paths from the route table
instead of a tuple closes it, and a route table cannot drift from itself.

A guarded prefix with **no** routes asks nothing of the map, which is
deliberate: requiring a deployment to open `/org` before anything serves
`/org` would be asking operators to widen a map for paths that do not exist.
(The chart choosing to ship `/admin` early is a separate decision, made on
merge-ordering grounds above — the check does not demand it.)
While a prefix is empty, a request to it matches nothing here and, once the
session guard (which authenticates by path, before routing) has let it
through, answers the router's own 404. That is not a hole — a 404 serves
nothing. (Until [#67] the MCP app was mounted at `/` and answered such paths
instead; it is now registered at `/mcp` only.) Map-public means "the web
surface's own session flow decides this path", never "unauthenticated".

Pages built on the surface register their own paths the same way: `/admin/*`
and `/org/*` public at the map level with session + role enforcement in-route.
Role-scoped map levels remain deliberately rejected — the map must never claim
a protection the middleware does not itself enforce.

## Routes and scaffolding

| Route | What |
|---|---|
| `GET /web` | Signed-in overview; redirects to sign-in when there is no session. |
| `GET /web/signin?next=&renew=` | Starts the code flow. `next` accepts only app-relative paths that are not the flow's own routes — anything else falls back to `/web` (no open redirect, and no self-referential loop). `renew=1` runs the flow even with a valid session, which is how the acceptance page obtains current claims. |
| `GET /web/oidc/callback` | Finishes the flow, mints the session. Every failure renders one fixed page; nothing from the request or the IdP response is echoed. |
| `POST /web/signout` | CSRF-protected; clears the session cookie. |
| `GET /web/signed-out` | Confirmation page. |
| `GET /web/app.css` | The shared stylesheet (documents keep `style-src 'self'`). |
| `GET /web/collab-logo.png` | The product wordmark the server-rendered pages show (documents allow `img-src 'self'`). **Anonymous**, like the stylesheet: the sign-in and acceptance pages show it before anybody has a session. |
| `GET /web/data-statement` | The data statement ([#146]): what is stored, who can see it, and the address deletion requests go to. **Anonymous** — see below. The copy lives in `web/data_statement.py` and the acceptance page renders the same constant above its accept control. Links to the two documents below. |
| `GET /web/terms` | The canonical Terms of Service ([#95]). **Anonymous** — see below. The copy lives in `web/terms_of_service.py` as one constant; placeholder until counsel replaces it. |
| `GET /web/privacy` | The canonical Privacy Statement ([#95]). **Anonymous** — see below. The copy lives in `web/privacy_statement.py`, same shape. |
| `GET /invite/accept` | The acceptance page ([#90]). **Anonymous** — see below. |
| `POST /invite/accept/redeem` | Redeems the token from a JSON body. Session + CSRF required. |
| `GET /admin/invitations` | The operator invitation page ([#91]). Session + `operator`. |
| `POST /admin/invitations` | Issues one invitation and renders its link. Session + `operator` + CSRF. |
| `POST /admin/invitations/revoke` | Revokes one invitation. Session + `operator` + CSRF. |
| `GET /admin` | Redirects (308) to `/admin/`. The trailing slash matters: the panel's asset and API URLs are relative to the document. Session + `operator`. |
| `GET /admin/` | The admin panel's document (`routers/admin_ui.py`), served from `web.admin_ui_dist`. Session + `operator`. Mounted only when that directory holds an `index.html`. |
| `GET /admin/assets/*` | The panel's hashed bundle files. File names are checked against a strict pattern and must resolve inside the assets directory; anything else is 404. Session + `operator`. |
| `/admin/api/*` | The panel's JSON API (`routers/admin_api.py`): session, models, model access, users and roles, connectors, usage, invitations and the audit log. Session + `operator`; every `POST` also needs the `X-CSRF-Token` header, whose value `GET /admin/api/session` returns. Refusals are JSON. |
| `GET /web/org/invitations` | The owner invitation page ([#142]). Session + org `owner`. |
| `POST /web/org/invitations` | Issues one invitation into the caller's org and emails it. Refused (409) while the organization still carries the placeholder name ([#44]). Session + org `owner` + CSRF. |
| `POST /web/org/invitations/revoke` | Revokes one of the caller's org's invitations. Session + org `owner` + CSRF. |
| `POST /web/org/invitations/name` | Gives a placeholder-named organization its name, once; audited as `org.rename` ([#44]). Session + org `owner` + CSRF. |

The `/admin` paths must be **map-public** in `security.paths`, like the rest of
this surface, and for the same reason: the map's `authenticated` level runs the
*API* credential check and an operator holds a browser session, not a bearer
token. Startup refuses a map that hides them. Authorization is not thereby
weakened — the session guard and the operator role are both enforced in-app,
per request.

### Authenticated by default

Every path under a guarded prefix requires a session unless it appears in
`web.surface.PUBLIC_WEB_PATHS`, which names exactly nine: sign-in, the
callback, the signed-out confirmation, the stylesheet, the product wordmark,
the acceptance page, the data statement, and the Terms of Service and Privacy
Statement.

The last two ([#95]) are linked from the deployment's Keycloak
terms-acceptance step, which runs *before* Keycloak issues a token — and this
surface's session is minted from that token. A session gate on them would
mean the documents could only be read by someone who had already accepted
them, which is not a stricter policy but an incoherent one.
That is enforced by a middleware, not by a convention, because the protection
map cannot supply it — the map's `authenticated` level runs the *API*
credential check, which a browser mid-sign-in cannot pass, so `/web` must be
map-public.

Build a page router with `routers.web.session_gated_router()` and pass it to
`make_router(surface, page_routers=[...])`. Either alone is sufficient: the
helper carries the dependency, and `make_router` includes page routers *inside*
the gated router so a bare `APIRouter` inherits it.

A page that genuinely must be anonymous goes through
`make_router(surface, public_page_routers=[...])`, which **refuses** anything
that is not all three of: a real `APIRoute`, on a path already in
`PUBLIC_WEB_PATHS`, answering only `GET`/`HEAD`. Choosing that argument
therefore buys placement, never anonymity — anonymity still costs the reviewed
line in the allowlist — and it cannot be used to hang a state-changing handler
off an anonymous path.

That last rule matters because `PUBLIC_WEB_PATHS` is a set of **paths**: the
guard runs before routing and has no route to ask about methods, so a path in
the set is anonymous for *every* method. A handler that changes something must
live at its own path outside the allowlist, the way `/invite/accept/redeem`
does.

Neither, however, can stop a registration that happens **outside** that path —
`app.include_router()`, `app.mount()`, a `WebSocketRoute`, or a dependency that
merely *looks* like the session check. Every one of those was demonstrated
serving an anonymous page.

So the surface does not try to prove that routes enforce sessions. **The guard
enforces the session itself.** `WebSessionGuardMiddleware` is raw ASGI and runs
before routing: for any request whose path is under a guarded prefix and is not
in `PUBLIC_WEB_PATHS`, it validates the session cookie and redirects to sign-in
when there isn't a valid one. It consults no route, no dependency, and no
marker.

That makes the failure mode benign: a page that forgets `require_web_session`
is *authenticated by the guard* rather than served anonymously, and forging a
marker buys nothing because nothing on the request path reads one.

| Piece | Role |
|---|---|
| `WebSessionGuardMiddleware` | **The control.** Path in, session out. |
| `require_web_session` | **A convenience.** Still runs at the route, still hands the handler a typed `WebSession` — but it is no longer the boundary. |
| `verify_web_route_protection` | **Both.** A *lint* for the session dependency — it shouts at startup about routes missing it, because the handler won't get its session object, and its failure can no longer mean anonymous access. The *enforcement* for CSRF, mounts and sockets, where nothing else covers the gap. |

Two route types are still refused outright at startup, because a path-based
HTTP guard cannot stand in for them:

| Route type | Rule |
|---|---|
| `Mount` | **Refused** unless in `ALLOWED_WEB_MOUNTS` (empty). A sub-application's routing is opaque. Note this is not an authentication exemption — the guard still authenticates every path into a mounted app — it only permits mounting at all. |
| `WebSocketRoute` | **Refused.** The surface serves no sockets, and there is deliberately no flag to change that: a boolean would permit every socket with no session check anywhere. Connections are also refused at request time (close code 1008), covering a socket registered after startup. |
| anything not an `APIRoute` | **Refused** by the lint — an `isinstance` check, not a duck-typed `.dependant`, which a fabricated object satisfied. |

The guard derives the request path with Starlette's own `get_route_path()`,
not a reimplementation. This matters more than it looks: a hand-rolled
`root_path` strip disagreed with Starlette's segment-aware one, and under
`root_path="/"` the guard decided a request was off-surface while the router
routed it to the handler — an anonymous page produced purely by two functions
disagreeing about what "the path" means. Any path-based control here must take
its path from the same function the router does.

**Every guard refusal closes the connection** (`Connection: close`, on both
the sign-in redirect and the authorization-unavailable 503). The guard answers
before routing and never reads a request body, so a refusal of a body-bearing
`POST` would otherwise leave the server working through what the client is
still sending before it could begin the next cycle — the connection is held,
measured at about twenty-five seconds against uvicorn. That became reachable
with the first body-bearing POST route on the surface
(`/invite/accept/redeem`): the handler closes its own refusals, but a request
the guard rejects never reaches the handler.

It is unconditional rather than keyed on the method, because the invariant
that makes it correct — this middleware never consumes a body — is a property
of the guard and holds for every request it sees, while "does this method have
a body" is a second thing to get right on attacker-supplied input. The
WebSocket refusal needs nothing: a handshake carries no body and that path
closes the socket outright.

The guard sits inside `RequestObservabilityMiddleware`, so a refusal gets a
request id, an access-log line, and a metrics sample — denials are what an
operator most needs to see. WebSocket refusals emit the same log line and
metric explicitly, because `BaseHTTPMiddleware` never runs for socket scopes.
It fails closed with the documented 503 if it cannot reach or use the
surface's codec, and that response renders no internals.

Two details of that instrumentation are load-bearing rather than incidental:

- **Metric labels never carry caller-chosen values, on either axis.** A denial
  happens before routing, so there is no route template; the `path` label is
  the fixed `<unmatched>` sentinel, and the `method` label is mapped to
  `OTHER` for anything outside a fixed set of HTTP verbs. Both were live
  cardinality vectors — an unauthenticated caller could mint one Prometheus
  series per invented path, and then per invented extension method — and
  unbounded cardinality is memory exhaustion of the metrics store by anyone
  who can reach the port. The access log still records the real path and
  method, because a log is bounded by retention and "what was refused" is the
  operator's actual question.
- **The correlation id is always server-generated.** An inbound
  `X-Request-ID` is not adopted: these endpoints are reachable without
  credentials, so honouring it would let anyone stamp their requests with a
  known id and make a victim's trail indistinguishable from theirs. The
  caller's value is still recorded, under `client_request_id`, bounded to 128
  characters of an id-shaped alphabet and dropped otherwise.

A page route is identified by its dependence on `require_web_session`. One
registered outside every prefix in `WEB_SURFACE_PREFIXES` **fails the
rollout**: the guard keys on those prefixes, so such a page would be reachable
without a session no matter what its dependencies say. Adding a prefix is
therefore a deliberate edit, not something that can be forgotten into.

### Building a page

Page routers compose `collab_hub_api.web.pages.render_page` for the layout
(escaping every dynamic value with `pages.escape`) and take their access rules
from `collab_hub_api.web.authz`:

- `require_web_session` — a valid session, else a 303 through sign-in that
  returns to the requested page.
- `require_operator` — `platform_role = 'operator'`, resolved per request from
  `OrgStore.resolve_principal` (issue [#87]'s `collab_platform_roles`), which
  is canonical and **outranks** the `app.state.platform_role_resolver`
  override; the override is consulted only when the store offers no answer, so
  a stray assignment cannot grant authority the server's own table never
  recorded. The resolved role must be an actual `str` and is compared exactly —
  a non-string is logged and refused, never compared, because an object with a
  permissive `__eq__` was otherwise enough to obtain operator access. With
  **no** source available
  the dependency raises `WebAuthorizationUnavailable`: a 503 page and an
  error-level log, deliberately not a 403, because a missing role source locks
  out every operator and must not look like a correct refusal. It never
  grants — `app.state.web_platform_role_source` reports the resolved source at
  startup, and its absence is logged then too.
- `require_org_owner` — org `role = 'owner'`, read live from the org
  membership store.
- `require_csrf` — for every state-changing method, and **the startup check
  enforces this one** rather than merely reporting it.

#### CSRF is the dependency the guard inversion did not make safe

Forgetting `require_web_session` is harmless: the guard authenticates by path
whatever the route says. Forgetting `require_csrf` was not, and nothing else
covers it — so a `POST` added by a later page with no token check was silently
unprotected, with nothing at startup noticing.

`SameSite=Lax` bounds that but does not close it. The registrable domain is
`openteams.app`, so a request from any sibling subdomain is **same-site** and
carries the cookie; the `__Host-` prefix prevents cookie *planting*, not
cookie *sending*.

So `route_offence` refuses to serve any non-public route whose methods include
`POST`, `PUT`, `PATCH` or `DELETE` unless `require_csrf` is in its dependency
tree — walked to any depth, so a page that wraps the CSRF and role checks in
one dependency still passes. This is enforcement against *forgetting*, not
against an adversary: only someone who can register routes can trip it.

A handler may also run the check itself, and two pages have real reasons to.
`/invite/accept/redeem` ([#90]) calls it by hand so a content-type gate
provably runs first — `require_csrf` falls back to parsing a form, and a form
parse is an unbounded read — and `/admin/invitations` ([#91]) runs the same
comparison as a predicate because it answers a refusal by re-rendering its own
page rather than the surface's fixed 403 — as does the owner page ([#142]),
through the same shared predicate. None of this is visible to a dependency
walk, and no check can make it visible: an endpoint that calls the comparison
is indistinguishable from one that does not without reading its body, and
reading a body for a *name* is the label-not-structure mistake this module
exists to avoid. Such a route is therefore declared in
`web.surface.CSRF_ENFORCED_IN_ROUTE` — one reviewed line, exactly like
`PUBLIC_WEB_PATHS`.

The set ships with **every** exemption the surface needs, and it is
worth knowing why they live here rather than on the changes that add their
routes:

| Path | Added by | Checks CSRF via |
|---|---|---|
| `/invite/accept/redeem` | [#90], merged | `await require_csrf(...)`, after a content-type gate |
| `/admin/invitations` | [#91], open | `_csrf_ok()` → `csrf_token_matches`, as a predicate |
| `/admin/invitations/revoke` | [#91], open | same |
| `/web/org/invitations` | [#142] | `web.forms.csrf_ok()` → `csrf_token_matches`, as a predicate |
| `/web/org/invitations/revoke` | [#142] | same |
| `/web/org/invitations/name` | [#44] | same |

The check and the routes land in **different changes**, and that is the whole
argument. Split across both — an empty set here, a route there with no
dependency — each half is inert alone and the merged pair makes the API
**refuse to start**. Getting the count wrong therefore costs a *second broken
build* rather than a wrong answer, so the registry carries every entry it will
need and removes the coordination dependency on the other change remembering.
It is the same reasoning that puts `/admin` in the chart's default protection
map ahead of its routes.

The two `/admin` entries are **string literals**, not constants:
`ADMIN_INVITATIONS_PATH` and `ADMIN_INVITATIONS_REVOKE_PATH` live in
`web/surface.py` on #91's branch and do not exist here. Declaring them locally
to make the entries look tidy would create a second spelling of each path —
the drift this module removed for the stylesheet — so the literals stand until
#91 lands and those two lines become the constants.

Entries may lead their routes because `stale_csrf_exemptions` judges only
paths that are actually mounted; see below.

An entry also has to keep describing its route. Once the path it names is
mounted, startup refuses if that route is off-surface, answers no
state-changing method, or **already declares `Depends(require_csrf)`** — the
last being the interesting rot, because a route that gains the dependency
leaves behind an entry claiming an in-route check that is no longer there, and
the registry silently stops meaning what it says.

An entry naming a path with **no** mounted route is tolerated, deliberately,
and that tolerance is load-bearing twice over. Two of the three shipped entries
name routes #91 has not landed, so a check requiring every entry to be mounted
would refuse to start this branch. And after #91 lands it still matters:
`make_app` mounts the operator router only when `org_source_resolves_membership()`,
so on a claims-sourced deployment those routes are legitimately absent while
the entries are correctly present, and failing on absence would refuse every
such deployment. That leaves a typo inert, and the cost is
tidiness rather than safety: a misspelled entry exempts nothing, so the route
it was meant to cover is still refused by the primary check, loudly, naming
the real path.

### These checks are a rollout gate, not a runtime control

`verify_web_route_protection` and `enforce_web_surface_map_access` run twice —
in `make_app` once every router it mounts is registered, and in the lifespan
for anything a caller added afterwards — and then never again. **A route
registered after the server starts accepting traffic is not rechecked.** For
CSRF, whose only other enforcement is the per-route dependency these walk for,
such a route is genuinely unprotected and nothing here will say so.

That gap is accepted, and the reasoning is the same one that decided the
session guard the other way. The session check became request-time because
route *structure* was being trusted as an authentication boundary, and
structure is authored by exactly the person the boundary defends against. CSRF
is not that shape: the attacker is a cross-origin page, and a cross-origin page
cannot register a route. The only actor who can is code already running in this
process, and a route added post-boot is not a deployment path — `make_app`
registers everything a pod serves before the lifespan opens. A request-time
CSRF middleware would close it by re-deriving on every mutating request what
the dependency already decided, against nobody.

So: **the session is enforced at runtime** (the guard), **CSRF is enforced
per route** (`require_csrf`, or its declared in-route equivalent), and **these
checks enforce at rollout that those are present**. A passing startup is not a
claim about routes that did not exist when it ran. Both halves of that boundary
are asserted in `test_web_surface.py` so the claim and the code cannot drift.

[#91]: https://github.com/openteams-ai/collab-hub-pack/issues/91

[#87]: https://github.com/openteams-ai/collab-hub-pack/issues/87

## Invitation acceptance (`/invite/accept`)

The page an invitation link points at. The one-time secret is delivered in the
URL **fragment** (`…/invite/accept#token=…`), which browsers never transmit —
so it appears in no request line, no `Referer`, and no access log. The page
reads it in the browser, banks it in `sessionStorage` across the Keycloak
registration / sign-in round trip, and sends it to
`POST /invite/accept/redeem` in a **JSON body**.

### The one page that runs JavaScript

A fragment is only readable from client-side script. Keeping the token out of
the request line and keeping the surface script-free are mutually exclusive,
and the token requirement wins — so this page, and only this page, relaxes the
CSP:

```
default-src 'none'; style-src 'self'; script-src 'sha256-…';
connect-src 'self'; img-src 'self'; base-uri 'none';
form-action 'self'; frame-ancestors 'none'
```

Everything about that is deliberately narrow:

- **Scoped by path.** `web.pages.headers_for_path()` returns this policy for
  `/invite/accept` and the default policy for every other path, and the
  security-header middleware consults it. Path in, policy out — the same shape
  as the session guard, and for the same reason: a per-response flag would be
  set by whoever is most likely to get it wrong.
- **Pinned to one hash.** No `'unsafe-inline'`, no `'unsafe-eval'`, and
  deliberately no `'self'` — one SHA-256 digest is the page's entire script
  budget, so an injected `<script>`, same-origin or not, does not run. The
  digest is computed at import from the served bytes, so the policy cannot
  drift from the code, and a test re-derives it from the response body.
- **Everything else is unchanged.** `no-referrer`, `no-store`, `nosniff`,
  frame-deny, `default-src 'none'`, `base-uri 'none'` and
  `frame-ancestors 'none'` all still apply. `connect-src 'self'` is the only
  other addition, and it is what the redemption POST needs.
- **A failed page is stricter, not looser.** If the page raises, the surface's
  error document is served with the default no-script policy.

If the inconsistency looks like a bug: it is not. Narrow the exception if you
can; do not widen `CONTENT_SECURITY_POLICY` to match it.

### Token handling, and what is actually proven

The application never logs the token, and it never reaches a request line, a
query string, a path, a header, a form field, or the DOM. The redeem body is
parsed **by hand** rather than declared as a FastAPI body model, because a
pydantic `ValidationError` carries the rejected input — which is how an
almost-valid one-time secret ends up in a 422 body. Every exception in that
parsing frame is caught inside it, so no traceback holding the raw string ever
propagates; above it the value exists only inside an `InvitationSecret`.

**Not provable here:** that the gateway does not log request bodies. It is a
POST body by design, so a body-logging proxy will capture it. That half is
[an internal issue], verified against the running deployment.

[an internal issue]: an internal issue

### Behaviour

- **Redemption needs a click.** Joining an organization is permanent in this
  beta — one organization per login — so the page shows the signed-in account
  and waits for an explicit *Accept invitation*. Without that, anyone able to
  issue an invitation to a known address could bind that login to their
  organization by getting the person to open a URL, and the CSRF token is no
  defense because the page reads it from its own DOM.
- **A reload does not re-redeem.** After an outcome that consumes or kills the
  invitation, the browser drops the token and records the result. Outcomes the
  person can act on — mismatched address, unverified email, already in an
  organization, service unavailable — keep the token so a retry is one click
  rather than a hunt through their email. The service is idempotent for the
  same login regardless (#89's replay semantics).
- **Terminal states** each render their own copy: not found, expired, revoked,
  already used, mismatched address, unverified email, already in an
  organization, the organization gone, and unavailable. A test asserts every
  `InvitationError` subclass has one, so a new terminal state fails the suite
  rather than falling through to the generic error page.
- **Claims-sourced deployments refuse.** Redemption writes
  `collab_org_members`, which claims-mode authentication never reads, so the
  endpoint answers `invitations_unavailable` instead of reporting a success
  that granted nothing.
- **The session carries `email_verified`**, recorded from the ID token at
  sign-in, because the browser never holds that token afterwards and acceptance
  may need it. A session minted before that field existed decodes as
  unverified — fail-closed. Whether acceptance *requires* it is
  `frames.invitations.requireVerifiedEmail` (default on); the address match it
  is paired with is not configurable.

### The verified address has to be current

Where `frames.invitations.requireVerifiedEmail` is off, the revocation argument
below no longer applies — acceptance does not read `email_verified` as an
authorization input at all — and **the bound is unchanged anyway.** What still
matters there is the `email` claim's currency: it is an authorization input in
both modes, since the address match is not configurable, and an address can be
reassigned at the IdP just as a verification can be revoked. So the window
keeps its value with half of its original justification unused, rather than
being loosened because one reason weakened.

Holding a session is not enough to redeem. Identity in the cookie is stable —
a subject does not stop being that subject — which is what makes an eight-hour
session acceptable for it. The verified-address pair is different in kind: it
is a fact about the account **at the IdP**, the IdP can withdraw it, and what
a redemption decides on it is permanent (one organization per login). An
assertion up to a whole session old could bind a membership on a verification
that had already been revoked.

So redemption requires the assertion to be no older than
`web.session.VERIFIED_CLAIM_MAX_AGE_SECONDS` (5 minutes) **by the deciding
replica's clock**, and otherwise answers
`reauthentication_required`. The page then offers
`/web/signin?next=/invite/accept&renew=1`, which runs the authorization-code
flow **even though a valid session exists** — that is what the `renew` flag is
for, and it can only ever cause more authentication, never less. Keycloak then
mints a new ID token from the *current* user record, which is usually
invisible: an active SSO session satisfies it without prompting, and protocol
mappers read the user at token-mint time rather than at sign-in.

The surface cannot simply re-read the IdP: it keeps no access or refresh token
(deliberately), and acquiring one to close this would be a larger regression
than the gap. Stated no wider than it is: **the assertion a redemption acts on
was minted by the IdP within
`VERIFIED_CLAIM_WORST_CASE_AGE_SECONDS` — six minutes — not up to eight hours
earlier.** It is a bound, not "as of this instant": nothing short of reading
the IdP inside the redemption transaction gives that, and no OIDC relying
party has it.

Six, not five, and the difference is worth writing down rather than rounding
away. The session codec accepts an `iat` up to `CLOCK_SKEW_SECONDS` (60s) in
the future so replicas whose clocks disagree do not reject each other's
cookies; a session minted by a replica at that limit is therefore up to a
minute older in real time than the deciding replica computes. The constant is
derived from the two numbers rather than written down, so raising the skew
allowance moves the documented guarantee with it.

The page's render-time `data-claims-current` is a hint that saves a click; the
endpoint re-checks, because the window can lapse between render and click.

**Deployment verification item, not yet done:** this rests on Keycloak
rebuilding `email_verified` from the current user record when it issues a
token off an existing SSO session, rather than replaying the value from
sign-in. That is how protocol mappers are documented to work and it is what
the stub IdP models, but it has not been exercised against the deployed
Keycloak. Confirm it there — withdraw a verification, renew, and check the new
ID token — before treating the freshness guarantee as established.

### Request-size limit

`POST /invite/accept/redeem` reads at most `MAX_REDEEM_BODY_BYTES` (2 KiB),
enforced by **counting the bytes it reads**, not by consulting
`Content-Length`. A chunked request carries no such header at all, so a
header-only check was no limit against exactly the caller it needed to bound.
The header remains a fast path that refuses a body the caller admits is too
big; it is not the gate.

**Any refusal issued before the body has been read closes the connection**
(`Connection: close`, which is what a 413 conventionally does). Stopping the
read at the cap fixes memory and breaks connections if you stop there:
answering an HTTP/1.1 request whose body has not reached end-of-message leaves
the server working through what the client is still sending before it can
start the next cycle, so the connection is held — measured at about ten
seconds against uvicorn, versus under one with the close. That is the same
exhaustion moved from one resource to another. Draining instead would mean
reading past the cap, which is the original problem again.

`_outcome_response` therefore takes a required `body_consumed` argument with
no default, so a refusal added above the read has to answer the question
rather than inherit the wrong answer.

The endpoint also refuses any content type that is not `application/json`,
*before* the CSRF check runs. That is not fussiness: `require_csrf` falls back
to parsing a **form** when no `X-CSRF-Token` header is present, and
`request.form()` buffers a urlencoded body with no cap of its own — so the
gate is what keeps this endpoint from delegating an unbounded read. The order
is written out in the handler rather than expressed as dependency ordering,
because a security property should not rest on how a framework happens to
sequence its dependency graph.

That same form fallback is reachable on the surface's other POST routes; it is
#88's shared dependency and is tracked as [#119] rather than changed here.

A CSRF refusal on this route is answered as JSON rather than as the surface's
HTML 403 page, so the endpoint's contract is JSON on every path. Catching the
exception takes the route out of the shared exception handler's reach, so it
re-emits `web_forbidden` with the same `reason` field an operator's existing
query groups by, alongside its own `web_invitation_accept_csrf_refused`.

[#119]: https://github.com/openteams-ai/collab-hub-pack/issues/119

## Operator invitations (`/admin/invitations`)

The first **operator action** on this surface, and Gate E's decision was that
it exists as much to set the template as to send invitations. An operator types
an address, the page issues an invitation, and it shows the invitations on this
deployment with their state and a revoke control.

### The operator-action pattern

Four parts, in `web/operator.py` and `routers/admin.py`. A future operator page
copies this shape and should need nothing else:

1. **The router carries the role**, not each handler:
   `APIRouter(dependencies=[Depends(require_operator)])`. Every route on it —
   the page included — refuses a signed-in non-operator with the surface's own
   403 page. Not a 404, not a blank page, not an API error envelope.
2. **The action is authorized again, on the function that performs it**, with
   issue [#87]'s `@requires_platform_role("operator")`. The router dependency
   protects the *route*; the decorator protects the *action*, which outlives
   any route and can be called from a future CLI or MCP tool.
3. **The authority those guards read is resolved, never asserted.**
   `operator_context()` builds the `AuthContext` with whatever
   `resolve_platform_role()` actually answered. Stamping
   `platform_role="operator"` because the request got this far would make the
   guard compare a constant with itself — worse than no check, because it reads
   as one. The role is therefore resolved twice on a mutating request, and that
   is the intended cost.
4. **The event row comes from the shared primitive.** No page code writes
   `collab_audit_events`; `audited()` is its only writer, composed inside the
   invitation service. An operator-issued `invitation.send` row is identical to
   an owner-issued one apart from `actor`/`actor_label`.

Deliberately absent: cross-org browsing, member management, org deletion. Gate
E scoped the operator surface to invitation issuance exactly.

### What the page does, and does not, create

Every invitation issued here carries a **null `org_id`**. The organization is
created atomically on first acceptance, with the accepter as its owner (Gate B,
revised 2026-08-04), and there is no field on this page that can name one.
Pre-creating an organization would leave an orphan behind every invitation that
is revoked, expires, or is never accepted, and would move the `org.create`
actor from the accepter to the operator.

### One live invitation per address — issued *from this page*

Issuing twice from this page for the same address does **not** mint a second
live token. The service's `create_unless_live()` refuses while a `pending`,
unexpired invitation for that address exists and returns that invitation
instead; the page says so and points at the revoke control. The check runs
inside the audited transaction behind a transaction-scoped advisory lock keyed
on the address, because a row that does not exist yet cannot be locked and two
concurrent issuances would otherwise both read "none" and both insert — which
is exactly what a double-submitted form is.

**The scope is the page, not the deployment.** The `/v1` operator and owner
routes still call `create()`, which mints unconditionally, so an address can
hold two live invitations if one came from the API. That is the milestone's
deliberate line: the rule exists for a human retrying after an ambiguous send,
and unifying the paths would change the semantics of a shipped endpoint the
desktop is built against. Re-send policy, including rotation, is #93.

Matching is **exact**, like every other address comparison here (Gate B):
`Alice@example.com` and `alice@example.com` are two addresses.

### The invitation email is the secret's only route off this page

Issuing hands the freshly minted secret to `InvitationEmailDelivery.deliver` —
the same adapter [#89]'s API route uses — and drops it. The page words the
sanitized outcome: sent, could not be sent, or unconfirmed, never the
provider's own error text. **No response of this surface carries the token**,
which is checked across every response of the whole flow, headers and cookie
jar included.

R3 holds whole here, and the parts that were always true are unchanged: the
token appears in **no log** at any level, is hashed at rest, single-use and
replay-protected, and reaches the handler through an **in-process** call to the
invitation service — proven by issuing successfully on an app whose `/v1`
invitation routes have been removed.

The issue `POST` still **renders** rather than redirecting, because the
delivery outcome belongs to the response that produced it and a redirect would
mean carrying it in a URL, a cookie, or server-side session state.

The send is sequenced **after** the audited transaction commits, per [#89]'s
contract: a send cannot be rolled back. The residual risk therefore runs the
other way — a committed invitation whose mail failed — which is why that
outcome is a sentence on the page rather than a silently swallowed error.

#### What this replaced, and how it lapsed

Between the 2026-08-07 amendment to [#91] and the completion of
[an internal issue], this page did the opposite: it **rendered the live
secret** for an operator to copy into a human-composed message, and suppressed
the SES send, because one single-use secret travelling two routes doubles its
exposure and makes "was it sent?" ambiguous. `web/invite_link.py` held that
decision, its own end condition, and the recipe for its removal; a test
enumerated every file that mentioned it so the recipe could not silently stop
being complete.

Invitation mail is deliverable, so the module is deleted, the send is restored
in the same edit, and the owner page's no-provider fallback went with it — a
deployment with no mail provider is now **warned above the form** and, if it
issues anyway, told the send failed. That is the correct answer once mail is
the delivery channel: there is no second route a secret can take.

One decision does **not** roll back automatically. `INVITATION_TTL` was
shortened to 48 hours as the display's compensating control
(collab-hub-pack#131); restoring 7 days is a decision with its own receipt,
not a consequence of this one, and it has not been taken.

[an internal issue]: an internal issue
[#89]: https://github.com/openteams-ai/collab-hub-pack/issues/89

### Where the surface's URLs point — configuration only, never the request

`web.public_base_url`, and **nothing else**. There is deliberately no fallback
to the request's base URL: the surface builds absolute URLs for every operator
and owner who signs in — the OIDC `redirect_uri` among them — and taking their
origin from the request means taking it from a `Host` the caller chooses.
Behind a proxy that does not forward its scheme the result is `http://`, which
Keycloak refuses; a forged header is an origin nobody configured.

No syntactic check substitutes for this — every attacker-chosen host is a valid
https host — so `web.public_base_url` is **required at startup** whenever the
browser surface is enabled on a membership-resolving deployment. A misconfigured
deployment fails to boot rather than at an operator's first sign-in.

This requirement was introduced for the rendered link and is deliberately
**not** relaxed along with it: a deployment that reaches an operator page has
an external origin, and saying so once at boot is cheaper than every URL the
surface builds guessing.

### Request bodies are bounded by counting, not by `Content-Length`

Both `POST` routes refuse a body over 4 KiB, and the bound is enforced by
counting what arrives — `web/request_limits.py`, shared with the acceptance
page. `Content-Length` is a fast path and never the gate: a chunked request
carries none, so a header-only check was no limit at all against exactly the
caller it needed to bound. That defect shipped in the first draft of this page
and 2,000,000 bytes went through the cap; the acceptance page had already fixed
the same thing, which is why the implementation is now shared rather than
written twice.

Neither route calls `request.form()`. The body is read under the cap and the
urlencoded fields are parsed from those bytes, so Starlette's unbounded form
parse is not on either path — and `Content-Type` must be
`application/x-www-form-urlencoded`, checked before anything is read, which
keeps multipart parsing (whose cost is not bounded by byte count alone) off a
page that uploads nothing.

A refusal issued **before** the body was read sends `Connection: close`.
Answering without it leaves the server stalled on that connection until
something times out — the same exhaustion moved from memory to sockets. That is
covered by tests against a real uvicorn over a raw socket, because an
ASGI-transport test cannot observe chunked framing, keep-alive, or a held
connection at all.

### Not mounted where operators cannot exist

Claims-sourced deployments never read the `collab_` tables, so `platform_role`
is structurally `None` and there are no operators. The page is not mounted
there at all: absent is a truer answer than a page that refuses everyone.

## Owner invitations (`/web/org/invitations`)

The first **owner action** on this surface ([#142]), and it is [#91]'s
template instantiated for the org axis — `web/owner.py` is the counterpart of
`web/operator.py`, and its module docstring records exactly what carries over
and what the axis changes. An owner types an address, the page issues an
invitation **into their own organization**, and it lists that organization's
invitations with their state and a revoke control.

What differs from the operator page, and why:

- **The gate is org ownership, resolved live.** The router carries
  `Depends(require_org_owner)`; the actions carry
  `@requires_org_role("owner", org_arg="org_id")`, pinned to the organization
  `owner_context()` resolved from the caller's membership row. The
  organization is **never a form field** — a smuggled `org_id` in the POST
  body changes nothing — and revocation re-asserts the scope inside the
  transaction with `expect_org_id`, so another organization's invitation is a
  plain not-found.
- **Invitations carry the caller's `org_id`**, so acceptance joins the
  existing organization as a `member` — no organization is created, which is
  the operator page's job. The listing is `list_for_org`: org-creating
  invitations belong to no organization and never appear on it.
- **Nothing, in delivery.** Both pages hand the secret to
  `InvitationEmailDelivery.deliver` and word the sanitized outcome — sent,
  could not be sent, or unconfirmed — never the provider's error text. The
  owner page briefly had a second mode that rendered the link where no
  provider was configured; it is gone with `web/invite_link.py`, and the
  adapter's `configured` attribute now decides only whether the page **warns
  the owner before they issue** that this deployment cannot send mail.

Everything else is deliberately identical: `create_unless_live` under the
advisory lock (one live invitation per address, issued from this page),
`web.forms`' bounded body handling and in-route CSRF predicate, fixed
notices, no JavaScript, render-not-redirect on the POSTs, and the same
mounting rule — claims-sourced deployments have no org roles, so the page is
absent there rather than a page that refuses everyone.

### The first-invite naming step ([#44])

Every organization starts with the neutral placeholder name — `collab_orgs.name`
defaults to `'Unnamed organization'` and accepting an org-creating invitation
never supplies one (Gate B revision of 2026-08-04). So the first owner to reach
this page owns an unnamed organization, and the page identifies the destination
of every invitation they issue only as "Unnamed organization". [#92] specified
a rename → invite sequence for exactly this; [#142] shipped without it, and the
live hub showed the consequence on 2026-08-25.

**Scope: the owner's side only.** The invitation email is organization-neutral
by decision — `render_invitation_email()` discards `organization_name`, and a
regression in `test_invitation_email.py` pins that the name appears in neither
subject nor body. This step does not change that. What it changes is that an
owner has named, and can see, the organization they are inviting people into
before the first invitation leaves; the name lives on this page, on the
organization's record, and on the `org.rename` audit row. The delivery adapter
still receives the resolved name (as it always did) and the renderer's
treatment of it remains the renderer's decision.

The page now branches on `is_placeholder_organization_name()`:

- **While the placeholder stands**, the issue form is not rendered; a naming
  form (`organization_name`, ≤ 120 characters) posts to
  `/web/org/invitations/name`, and `POST /web/org/invitations` answers **409**
  with a fixed notice for any submission that passes the page's ordinary
  request checks (bounded body, CSRF, a valid address) — before the audited
  issue action and before the mail seam. A submission that fails those checks
  is answered the way it always was (413/403/400), so no body shape reaches
  issuance. The server refuses; the missing form is the honest rendering of
  that refusal, not the enforcement.
- **Naming is one shot.** `name_organization()` reads the current name
  `FOR UPDATE` inside the audited transaction and refuses
  (`OrganizationAlreadyNamedError`, 409 on the page) unless it is still the
  placeholder. Two owners naming at once resolve to one `org.rename` row and one
  refusal; a stale tab cannot overwrite a chosen name. Changing a name that was
  already chosen is a different action for a different surface — an operator
  today, by hand — and this page does not offer it.
- **The row.** `org.rename`, actor = the owner, target = the organization, and
  the new name as `target_label`, written by `audited()` like every other
  mutation here; page code writes nothing. The name itself never reaches a log
  line — the log carries the owner and the org id.
- **The name is validated as display text**: stripped, non-empty, ≤ 120
  characters, one line by Unicode category (every `Cc` control including the
  C1 block, the `Zl`/`Zp` separators, and `Cf` format characters such as the
  bidi overrides are refused — wider than the audit primitive's ASCII-only
  rule, so nothing accepted here is refused there), whitespace-normalized
  (every Unicode space separator becomes one ASCII space, runs collapse) with
  at least one letter or digit (so a Braille blank, combining marks alone, or
  bare punctuation cannot be a name), and not the placeholder in any ASCII
  capitalization or spacing — typing "Unnamed organization" would satisfy the
  check while leaving the owner exactly as uninformed.

Once named, the page is what [#142] shipped: the issue form, the org's name in
the intro, and the listing.

Before (shipped [#142], placeholder-named org) · after (naming form) · after
naming:

![Before: the issue form offered "Invite someone to join Unnamed organization"](images/owner-invitations/44-before-placeholder-issue-form.png)
![After: the naming form replaces the issue form while the placeholder stands](images/owner-invitations/44-after-naming-form.png)
![After naming: the confirmation notice and the issue form for "Acme Widgets"](images/owner-invitations/44-after-named.png)

## What this surface deliberately does not do

- No RP-initiated (Keycloak) logout: sign-out ends the app session only; the
  Keycloak SSO session is the IdP's own, and ending it belongs to the page
  that needs that semantics.
- No JavaScript on the server-rendered pages except the acceptance page:
  plain documents and forms, so the CSP forbids script outright on every other
  server-rendered path. That includes the operator page, which once rendered a
  live secret, so it is checked explicitly. The admin panel at
  `/admin` is a separate single-page app: it serves its own bundle from this
  origin under its own CSP (`script-src 'self'`, no inline script, no other
  origin), quoted in full in the headers list above.
