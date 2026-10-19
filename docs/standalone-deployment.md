# Standalone deployment

The chart has two exposure models.

- **Inside a Nebari install**, the `NebariApp` CR (`api.nebariapp.enabled`)
  hands routing, TLS, and browser auth to the Nebari gateway.
- **Standalone**, there is no gateway. `api.ingress.*` publishes the API over
  TLS, and the application protects its own surface through the protection map
  in `security.paths`.

The two are independent: the chart will render both if both are enabled, but a
standalone install normally leaves `api.nebariapp.enabled=false`.

## Exposure mode decides what is enforced

The hardening on this page is **tied to the exposure mode**, because standalone
exposure is new and has no installed base while gateway installs are already
running:

| | gateway install (`api.ingress.enabled=false`) | standalone (`api.ingress.enabled=true`) |
|---|---|---|
| Protection map | not enforced — route dependencies are the only auth, as before | enforced |
| `/` | gateway-authenticated, served by the app to anyone the gateway lets through | requires a verified token |
| `/metrics` | unauthenticated, in-cluster scrapes keep working | requires a verified token (`security.metricsAccess`) |
| CORS | application default `["*"]` | `[]` unless you name origins |
| `X-Forwarded-*` | not trusted (unchanged) | trusted, and you must say by whom |

**Upgrading an existing gateway install changes none of its behavior.** The
Deployment gains environment variables, and every one of them states the
behavior that install already had.

To adopt the hardening on a gateway install, opt in deliberately:

```yaml
security:
  enforce: true
```

Before you do, check that `frames.auth.idToken.jwksUrl` is set. With the map
enforced, the app verifies the IdToken cookie itself rather than trusting that
`enforceAtGateway` already did, and an install without usable JWKS settings
will answer 401 on `/` even for a user the gateway authenticated. Confirm
in-cluster `/metrics` scrapes at the same time — set
`security.metricsAccess: public` if a Prometheus needs them (see below).

`security.enforce: false` opts back out.

### Say where organizations come from

One additional rule applies to standalone exposure: a chart that sets
`frames.auth.defaults.orgId` while `api.ingress.enabled` **fails to render**
unless `frames.auth.orgSource` is set explicitly. That combination puts every
authenticated user in one organization, where an `internal` Frame is readable by
anyone who can sign in — a state worth choosing rather than inheriting. Both
answers render:

- `frames.auth.orgSource: membership` — organizations come from the server's
  `collab_org_members` table, and `frames.auth.defaults` must be cleared.
- `frames.auth.orgSource: single` — membership resolution for a hub that
  declares it hosts exactly one organization: sign-ins from the identity
  providers named in `frames.auth.singleOrg.memberSources` are auto-admitted
  as members, and `frames.auth.defaults` must likewise be cleared.
- `frames.auth.orgSource: claims` — the shared-organization behavior is
  intended and stated.

See [Organization source](frames-operations.md#organization-source-framesauthorgsource)
for what each mode does, what an unaffiliated caller receives, and the
preconditions membership mode enforces at render and at startup.

## Namespace ownership

`namespace.create` (default `true`) decides whether the release owns a
`Namespace` object. Set it to `false` when installing into a namespace that
already exists, or when using `helm install --create-namespace` — otherwise
Helm tries to adopt a namespace the release did not create and the install
fails.

```console
helm install collab-hub helm/collab-hub -n nebari-nexus --set namespace.create=false
```

## Exposure: Ingress or HTTPRoute

`api.ingress.kind` chooses the routing object.

| | `Ingress` (default) | `HTTPRoute` |
|---|---|---|
| API group | `networking.k8s.io/v1` — present in every cluster | `gateway.networking.k8s.io/v1` — needs the Gateway API CRDs |
| Routed by | any ingress controller (ingress-nginx, Traefik, cloud ALB controllers) | a Gateway API controller such as Envoy Gateway |
| TLS | terminated by the controller using `spec.tls`'s Secret | terminated on the parent Gateway's listener |
| Extra values | `className`, `annotations` | `parentRefs` (required) |

`Ingress` is the default because its API is always available: a chart that
defaulted to `HTTPRoute` would fail to install on any cluster without the
Gateway API CRDs. Clusters whose only router is a Gateway (Envoy Gateway serves
no `Ingress` objects) set `kind: HTTPRoute` and list the Gateway in
`parentRefs`.

Neither object authenticates anything — see the protection map below.

### TLS

With `kind: Ingress`, TLS is `spec.tls` naming
`api.ingress.tls.secretName` (default `<release>-nebari-nexus-api-tls`). The
usual way to fill that Secret is a cert-manager annotation on the Ingress:

```yaml
api:
  ingress:
    enabled: true
    kind: Ingress
    host: collab.example.com
    className: nginx
    annotations:
      cert-manager.io/cluster-issuer: letsencrypt-prod
    tls:
      enabled: true
```

`api.ingress.tls.certificate.enabled=true` (plus an `issuerRef`) has the chart
create that cert-manager `Certificate` instead, for the case where no issuer
annotation is in play. Leave it off whenever something else already provisions
the Secret, or the two race for it. It requires the cert-manager CRDs.

**With `kind: HTTPRoute` the chart provides no TLS at all**, and refuses to
render `tls.certificate.enabled`. TLS there is a property of the parent
Gateway's listener, which this chart neither creates nor configures: a
`Certificate` created in the release namespace would have nothing pointing at
it. The route can render perfectly and the host still never serve TLS. The
cluster (not this chart) must provide:

- a Gateway **listener** whose `hostname` matches `api.ingress.host`, with
  `tls.certificateRefs` naming the Secret;
- the certificate issued **into the namespace the Gateway reads from**, which
  is usually the Gateway's namespace, not the release's;
- a **`ReferenceGrant`** if the Gateway and the TLS Secret end up in different
  namespaces. Its absence is the failure mode worth remembering: the listener
  simply never programs, and nothing surfaces on the HTTPRoute — no error, no
  event on the route, just a hostname that does not answer.

For the Collab hub that wiring is tracked in
[an internal issue](an internal issue).

Two things the chart renders but **cannot verify** — check them against the
live controller after deploying:

- **Plaintext HTTP.** `tls.enabled` provisions HTTPS; it does not stop the
  controller from also serving `http://`. ingress-nginx redirects by default
  when a TLS block is present; other controllers need an annotation. Verify
  with `curl -si http://<host>/health` — expect a 301/308 or a refused
  connection, never a 200. Bearer tokens and IdToken cookies travel on these
  requests.
- **Certificate issuance.** A `Certificate` object is a request, not a
  certificate.

## Protection map

Route dependencies protect the API routers, but they cannot protect what they
do not decorate. `/` (the landing page) and `/metrics` had no dependency and
relied on the gateway's `enforceAtGateway`; behind an ordinary ingress that
reliance publishes them.

`security.paths` is a per-path map the application enforces in-process, before
routing, whenever `security.enforce` resolves true (see the exposure-mode table
above). These are the chart defaults:

```yaml
security:
  paths:
    - path: /health
      match: exact
      access: public
    - path: /health/db
      match: exact
      access: public
    - path: /v1/auth/cli
      match: exact
      access: public
    - path: /web
      match: prefix
      access: public
    - path: /invite
      match: prefix
      access: public
    - path: /admin
      match: prefix
      access: public
    - path: /
      match: exact
      access: authenticated
  metricsAccess: authenticated
  defaultAccess: authenticated
```

`metricsAccess` is a separate setting rather than a map entry on purpose: the
chart appends it to the map, so changing the protection of `/metrics` never
means retyping the list — and a retyped list that drops the `/health` entries
would leave the pod failing its own probes. (The chart refuses to render that
combination, but not restating the list is the better habit.)

- `match` is `prefix` (default) or `exact`. Prefix matching is segment-aware:
  `/v1` covers `/v1` and `/v1/frames`, not `/v1beta`.
- `access` is `public` (no credentials) or `authenticated` (a verified IdToken
  cookie or bearer token — the same check the API routes use).
- The most specific rule wins: exact beats prefix, then longest path. Among
  equally specific rules the **last** one listed wins, so an operator can
  append an override rather than restate the map.
- `defaultAccess` applies where no rule matches. Keep it `authenticated`: a
  route that ships without its own auth dependency then fails closed.
- `/v1/auth/cli` is public because the `collab-hub` CLI asks it how to sign in
  before it holds any credential. It names the bearer issuer, the realm client
  the CLI signs in with and whether dev auth is on — nothing the realm's own
  discovery document does not already publish. `GET /v1/me`, which the CLI's
  `whoami` reads, stays behind the default like every other route.
- `/v1/runs/**`, the run API, needs no entry either, and is never made public:
  every run route also carries its own auth dependency, and scopes each run to
  the organization that launched it, another organization's run being a 404. A
  request without credentials is a 401 there under the hardened map
  ([runs.md](cog-execution/runs.md#the-run-controller-and-the-run-api)).
- The API routes under `/v1` need no entry: the default covers them, and each
  also carries its own auth dependency, so a `public` entry does not make
  frames or tasks anonymous. The one exception is the Cog catalog: a `public`
  rule at or below `/v1/cogs` opens anonymous discovery
  ([cog-registry.md](cog-registry.md#read-api)). Under that rule a request
  with no credentials gets the anonymous view. It omits `source_id` and the
  cards' reader diagnostics (`errors`, `warnings`), and refuses the
  `source_id` filter with 422 `validation_error`. So does a signed-in
  subject for whom membership resolution finds no organization (platform
  operators without one are accepted callers and get the full answer).
  Credentials the API accepts get the full answer, and an `Authorization`
  header or `IdToken-*` cookie that is present but empty counts as rejected
  credentials, not as none.
  Credentials it rejects, including a JWKS it cannot reach, get 401 as
  anywhere else. For every caller, `GET /v1/cogs` list items carry a
  trimmed card without `body`, `profile_raw` and `frontmatter_raw`, which
  the per-version routes serve.

### Why `/web` and `/invite` ship public

The server-side web surface ([web-surface.md](web-surface.md)) enforces its own
session, CSRF, and role checks in-route, so its prefixes must be left public at
the map level — and the app **refuses to start** a hardened deployment whose
map does not cover the routes it actually serves. They are in the defaults
above rather than left as a values change because the check that refuses is
shipped in the same release: a hardened install that enabled the web surface
would otherwise fail at startup on defaults it never edited.

"Public" here does not mean unauthenticated. This map's `authenticated` level
runs the **API** credential check (an IdToken cookie or bearer token) before
routing, which a browser in the middle of signing in cannot pass — so a map
that authenticates `/web` would 401 the sign-in flow itself, on every request,
for every operator. `/invite` is the same for the acceptance page, whose whole
audience is people with no account on this deployment yet.

Both entries are **inert on an install that does not enable the web surface**.
No routes are registered under either prefix, so a request there matches
nothing in the app and falls through to the MCP catch-all mounted at `/`, which
runs its own `McpAuthMiddleware` and authenticates on the API axis before the
sub-application sees it. A prefix declared public that serves nothing is not a
hole. Unhardened installs never see these entries at all: the chart passes
`PATHS="[]"` and `DEFAULT_ACCESS="public"` when `security.enforce` resolves
false.

`/admin` was listed ahead of its routes for merge ordering: the operator page
(nebari-dev/collab-hub-pack#91) added `/admin` routes without a values change,
so with the entry on neither side a hardened install would have refused to
start between the two merges. `/admin` now serves the admin panel (`/admin/`,
`/admin/assets/*` and the JSON API under `/admin/api/*`) on an install that
enables the web surface and resolves organizations from membership
(`frames.auth.orgSource` `membership` or `single`), plus the operator
invitation page when `orgSource` is `membership`. These routes authenticate
themselves with the web session, the CSRF token and the operator check. On
any other install `/admin` has no routes and is inert, like the two entries
above. See [Configuring the admin panel](#configuring-the-admin-panel).

`/org` stays **absent**: it is routeless with no change adding routes to it, so
there is no merge for an early entry to be early for. It arrives with #92.

Because the map is data, a new public page remains a values change for anything
the chart does not ship.

Role-scoped levels (operator-only `/admin`, owner-only `/org`) are deliberately
**not** accepted by the map: role checks are enforced in-route by the web
surface's dependencies (`require_operator`, `require_org_owner` — see
[web-surface.md](web-surface.md)), and a map that accepted `access: operator`
would claim a protection this middleware does not itself apply.

### Scraping /metrics

On a gateway install nothing changes: `/metrics` stays unauthenticated and
in-cluster scrapes keep working. Under enforcement it answers 401 unless the
scraper carries a credential `get_auth_context` accepts. There is no
scrape-token mode yet — that is
[nebari-dev/collab-hub-pack#97](https://github.com/nebari-dev/collab-hub-pack/issues/97).

To let an in-cluster Prometheus scrape a hardened deployment, open the one
path and keep it off the internet:

```yaml
security:
  metricsAccess: public   # one line; the rest of the map is untouched
api:
  ingress:
    paths:                # and do not route /metrics from outside
      - path: /v1
        pathType: Prefix
```

The chart refuses to render an unauthenticated `/metrics` that
`api.ingress.paths` also publishes — including when `security.enforce` is
false while the ingress routes everything.

`api.ingress.paths` is an exposure list — which paths the routing object sends
to the Service. It is not an auth policy; `security.paths` is.

### Relationship to the NebariApp

For gateway installs the two lists answer different questions and are kept
separate on purpose:

- `api.nebariapp.routing.publicRoutes` — paths the **gateway** does not
  intercept, so native clients can present bearer tokens to the app.
- `security.paths` — what the **app** requires on each path.

A path in `publicRoutes` is not public: it is app-enforced. Keep
`publicRoutes` ⊆ the paths your map marks `authenticated`, plus whatever is
genuinely `public`.

## CORS, proxy headers, root path

```yaml
security:
  cors:
    allowedOrigins: null       # null: [] under enforcement, ["*"] otherwise
    allowedHeaders: ["Authorization", "Content-Type"]
    allowCredentials: false
server:
  proxyHeaders: null           # null: follows api.ingress.enabled
  forwardedAllowIps: []        # required when proxy headers are on
  trustAnyProxy: false
  rootPath: ""
```

- **CORS.** `allowedOrigins: null` means the chart passes nothing and the
  application keeps its `["*"]` default — which is what every install running
  today has, so an upgrade takes no browser caller away. Under enforcement the
  chart passes `[]` instead: native clients (the desktop app) send no `Origin`
  and are unaffected, the server's own pages are same-origin, so a wildcard
  buys nothing on a multi-tenant hub while handing every site on the internet a
  cross-origin call path. Set an explicit list to name origins in either mode.
  `"*"` with `allowCredentials: true` is rejected by both the chart and the app
  in *every* mode: Starlette echoes the caller's `Origin` in that combination,
  which lets any site make credentialed calls.
- **`proxyHeaders`.** `null` follows `api.ingress.enabled`: off for gateway
  installs, which is what they run today, and on for ingress exposure, where
  the app would otherwise record the proxy's address and `http` as the scheme.
  When it is on, the chart requires you to say which peers are trusted:
  `forwardedAllowIps` naming the ingress controller's address or CIDR, or
  `trustAnyProxy: true`. What the wildcard costs, plainly: the chart creates no
  `NetworkPolicy`, so "only the proxy can reach the pod" is an assumption, not
  a guarantee — any workload in the cluster can dial a ClusterIP Service, and
  with `"*"` any of them can choose the client address and the scheme this app
  records in its logs and audit trail.
- **`rootPath`** is for serving the app under a URL prefix. The protection map
  is matched against paths with the prefix stripped, so rules stay written
  against the app's own paths.

These were previously reachable only by smuggling `extraEnv` entries.
`api.deployment.extraEnv` still renders last and therefore still wins, so an
existing install that set them that way keeps its values — **except while
protection is enforced**, where the values above are the only way to say it.

### `extraEnv` may not restate the enforced settings

Kubernetes permits duplicate `env` names and the container resolves the last
one, so an `extraEnv` entry naming a variable the chart already renders would
quietly replace it. While protection is enforced that would reopen `/` and
`/metrics` on the routed host with every check above still reporting
enforcement, so the chart refuses the render instead:

```
api.deployment.extraEnv sets COLLAB_HUB_API__SECURITY__PATHS, which the chart
renders itself while protection is enforced; ...
```

Refused under enforcement: `COLLAB_HUB_API__SECURITY` and anything beneath it
(`__PATHS`, `__DEFAULT_ACCESS`, `__CORS__*`), plus `COLLAB_HUB_API__SERVER`,
`…__SERVER__PROXY_HEADERS` and `…__SERVER__FORWARDED_ALLOW_IPS`. Names match
case-insensitively, and the bare roots are refused too, because
pydantic-settings ignores env-name case and reads `COLLAB_HUB_API__SECURITY`
as JSON for the whole nested model. Unrelated variables — including
`…__SERVER__ROOT_PATH`, `…__SERVER__HOSTNAME` and `…__SERVER__PORT` — are
untouched, and an install that has not opted into enforcement keeps every
override it has today.

### `extraEnv` may not carry an unsafe-auth switch on a routed host

`FRAMES_UNSAFE_AUTH_ENABLED`, `FRAMES_IDTOKEN_ALLOW_UNSIGNED`,
`FRAMES_BEARER_ALLOW_UNSIGNED` and `DEV_AUTH_ENABLED` make the app accept
unsigned or dev-issued tokens. On an externally routed host that is an
authentication bypass, not a dev convenience, so `api.ingress.enabled: true`
refuses them two ways:

- **Literal `true`** — decidable at render time, and exhaustively so: the app
  compares against exactly that string, so no other literal turns the switch on.
  The literal `false` still renders, since it is provably the safe value.
- **Any `valueFrom`** — refused on presence alone. The Secret or ConfigMap
  resolves in the kubelet, long after the template runs, so the render cannot
  see whether it says `true`. Drop the entry instead; not setting the variable
  at all is the safe state, so this costs a routed install nothing.

Gateway-only installs are unaffected in both cases — that is where the dev and
smoke-test workflows that need these switches actually run.

## Configuring the admin panel

The admin panel at `/admin` ships in the image: the Docker build copies the
built bundle to `/var/collab-hub-api/admin-ui-dist` and sets
`COLLAB_HUB_API__WEB__ADMIN_UI_DIST` to that path. It mounts when both of
these hold:

- the web surface is enabled (see [web-surface.md](web-surface.md#deploying-it));
- `frames.auth.orgSource` is `membership` or `single`.

Role changes, connector switches, model access and the audit log also need the
shared `frames.postgres` URL, and the Users section needs `userDirectory`
enabled, since it lists people from Keycloak. An endpoint whose dependency is
missing answers 503 with an `*_unavailable` error, and the rest of the panel
keeps working.

The chart has no values for the panel's own settings, so they are set through
`api.deployment.extraEnv`, which the chart renders verbatim after its own
environment block and which therefore accepts `valueFrom`. The names follow the
API's settings: prefix `COLLAB_HUB_API__`, with `__` between nested keys.
`group_ids` and `model_groups` are maps, and pydantic-settings reads a map from
one variable holding a JSON object.

```yaml
api:
  deployment:
    extraEnv:
      # Members of this group hold the operator role (sign-in sync).
      - name: COLLAB_HUB_API__WEB__ADMIN_GROUP
        value: /collab-admins
      # The serving layer's origin; the panel reads <origin>/v1/models.
      - name: COLLAB_HUB_API__FRAMES__MODEL_ACCESS__CATALOG_BASE_URL
        value: https://models.example.com
      # The model-access client: a third confidential client in the realm.
      - name: COLLAB_HUB_API__FRAMES__MODEL_ACCESS__KEYCLOAK__TOKEN_URL
        value: https://keycloak.example.com/realms/nebari/protocol/openid-connect/token
      - name: COLLAB_HUB_API__FRAMES__MODEL_ACCESS__KEYCLOAK__ADMIN_API_BASE_URL
        value: https://keycloak.example.com/admin/realms/nebari
      - name: COLLAB_HUB_API__FRAMES__MODEL_ACCESS__KEYCLOAK__CLIENT_ID
        value: collab-model-access
      - name: COLLAB_HUB_API__FRAMES__MODEL_ACCESS__KEYCLOAK__CLIENT_SECRET
        valueFrom:
          secretKeyRef:
            name: collab-model-access
            key: client-secret
      # Group path to Keycloak group id. Only these groups can be changed.
      - name: COLLAB_HUB_API__FRAMES__MODEL_ACCESS__GROUP_IDS
        value: '{"/models/example-model": "0b4c1f6e-0000-4000-8000-000000000001"}'
      # Model id to the group path that gates it.
      - name: COLLAB_HUB_API__FRAMES__MODEL_ACCESS__MODEL_GROUPS
        value: '{"example-model": "/models/example-model"}'
```

`COLLAB_HUB_API__WEB__ADMIN_GROUP` works only when the ID token carries a
`groups` claim. Add a group membership mapper (or the `groups` client scope)
to the web surface's client in Keycloak. A missing claim changes nobody's
role, so without the mapper the setting does nothing.

The model-access client's service account must hold `Groups/view-members`,
`Groups/manage-membership` and `Users/manage-group-membership`, and must not
hold `Groups/manage-members`. Keep it separate from the `userDirectory` and
`frames.serviceAccess` clients. The reasons, and what each change records, are
in [frames-operations.md](frames-operations.md#model-access-from-the-admin-panel).

## Identity and data-store separation (register R16/R18)

The chart provides the knobs; the values that satisfy the requirements live in
the deployment repository, and the verification is a deployment test there, not
a chart test:

- **Separate data stores (R16).** An external deployment must not share the
  internal hub's Postgres database or S3 bucket. Set `frames.postgres.url` (or
  `existingSecret`) to its own database and `frames.s3.bucket` to its own
  bucket; if a bucket genuinely must be shared, give it a distinct
  `frames.s3.prefix`. The chart cannot tell one deployment's URL from
  another's — assert the separation where both deployments' values are visible.
- **Keycloak conventions (R18).** The desktop client expects
  `keycloak.<hub-host>`, realm `nebari`, and client `apollo-desktop`. In the
  chart those appear only as `frames.auth.bearer.*` / `frames.auth.idToken.*`
  URLs and as `userDirectory.keycloak.*`; whether the issuer behind them
  actually follows the convention is a property of the live identity provider.
  Confirm it there, or record the deviation, before exposing a host. The web
  surface adds a second, confidential Keycloak client for the browser
  authorization-code flow in the same realm as `apollo-desktop`, and the admin
  panel's model access adds a third: a confidential service-account client
  that lists and changes model-group membership
  (`frames.model_access.keycloak.*`, see
  [Configuring the admin panel](#configuring-the-admin-panel)). The
  [`collab-hub` CLI](../cli/README.md) adds no client: it signs in with
  `apollo-desktop` and the desktop's flow — authorization code with PKCE and a
  `http://127.0.0.1` loopback redirect — so the client must keep allowing
  `http://127.0.0.1:*/*` as a redirect URI. `GET /v1/auth/cli` names it to the
  CLI; `frames.auth.cliClientId` overrides it for a realm whose desktop client
  has another id.

Any path marked `authenticated` needs a verifiable token: set
`frames.auth.idToken.jwksUrl` (browsers) and `frames.auth.bearer.jwksUrl`
(native clients). Without them the app cannot verify anything and every
protected path answers 401.

Both JWKS URLs **must be `https://`**. Signing keys fetched over cleartext
`http` can be substituted by an on-path attacker, who can then mint tokens the
deployment accepts, so the chart's values schema rejects an `http` URL and the
API refuses to start on one (the failure is in the pod's events, not a stream
of 401s). URLs carrying userinfo (`user:pass@host`) or a fragment are refused
too. The only exception is local development with
`FRAMES_UNSAFE_AUTH_ENABLED=true`, where `http` is permitted and logged
loudly. Internal or cluster-local hostnames are fine — the rule is about the
scheme, not the host.
