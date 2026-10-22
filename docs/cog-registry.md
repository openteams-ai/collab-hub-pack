# Cog registry

The hub indexes Cogs from OCI registries. This page covers the `cogs:` block
of the Helm chart and the matching application settings: which registries are
read (**sources**), how the hub authenticates to them (**credentials**), how a
private CA is trusted (**CA bundle**), how often the index is rebuilt
(**indexer**), whether the hub serves pulls itself
([**pulls through the Hub**](#pulls-through-the-hub)), and whether it accepts
publishes ([**publishing through the Hub**](#publishing-through-the-hub)). The adapters themselves — what "Harbor" and "static" mean and
why the registry stays swappable — are documented in
`api/src/collab_hub_api/cogs/registry.py`; the configuration surface is
[#87](https://github.com/openteams-ai/collab-hub-pack/issues/87).

> **Status.** This page describes the configuration surface, the chart
> wiring, and the [read API](#read-api) clients discover Cogs through. The
> indexer sweep ([#84]) consumes the block at runtime and the read API
> ([#85]) serves what it indexed; the webhook receiver ([#86]) lands
> separately. The API validates the block at startup and refuses to start on
> the errors described below.

[#84]: https://github.com/openteams-ai/collab-hub-pack/issues/84
[#85]: https://github.com/openteams-ai/collab-hub-pack/issues/85
[#86]: https://github.com/openteams-ai/collab-hub-pack/issues/86
[#148]: https://github.com/openteams-ai/collab-hub-pack/issues/148

Two rules shape everything below:

- **The chart and the values must land together.** `values.schema.json` has
  `additionalProperties: false` everywhere, so a values file that names a
  `cogs` key the installed chart does not know fails at `helm upgrade`, and a
  chart that expects one the values do not set fails at startup. Upgrade the
  chart and the values in one release.
- **Secrets are never rendered.** The source list reaches the API as one JSON
  environment variable; passwords and webhook secrets do not. They are mounted
  from Kubernetes Secrets you create, under variable names the chart derives
  from the source id, and the API reads those variables at startup.

## Sources

`cogs.registry.sources` is an ordered list. Each entry has an `id`, a `kind`,
and the external `url`; the rest depends on the kind.

| Key | Applies to | Meaning |
| --- | --- | --- |
| `id` | all | Stable identifier stored with every indexed row. Lowercase `[a-z0-9._-]`, at most 64 characters. Renaming it orphans the rows indexed under the old name. |
| `kind` | all | `harbor` or `static`. |
| `url` | all | External registry URL. Its host becomes the identity `<host>/<repo>@<digest>`; never an in-cluster address. |
| `apiUrl` | harbor | In-cluster URL for Harbor's REST API and OCI endpoints, so sweeps stay inside the cluster. Identity still derives from `url`. |
| `tokenUrl` | all | In-cluster bearer-token endpoint for the OCI client, when the registry's `WWW-Authenticate` realm points at an external host the pod cannot reach. |
| `projects` | harbor | Harbor projects to enumerate. At least one. |
| `repositories` | static | OCI repository paths to index (`project/name`). |
| `indexUrl` | static | URL of a `catalog.v1.json` listing repositories. A static source needs `repositories`, `indexUrl`, or both, unless it is the `publish` source, whose repositories are the ones published through the Hub. |
| `caBundlePath` | all | Per-source CA bundle path inside the pod. Defaults to the shared bundle below when that is configured. |
| `requestTimeoutSeconds` | all | HTTP timeout, default 10, at most 60. |
| `publish` | all | `true` on exactly one source: pushes through the Hub are written to it. See [publishing](#publishing-through-the-hub). |
| `blobRedirectHosts` | all | Hosts a blob redirect from this registry may point at (its object storage): exact names or IPv4 addresses, or leading-dot suffixes such as `.s3.amazonaws.com`. Empty means no allowlist. Setting it turns the [redirect rules](#redirects) on for this source. |
| `credentials` | all | Where the robot/service credential lives — see the next section. Omit for anonymous access. |
| `webhook` | harbor | Where the shared webhook secret lives. A static source has no webhook and the render refuses the block. |

Field mismatches are refused rather than ignored: `repositories` on a Harbor
source, or `projects` on a static one, almost always means the kind is
mistyped, and the source would otherwise start and index nothing. The chart
fails the render (`templates/cogs-validations.yaml`) and the API fails
startup on the same rules, so a values mistake surfaces at `helm template`
rather than as a crash-looping pod.

## Credentials

Each source may name a Secret for its credential and, for Harbor, another for
its webhook:

```yaml
credentials:
  existingSecret: collab-hub-harbor-robot   # required when the block is present
  usernameKey: username                     # default
  passwordKey: password                     # default
webhook:
  existingSecret: collab-hub-harbor-webhook
  secretKey: secret                         # default
```

The chart mounts those keys as environment variables named from the source id:

```
COLLAB_HUB_COGS_SOURCE_<ID>_USERNAME
COLLAB_HUB_COGS_SOURCE_<ID>_PASSWORD
COLLAB_HUB_COGS_SOURCE_<ID>_WEBHOOK_SECRET
```

`<ID>` is the id upper-cased with every character outside `[A-Z0-9]` replaced
by `_`: `harbor-main` becomes `HARBOR_MAIN`, `public.mirror` becomes
`PUBLIC_MIRROR`. Two ids that collapse to the same `<ID>` fail the render. The
JSON source list carries only these *names* (`credentials.username_env`,
`credentials.password_env`, `webhook_secret_env`); at startup the API replaces
each name with the variable's value and then validates the source as if the
value had been given directly. A name whose variable is unset or empty — the
Secret is missing, or its key is spelled differently from `passwordKey` — is a
startup error that names the variable, so the fix is a values change, not a
hunt through 401s on the first sweep. A username without a password (or the
reverse) is refused for the same reason: it is the shape a half-mounted
Secret produces.

Surrounding whitespace is removed from every resolved value, on purpose: a
Secret created with `--from-file` carries the file's trailing newline, and
forwarding it verbatim fails authentication at the registry with an error
that names nothing useful. Inline values are treated the same way, so both
routes yield the same bytes. A whitespace-only value counts as empty. A
credential whose surrounding whitespace is significant is not supported.

Why indirection rather than an indexed override such as
`COLLAB_HUB_API__COGS__REGISTRY_SOURCES__0__CREDENTIALS__PASSWORD`: the
pydantic-settings release the API pins does not layer index-style variables
over a list that arrived as JSON — the override is silently ignored, and a
silently ignored credential is the one failure mode this design must not
have. `api/tests/test_config_cogs.py` pins that measurement so a future
library version that starts honoring it is noticed.

Outside the chart (a bare process, docker-compose), the same mechanism works
with any variable names: put `"password_env": "MY_VAR"` in the JSON and set
`MY_VAR`.

## CA bundle

Dev clusters front the registry with a certificate from a private CA. Put the
CA certificate in a ConfigMap and name it:

```yaml
cogs:
  caBundle:
    configMap: collab-hub-cogs-ca
    key: ca.crt        # default
```

The chart mounts it read-only at `/etc/collab-hub/cogs-ca/<key>` (the API
container runs with a read-only root filesystem, so this is a volume, not a
file written at startup) and passes that path as `ca_bundle_path` to every
source that does not set its own `caBundlePath`. Leave `configMap` empty to
use system trust only.

## Indexer

```yaml
cogs:
  index:
    enabled: false      # sweep the sources on a schedule
    intervalSeconds: 300
    runOnStartup: true
  indexer:
    resources: {}       # empty = api.deployment.resources
```

`enabled` decides whether the chart renders the **indexer workload**
([#148]): a Deployment of its own, `<release>-indexer`, running the API
image with the API's settings and `cogs.index.enabled=true` — one replica, a
`Recreate` strategy (a rollout never runs two sweepers side by side), no
Service, no ingress, and an `emptyDir` in place of the API's frames-storage
claim. That is the one process that sweeps (#84). The API replicas always
receive `cogs.index.enabled=false`, whatever the values say, and go on
serving the read API (#85); a rolling update of the API therefore never
starts a sweep. `enabled` gates the sweep loop and nothing else: every
process with sources and the catalog database builds the indexer, so the
API replicas hold its lock-less targeted entry points, which the webhook
receiver (#86) will call. Single flight is a property of this shape; the
sweep's advisory lock in the database is only a belt under it (see
[frames-operations.md](frames-operations.md#the-cog-catalog-collab_cog_artifacts)).
The replica count is fixed at `1` in the template and is not a value. Pause
sweeping with `enabled: false`.

Sources may be configured while the indexer is off — and they are still
rendered and validated: the source JSON, the Secret-backed env vars and the
CA mount are emitted whenever `registry.sources` is non-empty, regardless of
`enabled`, and the API still resolves every named Secret at startup. Only
the indexer Deployment, with `intervalSeconds` and `runOnStartup`, is
suppressed when `enabled` is false. `enabled: true` with zero sources is
refused at render and at startup — an indexer with nothing to index is a
misconfiguration, not an idle worker. The interval is bounded to 10 s..24 h
because a sweep lists every repository of every source.

With no sources and the indexer off, the chart renders only the `enabled`
flag (`false`) on the API, so a deployment without Cogs carries no other
`cogs` environment.

## Read API

`api/src/collab_hub_api/routers/cogs.py` serves the catalog the indexer
fills. It is read-only and answers from what was captured at index time: it
never contacts a registry, and an install goes to whatever host the pinned
reference names. By default that is the backing registry; with
[`cogs.serve.enabled`](#pulls-through-the-hub) it is the Hub itself. The catalog is
registry-agnostic — the answers are the same whichever adapter indexed a row.

| Route | Answers |
| --- | --- |
| `GET /v1/cogs` | Current Cogs, one entry per `cog_id`: its newest present version, with a **trimmed** card (below). Filtered and paged (below). |
| `GET /v1/cogs/{cog_id}` | The Cog's current entry, with its full card, plus `versions`: every indexed location of every version (digest, version, tags, `pushed_at`, `indexed_at`, `source_id`, repository, reference, `removed_at`), newest first, removed ones included. |
| `GET /v1/cogs/{cog_id}/versions/{digest}` | The exact, full card indexed for that digest — what an install pins. |
| `GET /v1/cogs/{cog_id}/versions/{digest}/cog.md` | The Markdown body of the version's `COG.md` (after its frontmatter), `text/markdown`, as captured at index time. The frontmatter is on the card (`frontmatter`, `frontmatter_raw`). |
| `GET /v1/cogs/{cog_id}/versions/{digest}/reference` | The **install reference** `{"reference": "<host>/<repository>@sha256:…", "source_id", "repository", "digest", "present", "locations"}` — ready for `nebi import`. |
| `GET /v1/cogs/catalog.v1.json` | **Transitional** compatibility view, below. |

Every entry carries the digest, the location (`source_id`, `repository`,
`reference`), `tags`, `pushed_at`, `indexed_at`, `removed_at` and the `card`
— the bundle reader's output, served verbatim (trimmed on list items, and
redacted for anonymous callers; both below). The OpenAPI document (`/docs`)
lists and describes every top-level key the reader emits but types none of
them: the card is the Cog's own declarations, not a hub schema, and a
publisher's odd value (a numeric `id`, say) is served as declared rather than
failing the response. Clients must tolerate unexpected shapes.

**List items carry a trimmed card.** A `GET /v1/cogs` item's card is the
stored card without `body`, `profile_raw` and `frontmatter_raw` — the
verbatim documents, up to three per item — and is a separate OpenAPI schema
(`CogListEntry` / `CogListCard`) so a client cannot mistake it for a full
card. Everything else, including keys a newer reader adds, is kept. The full
card is at `GET /v1/cogs/{cog_id}` and `…/versions/{digest}`, and the body
also at `…/cog.md`. The omitted set is `LIST_CARD_OMITTED_KEYS` in
`cogs/models.py`; adding a key back later is compatible, removing one would
not be.

**Identity is the digest.** `cog_id` (`<publisher>/<name>`, so it contains a
`/` — send it as is or percent-encoded) and `name` are search keys. "Newest"
is by `pushed_at` (unknown last), then `indexed_at`.

**Removed artifacts** are hidden from `GET /v1/cogs` and from the current
entry of `GET /v1/cogs/{cog_id}`, but every `…/versions/{digest}` route still
answers for them with `removed_at` set: installs and runs pin digests, and
the row outlives the artifact. A Cog whose every version was removed is a 404
at `GET /v1/cogs/{cog_id}` and stays reachable by digest. Non-Cog artifacts,
failed reads and cards without an `id` never appear.

**Filters** (`GET /v1/cogs`, all optional, combined with AND):

| Parameter | Matches |
| --- | --- |
| `kind` | the card's `kind`, exactly |
| `publisher` | the card's `publisher`, exactly |
| `provides` | an entry of the card's `provides` |
| `requires` | a capability named in the card's `requires` |
| `accepts` / `produces` | an io type in the card's `io.accepts` / `io.produces` |
| `q` | a case-insensitive substring of the Cog's `name`, or of its `description` when that is a string (`%` and `_` are literal; case folding is guaranteed for ASCII, locale-dependent beyond it) |
| `source_id` | only this registry source (refused for anonymous callers, below) |

`source_id` scopes the choice: the newest version is picked among that
source's rows. Every other filter tests the Cog's **current** version, never
an older one — a Cog that dropped a capability in its latest release does not
list under it — so without `source_id` a listed entry is always the version
`GET /v1/cogs/{cog_id}` serves. With `source_id` it is the newest version *in
that source*, which may be older than the one the detail route shows. The card filters are `jsonb` containment over the stored card, which
the GIN index on `card` serves.

**Paging.** `limit` (default 50, at most 200) and `offset` (default 0), as
the task routes page. The page is `{"items": [...], "limit", "offset",
"next_offset"}`, ordered by `cog_id` in code-point order; `next_offset` is
the `offset` of the next page, or `null` on the last one.

**Errors** use the API envelope `{"error": {"code", "message"}}`:
`cog_not_found` and `cog_version_not_found` (404 — including a digest that is
indexed, but as another Cog's), `cog_catalog_unavailable` (503 — neither
Postgres nor the development memory backend is configured; an empty catalog
is never invented), `validation_error` (422 —
an out-of-range parameter, a digest that is not `sha256:` followed by 64
lowercase hex digits — a well-formed digest the catalog does not hold is a
404 — or a `source_id` filter from an anonymous caller), and `unauthorized`
(401).

**Auth.** Every route requires an authenticated caller by default, like the
frames routes, and `/v1/cogs` is an API prefix of the path-protection map, so
a refusal under the hardened map keeps the envelope. **Anonymous discovery is
a `security.paths` entry**, not a code change:

```yaml
security:
  paths:
    - path: /v1/cogs
      match: prefix
      access: public
```

The routes admit a request without credentials when the rule deciding its
path is `public` and sits at or below `/v1/cogs` — so an `exact` rule can
open just `/v1/cogs/catalog.v1.json`. A broader public rule (`/` or `/v1`
prefix) does not open the catalog, and neither does `defaultAccess: public`
on its own (the unconfigured default), so nothing becomes anonymous by
accident. These are the only API routes that honor a `public` entry this
way; every other `/v1` route requires a caller whatever the map says.

Under such a rule credentials are optional, not ignored. What a request
gets depends on what it presents:

| The request | Answer |
| --- | --- |
| No credentials: no `IdToken-*` cookie and no `Authorization` header | The **anonymous view** (below) |
| Credentials the API accepts, the same check the frames routes use | The full answer. That includes a platform operator with no organization, whom the check accepts. |
| Credentials the API rejects: malformed, expired, an unsupported scheme, or a verifier that cannot run (no JWKS configured, JWKS unreachable) | 401 `unauthorized`, as on any other route; never the anonymous view |
| A valid subject with no organization, where the check answers 403 `no_organization` | The anonymous view: a public page needs no organization |

Any other failure, an unavailable organization store for instance,
propagates as it would elsewhere. The anonymous view leaves out what
discovery does not need:

- `source_id`, everywhere it appears: on list items, the detail entry and
  its `versions`, the version entry, and the reference and its `locations`.
  Source ids name the hub's internal registry configuration.
- the card's reader diagnostics, `errors` and `warnings`, on list and full
  cards alike.

The pinned `reference` (`<host>/<repository>@<digest>`) and `repository`
stay: a client must know where to pull from. When the Hub serves pulls the
host in it is the Hub's own, so an anonymous answer names no backing
registry at all. An anonymous `source_id` filter
is refused with 422 `validation_error` rather than answered, since filtering
by a value the caller cannot see would let it probe for source ids. The
refusal is decided on the raw query string, before any other parameter is
validated, so an empty or over-long value is refused the same way and the
value is never echoed in the error details.
`catalog.v1.json` carries neither and is the same for every caller. The
OpenAPI schemas mark these fields "omitted for anonymous callers" and do not
list `source_id` as required.

### `catalog.v1.json` (transitional)

`GET /v1/cogs/catalog.v1.json` renders the index in the shape of the static
`catalog.v1.json` that clients read before the hub had a catalog, so a client
can switch its catalog URL before it grows new logic:

```json
{"schemaVersion": 1, "repositories": [
  {"namespace": "cogs", "name": "cog-audio-transcriber-1a2b", "description": "…"}
]}
```

One entry per repository path holding a present, indexed Cog: `namespace` is
the path's first segment, `name` the rest, `description` the newest
version's card description (empty when it has none). Entries are sorted by
path and deduplicated across sources — the shape carries no registry host
and no digest, so the same path in two sources is one entry, and a client
reading it still resolves repositories against the registry it already knows.
A single-segment repository path has no namespace and is left out. The hub's
own `static` source parses this document, so a hub can index another hub's
view.

It is **transitional**: it exists for the switch-over and will be removed
once clients read `GET /v1/cogs`. Build nothing new on it.

### Level 1

`make api` keeps the catalog in process memory
(`COLLAB_HUB_API__COGS__CATALOG__BACKEND=memory`, a development override
with no chart value), so the read API answers 200 with an empty catalog at
level 1; with a `static` source and `cogs.index.enabled` the in-memory
catalog can be filled by a real sweep. Every other level, and every
deployment, keeps the catalog in the shared `frames.postgres`. See
[`dev/README.md`](../dev/README.md#level-1--the-api-alone).

## Pulls through the Hub

Off by default. With `cogs.serve.enabled` the Hub serves the
[OCI Distribution](https://github.com/opencontainers/distribution-spec) read
API on its own host, in front of the configured sources, so installing a Cog
needs a Hub sign-in and nothing else: no client holds a credential for a
backing registry, no install record names one, and the registry behind the
Hub can be replaced without a client noticing.

```yaml
cogs:
  serve:
    enabled: false
    publicUrl: ""               # derived from the API's host; see below
    credentialTtlSeconds: 900   # 60..86400
    tokenTtlSeconds: 300        # 30..3600
    maxBlobBytes: 1073741824    # 1 GiB
    maxBlobSeconds: 900         # 10..21600
    routeTimeout: true          # HTTPRoute or NebariApp public route
```

| Chart value | Setting (`COLLAB_HUB_API__COGS__SERVE__…`) | Meaning |
| --- | --- | --- |
| `enabled` | `ENABLED` | Mount `/v2/`, enable the credential exchange, and point catalog references at the Hub. Needs at least one source and a catalog store. |
| `publicUrl` | `PUBLIC_URL` | The Hub's external origin, `https://host[:port]`: no path, no trailing slash. |
| `credentialTtlSeconds` | `CREDENTIAL_TTL_SECONDS` | Lifetime of a registry credential. |
| `tokenTtlSeconds` | `TOKEN_TTL_SECONDS` | Lifetime of a pull token. |
| `maxBlobBytes` | `MAX_BLOB_BYTES` | Largest blob the Hub relays. |
| `maxBlobSeconds` | `MAX_BLOB_SECONDS` | Wall-clock limit on one blob response. |
| `routeTimeout` | (chart only) | Give the route carrying `/v2` a matching request timeout: the HTTPRoute's own rule, or a `BackendTrafficPolicy` on the NebariApp's public route. |

**Off changes nothing.** No route exists under `/v2/`, the exchange answers
404 `cog_registry_not_served`, and every `reference` names the backing
registry exactly as before, with no new keys in any answer. Clients released
before this existed pull from the backing registry and keep working across a
Hub upgrade; turn serving on once the clients that use it are out. The chart
renders no `serve` variable at all while it is off.

### One origin

The registry is **the Hub API's own origin**, not a hostname of its own. The
`registry` of an exchanged credential, the host of every catalog `reference`
and the host `/v2/` is served on are one value: the authority (`host[:port]`,
lowercase, the scheme's default port left out) of `publicUrl`. Clients
depend on it. Collab Desktop accepts a registry credential only when its
`registry` is exactly the authority of the origin it sent the exchange to,
and treats anything else as a Hub that does not serve installs.

So `publicUrl` is not a place to name a registry host. Left empty, the chart
derives `https://<host>` from `api.nebariapp.hostname`, else from
`api.ingress.host`. Set it only to add a port or to say `http` (no path and
no trailing slash; the chart and the API refuse the same values); a value
naming a different host than the one the chart routes fails the render, and
the API refuses to start when it disagrees with `web.public_base_url`. It is
configuration rather than the request's `Host` header because a value a
caller can influence has no business in a credential, a challenge or a
reference. A bare process must set it.

`/v2/` must sit at the **root** of that host: registry clients build
`https://<host>/v2/…` and have no notion of a path prefix. A Hub mounted
under `server.rootPath` needs `/v2` routed to it unprefixed.

### What a client does

```
GET  /v2/                                   401  WWW-Authenticate: Bearer realm="https://<hub>/v2/token",service="<hub>"
POST /v1/cogs/registry-credentials          201  {id, registry, username, secret, scope, expires_at}   (Hub sign-in)
GET  /v2/token?service=<hub>&scope=repository:<name>:pull     (Basic username:secret)
                                            200  {token, access_token, expires_in, issued_at}
GET  /v2/<name>/manifests/<tag|digest>      200  (Bearer token)
GET  /v2/<name>/blobs/<digest>              200
```

The middle three are what `oras`, `docker` and any other registry client do
by themselves once they hold the username and secret, so the only step a
client adds is the exchange:

```sh
oras login hub.example.com -u "$USERNAME" --password-stdin <<<"$SECRET"
oras pull hub.example.com/cogs/cog-audio-transcriber@sha256:…
```

| Route | Answers |
| --- | --- |
| `GET`/`HEAD /v2/` | `200 {}` with a pull token; the bearer challenge without one. |
| `GET`/`HEAD /v2/<name>/manifests/<reference>` | The manifest, byte for byte, by tag or digest, with `Docker-Content-Digest` and its own `Content-Type`. |
| `GET`/`HEAD /v2/<name>/blobs/<digest>` | The blob, streamed. `HEAD` answers the size from the manifest that references it. |
| `GET /v2/<name>/tags/list` | `{"name", "tags"}`, sorted. A page is at most 1000 tags (also the default without `n`); `last` continues after a tag, and a `Link: …; rel="next"` is sent while more remain. |
| `GET /v2/token` | The distribution token endpoint. |
| any other method under `/v2/` | The [push API](#publishing-through-the-hub) when a source is marked `publish`; otherwise 405 `UNSUPPORTED` ("this registry is read-only"), before authentication. Deleting a manifest or a blob is always 405. |

Errors are the registry format, `{"errors": [{"code", "message", "detail"}]}`:
`UNAUTHORIZED` (401), `DENIED` (403: the account has no organization, or a
blob is over `maxBlobBytes`, or the token's owner has lost access), `NAME_UNKNOWN`, `MANIFEST_UNKNOWN`,
`BLOB_UNKNOWN` (404), `UNSUPPORTED` (405), `UNAVAILABLE` (503: the source or
the database could not answer; retry).

### Registry credentials

A client that stores a registry login never stores its Hub access or
refresh token there. It exchanges its session for a **registry credential**:

| Route | Answers |
| --- | --- |
| `POST /v1/cogs/registry-credentials` | 201 `{"id", "registry", "username", "secret", "scope", "expires_at"}`. Optional body `{"scope": "pull"}` (the default) or, on a Hub that accepts publishes, `{"scope": "publish"}` ([publishing](#publishing-through-the-hub)); any other scope is 422, and so is `publish` on a Hub that accepts none. |
| `DELETE /v1/cogs/registry-credentials/{id}` | 204. 404 `cog_registry_credential_not_found` for an id that is unknown, expired or someone else's. |
| `DELETE /v1/cogs/registry-credentials` | 204: every credential and pull token of the caller. Idempotent. |

All three need an ordinary Hub sign-in and answer 404
`cog_registry_not_served` when serving is off. What the credential is:

- **Opaque and stored as a digest.** `secret` is shown once; the Hub keeps
  its SHA-256 (`collab_cog_registry_credentials`, migration 13). There is no
  signing key to configure or rotate. `id` is one URL-safe path segment
  matching `[A-Za-z0-9][A-Za-z0-9_-]{0,127}` (today `crc-` and 24 hex
  digits), and `username` is the same string.
- **Pull-only, unless exchanged to publish.** A `pull` credential can be
  turned into pull tokens and nothing else: of a token request's scopes it
  is granted the repositories it asks to `pull`, and `push` adds nothing.
  Only a credential exchanged with `{"scope": "publish"}` mints tokens that
  carry `push`, and then **per repository**: a token asked for with
  `repository:a:pull,push repository:b:pull` may push to `a` and only pull
  from `b`. On a Hub that accepts no publishes, `push` in a scope names
  nothing at all, as before publishing existed.
- **Short-lived.** It stops at `expires_at` (`credentialTtlSeconds`, fifteen
  minutes by default), and so does every token minted from it, whatever
  `tokenTtlSeconds` says. Exchange a fresh one before each install, and size
  the lifetime to the longest single install.
- **Revocable, at once.** Revoking deletes the row and its tokens with it;
  the next request with one of those tokens is a 401. A user holds at most
  20 live credentials; exchanging past that drops the oldest, and one
  user's exchanges are serialized so concurrent ones cannot exceed it.
- **Worth only what its owner is.** On a membership-resolving deployment the
  owner's membership is read again at every token mint *and on every `/v2`
  request*, by the same lookup the catalog's authentication makes. A member
  removed from their organization is refused on the next request (403
  `DENIED`), whatever tokens they hold, and a lookup that fails answers 503
  rather than admitting anyone. Under claims-sourced auth there is nothing
  server-side to re-read, and the two lifetimes are the bound.
- **Useless anywhere else.** It is not a JWT. Every Hub API outside `/v2/`
  answers 401 to the credential, to its secret presented as a bearer token,
  and to a pull token; the backing registry has never heard of it.

**What "the Hub session ends" means here.** The credential records the `sid`
of the session it was exchanged from, but the Hub has no channel to learn
that a Keycloak session ended (no introspection call, no back-channel
logout), and none was added for this. What bounds a credential after
sign-out is therefore: the client revoking it, by id (clients are expected
to revoke each credential they hold when they are done with it and at
sign-out, and not to rely on the revoke-all route), and its lifetime,
fifteen minutes by default.

Expired rows never authorize a request. An exchange deletes only that
caller's expired credentials. Credential and token lookups and mints can
start a background sweep on its own pooled connection in bounded batches.
The usual interval is five minutes per process; a full batch makes the next
call retry sooner. Cleanup does not run in the request's transaction. A quiet Hub can retain expired rows until another call starts
the sweep; that does not extend their validity.

The token endpoint also accepts a Hub credential the API already accepts (a
bearer access token, the gateway's cookie) in place of Basic auth, for a
client that attaches it per request and never stores it. A token minted that
way has no credential to be revoked with; it ends with `tokenTtlSeconds` or
with `DELETE /v1/cogs/registry-credentials`. The read API itself takes pull
tokens only.

### What is served

**The v1 catalog is shared across the Hub.** An authenticated caller admitted
by the catalog's existing auth check may pull every present, indexed Cog in
the configured sources. On membership-resolving deployments, membership in
any Hub organization is sufficient; a platform operator can also be admitted
without an organization. Claims-sourced deployments use the organization
claims accepted at sign-in, bounded by credential and token lifetimes.
There is no per-Cog or per-organization read visibility rule in this version.
Repository-scoped pull tokens limit the repositories named by a token; they
do not establish organization isolation. Configure sources for a shared
catalog, not for packages that must be hidden from other admitted Hub users.
This is the v1 access rule, not a promise of isolation pending another change.
Any later visibility policy must apply consistently to discovery and pulls.

**Only what the catalog would show.** Authorization is the rule
`GET /v1/cogs` applies: a caller the Hub authenticates sees the whole
catalog, so may pull all of it. A pull always needs a credential, even where
a `security.paths` rule opens catalog discovery to anonymous callers. Every
answer starts from the catalog's rows for the repository: present, indexed
Cogs. A repository, tag or digest the catalog does not list is a 404 whether
or not a registry holds it, and the request never reaches a registry.
Removed versions, non-Cog artifacts and failed reads are not pullable.

**Repository names are the backing repository paths**, unchanged and not
qualified by source: `<hub>/cogs/cog-a@sha256:…`. A repository path carried
by two sources is one repository here. The same digest in both is the same
content, served from whichever source answers; a tag both carry resolves to
the newest push, as the catalog orders versions. Moving a repository to
another registry under the same path changes nothing a client sees.

**Tags come from the catalog**, never from a live listing. A tag exists here
when an indexed, present row carries it, and resolves to that row's digest.
A tag pushed since the last sweep is not served until it is indexed.

**Every lookup is exact.** A manifest is pullable if and only if its digest
has a pullable catalog row in that repository, found by `(repository,
digest)` or by a stored tag naming it. There is no window over a
repository's versions: the oldest indexed pin pulls like the newest. Nothing
is scanned and nothing is cached; each read is a few catalog queries and at
most one request to the source holding the content (one more per additional
source holding the same digest or blob, up to four sources, when the first
no longer has it).

**A blob is served only while a pullable manifest of that repository
references it**, and that is established from stored data. When the Hub
serves a manifest, which it has just verified against its digest, it records
the manifest's config and layer descriptors in `collab_cog_manifest_blobs`
(migration 13). A blob request is then one query joining those rows to the
pullable rule:

- a blob no recorded manifest references is `BLOB_UNKNOWN`, and no registry
  is asked;
- removing a version (or a re-read marking it failed or not a Cog) makes its
  blobs unpullable at once, unless another pullable manifest of the
  repository references them;
- the descriptor's `size` is how a response declares its length, and how an
  oversized blob is refused, before the registry is asked. `maxBlobBytes`
  applies to every candidate source, not only the first, and again to the
  bytes as they are counted, whatever a descriptor said. A descriptor is a
  publisher's claim: if two manifests record different sizes for one digest,
  one of them is wrong, the disagreement is logged, and each candidate is
  held to its own recorded size and to the digest as it streams, so the
  wrong one fails verification rather than being served.

The record is written on the first manifest read (for every source that
holds the digest; later reads of it write nothing), which every OCI client makes
before it asks for a blob, and it lives in the shared database, so the blob
requests may land on any replica. A client that asks for a blob of a manifest
nobody has yet pulled through the Hub gets `BLOB_UNKNOWN` until the manifest
is read. The record of a version is deleted when the indexer marks that
version removed, so the table holds rows for present versions only; a version
that comes back is recorded again by its next manifest read. Recording and
removal take the version's catalog row, so a manifest has all of its
descriptors or none.

**Multi-platform indexes are not traversed.** An index whose own digest is a
pullable row is served as the bytes it is. Its child manifests are served
only if their digests are pullable rows themselves, and an index contributes
no blobs. Cog bundles are single manifests.

**Deadlines.** Every `/v2` request runs under one aggregate deadline that
starts before its first lookup: `maxBlobSeconds` for a blob body, thirty
seconds for everything else (manifests, tags, `HEAD` of a blob). Past it the
request ends in 503, or, for a blob whose headers are already out, in a
dropped connection. The response owns the open connection to the source and
closes it however the exchange ends, including when the deadline passes
while it is blocked sending to a client that has stopped reading (that
client's own connection is then the server's and the gateway's to reap).
The catalog, credential and membership queries a request makes spend from
the same budget on the database's side: the pool wait is capped at what is
left, and before each statement a transaction-local `statement_timeout` is
set to what is left at that moment, so Postgres cancels a statement that
would outlive the request and no later statement gets the time an earlier
one used. Once the budget is spent no further SQL is sent, including when
installing the timeout itself used up what was left. The membership lookup,
and the first-sign-in membership write a single-organization deployment may
make at the token endpoint, are bounded this way only inside a registry
request; every other caller of the organization store is unchanged. The
token endpoint and the version check run under the same aggregate timeout
as the read routes, which also covers waiting for a worker thread.

One residual: the round trip that installs a statement's timeout is a
trivial statement with no timeout of its own. If that round trip itself
stalls (a dead server or network; it takes no lock), it is bounded by the
pool's connection settings and TCP keepalive, not by the request budget.

**Streaming.** Blobs are relayed in 64 KiB chunks and never buffered whole.
Each is hashed as it passes and its last chunk is held until the hash and
the length match the digest and the manifest's descriptor; a blob that fails
reaches the client short, with the connection dropped, never complete and
wrong. Digests are never rewritten. A redirect from the registry to object
storage is followed by the Hub, without the registry credential once it
leaves the registry's origin, and subject to the [redirect rules](#redirects);
the client is never redirected. (Redirecting
blobs to a signed URL would take that traffic off the Hub at the price of
showing clients the storage host; streaming is the default and the only
mode today.)

A source removed from configuration no longer has a sweep to collect its
stored blob descriptors. Those descriptors grant no pulls for that source,
but their retention needs a separate cleanup policy; tracked in
[#193](https://github.com/nebari-dev/collab-hub-pack/issues/193).

### Redirects

Registries commonly redirect a blob request to object storage. The Hub
follows those redirects itself. **With serving off, and no
`blobRedirectHosts` on the source, they are followed as they always were**
(the registry credential is dropped once a hop leaves the registry's
origin, and nothing else is checked), so an indexer that worked keeps
working.

**Enabling serving turns the rules below on for every source, for the
indexer as well as for pulls.** That is a consequence of enabling serving
worth checking before you do: a registry that redirects layers from `https`
to plain-`http` storage, to a loopback address, or to a private IP literal
the source does not list, indexes today and will stop indexing (its artifacts are recorded as failed and leave the catalog)
once serving is on. That is deliberate: a source the Hub could not serve a
pull from should not look healthy in the catalog. A source that sets
`blobRedirectHosts` has the rules on regardless of serving.

- `https` is never downgraded to `http`, on any hop, including one that
  leads back to an `http` registry;
- otherwise a redirect within the registry's own origin is followed;
- a loopback, link-local, multicast or unspecified address is never a
  destination. That covers `127.0.0.0/8`, `::1`, `169.254.0.0/16` (the cloud
  metadata address), `fe80::/10` and `localhost`;
- neither is an instance-metadata endpoint that sits outside those ranges:
  `fd00:ec2::254` (AWS over IPv6), `100.100.100.200` (Alibaba Cloud),
  `168.63.129.16` (Azure) and `192.0.0.192` (Oracle Cloud). An IPv6 literal
  that carries any refused IPv4 address inside it (IPv4-mapped, NAT64, 6to4,
  Teredo) is refused with it. `blobRedirectHosts` cannot re-admit any of
  the addresses in this item or the one above;
- a host must be either a canonical IP literal or a name with at least one
  non-numeric label. `2130706433`, `127.1`, `0x7f000001` and the like, which
  a resolver reads as addresses, are refused rather than interpreted; a
  trailing dot is ignored before every check;
- cluster-internal **names** are allowed: object storage inside the cluster
  is normal. A redirect to a private **address literal** (RFC 1918, IPv6
  unique-local `fc00::/7`, carrier-grade NAT `100.64.0.0/10`, or any other
  address that is not globally routable) is followed only when that address
  is listed in the source's `blobRedirectHosts`. Storage reached by IPv4
  literal needs an entry; `blobRedirectHosts` does not take IPv6 literals,
  so storage on a private IPv6 address must be given a name;
- when a source sets `blobRedirectHosts`, a redirect off the registry's
  origin must name a listed host, and nothing else is followed.

The trust model: a source is a registry the operator configured and gave a
credential to, so these rules are defence in depth against a registry that
is compromised or misconfigured into pointing the Hub elsewhere. They look
at the URL, not at what a hostname resolves to; a name that resolves to a
refused address is not caught unless `blobRedirectHosts` is set, which is
what closes that. Set it wherever the storage hosts are known.

### What stays inside the Hub

No response, header or error from `/v2/` or the catalog carries a backing
registry's credential or host. Upstream error bodies are not read, upstream
headers are not copied, and `Location` headers are followed rather than
relayed. A platform operator additionally gets `backing_reference`
(`<backing host>/<repository>@<digest>`) on
`…/versions/{digest}/reference` and on each of its `locations`; the field is
absent for everyone else, and `source_id` names the source for any
authenticated caller.

Logs are the operator's, and may name a backing host; they must not carry a
credential. The rule for the HTTP client's own request lines is the same
everywhere, publishing included: a **read** from a registry (indexing,
serving a pull, and in a publish the bundle validation and the check that a
repository is new) is logged with the backing URL, without its query string
or userinfo; a **write** is logged with the operation and the source id and
no URL at all. The Hub's own lines name the source id. The HTTP libraries'
lines are filtered: the request log (`httpx`, INFO) loses URL query strings
and userinfo, so a pre-signed storage URL is not logged with its signature,
and the transport trace (`httpcore`, DEBUG) loses every header value, so
neither a redirect's `Location` nor a `Set-Cookie` or `WWW-Authenticate` is
logged. A **write** to a registry (publishing) is logged without its URL
altogether: the URL is an upload session's, at the backing registry, and its
path can be the capability to write to that session, so the line carries the
operation and the source id instead
(`HTTP Request: PATCH [registry write: upload chunk, source main] "HTTP/1.1 202 Accepted"`).
What that protects is the session URL. The backing registry's **host name**
is configuration, not a secret, and still appears in logs an operator reads:
in read lines as above, and at DEBUG in the transport's connection trace
(`connect_tcp`, TLS `server_hostname`) for reads and writes alike.
These two filters are **process-wide**: they are installed when a
deployment turns on `cogs.serve.enabled` and then apply to every HTTP client
in the process, not only the registry's. With serving off, logging is
exactly what it was. That includes a deployment that only indexes: its
indexer follows the same redirects, and its DEBUG transport trace is
unfiltered, as it has always been; keep that logger above DEBUG there.

### Replicas and the indexer

Serving builds its own registry sources on every replica that serves `/v2/`,
separately from the indexer's, so an API replica that does not sweep (the
indexer off, or running as another workload) still reaches the sources. The
source list and its Secret-backed variables are already rendered whenever
`registry.sources` is non-empty. Replicas hold no serving state of their
own: what is pullable, which blobs a manifest references, credentials and
tokens are all in the shared database.

### Gateway

`/v2` carries bodies up to `maxBlobBytes` for up to `maxBlobSeconds`, on the
same host as the API:

- **Authentication.** The app authenticates `/v2` itself, and its 401 is the
  challenge a client follows. The path-protection map does not apply to it,
  whatever `security.paths` says. Behind the Nebari gateway the chart adds
  `/v2` to the NebariApp's `publicRoutes` when serving is on, so the
  gateway's browser sign-in does not answer in the app's place.
- **Timeouts.** With `api.ingress.kind: HTTPRoute` the chart renders a `/v2`
  rule with `timeouts.request` of `maxBlobSeconds` plus 30 seconds. That
  field needs Gateway API v1.2 CRDs; on older ones set `routeTimeout: false`
  and raise the gateway's own request timeout for the route. With an
  Ingress, set the controller's read and send timeouts (for ingress-nginx,
  `proxy-read-timeout` and `proxy-send-timeout`) to at least
  `maxBlobSeconds`. Behind the Nebari gateway (`api.nebariapp.enabled`) the
  route is the operator's and NebariApp has no timeout field, so the chart
  attaches an Envoy Gateway `BackendTrafficPolicy` with the same
  `requestTimeout` (a field of Envoy Gateway v1.2 and later) to the
  NebariApp's public route
  (`<api name>-public-route`). Without it the route has Envoy's 15 second
  default and a larger layer is cut off mid-body. Three things to know
  about that policy: it applies to the whole public route, so `/v1`, `/mcp`
  and `/health` get the same gateway ceiling (the app's own timeouts on them
  do not change); a policy on a route replaces a `BackendTrafficPolicy`
  attached to the Gateway for that route rather than merging with it, so
  carry over anything the Gateway-level one sets that the route still
  needs; and it names the route by the operator's naming convention, so
  check `kubectl get backendtrafficpolicy <api name>-cog-pulls -o yaml`
  reports the route as accepted after the first deploy. `routeTimeout:
  false` leaves the policy out.
- **Buffering.** Responses are streamed with a `Content-Length`; a proxy
  that buffers responses to disk (ingress-nginx's `proxy-buffering`, with
  `proxy-max-temp-file-size`) should have that turned off for `/v2`, or its
  temp-file limit raised past `maxBlobBytes`. Requests have no bodies.
- **Load.** Every byte of every install goes through the API pods. Size
  their network and replica count for it, and `frames.postgres.pool` for
  a handful of short queries per `/v2` request (the token, the owner's
  membership, the catalog lookup, and one insert the first time a manifest
  is read).

### Trying it

`scripts/cog-serve-e2e/run.sh distribution` (or `zot`) publishes a Cog
through the Hub into a private registry and pulls it back from the Hub with
`oras` over TLS; see [publishing](#trying-it-1). CI runs both
(`.github/workflows/test-cog-serve-e2e.yaml`); the same script against two
registries is the check that the registry behind the Hub is swappable.

## Publishing through the Hub

Off by default. Marking one source `publish: true` makes the Hub accept
pushes on the same `/v2/` surface it serves pulls on, and write them through
to that source. A publisher needs a Hub sign-in and the publish permission,
and no account on the backing registry:

```yaml
cogs:
  registry:
    sources:
      - id: main
        kind: static                 # any kind; harbor works the same way
        url: https://registry.example.com
        publish: true                # exactly one source
        credentials:
          existingSecret: collab-hub-registry-robot   # must be able to push
  serve:
    enabled: true                    # publishing needs the /v2 surface
  publish:
    allowedRoles: []                 # operator | owner | member
    allowedUsers: []                 # Hub user ids
    maxPendingRepositories: 20       # unsettled names one organization may hold
```

`publish: true` on more than one source, or without `cogs.serve.enabled`, is
refused at render and at startup. The publish source may be a `static`
source with neither `repositories` nor `indexUrl`: the repositories
published through the Hub are recorded, and the indexer enumerates them
along with whatever the source lists itself, so any OCI registry can be the
publish target with no repository list to maintain.

Standard clients work unchanged against the Hub host with a publish
credential: `oras push`, `nebi publish`, and a Nebi server's publish.

### Who may publish

**The publish permission.** Nobody holds it by default, and being able to
pull never implies it. An operator grants it in `cogs.publish`:

- `allowedRoles` grants it by role: `operator` (the platform role), `owner`
  and `member` (the caller's role in their organization). To let every
  organization owner publish, set `allowedRoles: [owner]`; to make one
  person a publisher, make them an owner of their organization, or list
  them in `allowedUsers`. Roles exist only where the Hub resolves
  organizations from membership.
- `allowedUsers` grants it to named Hub user ids (the ACL principal, the
  `sub` on a deployment that pins identity to it), whatever their role. Under
  claims-sourced auth there are no roles, and this is the only way.

Changing either is a values change and a rollout.

**Repository ownership.** A repository belongs to the organization that
first publishes to it through the Hub, and **is never reassigned to another
organization automatically**. The Hub writes the repository's row before it
forwards the first manifest: *pending*, and already owned by the publisher's
organization. What the registry answers decides what becomes of it:

- **accepted**: the row is committed. The repository is that organization's.
- **definitely refused** (a 4xx other than 408, including a 429, or the
  registry refusing the Hub's own credential): nothing was stored. The
  pending row is deleted once every manifest that organization had in flight
  for the name has been refused, and the name is free again. The row holds
  each attempt by its id, so a publish cut off by its deadline while the
  name was being reserved waits for the reservation to finish and takes back
  exactly its own hold.
- **unknown** (a timeout, a 408 or a 5xx, a failure to record the outcome): the
  registry may hold the manifest, so the row stays, pending and owned, for
  as long as it takes. No other organization can publish to the name, or
  open an upload in it, including a platform operator; the same organization
  can simply publish again, which settles it.

A pending repository is not ownership a sweep relies on at once: two minutes
after it was written, sweeps start looking there. What they find is listed
either way, and the row is committed **only if a digest found there is one
the registry is known to have accepted from that organization for that
repository**. That is the recovery path for a publish whose acceptance was
recorded and whose commit was not: it settles at the next sweep. Nothing
else found there proves whose the name is, and that includes a digest the
organization merely attempted: the attempt may have stored nothing, and the
same bytes can be pushed by anyone. So when acceptance itself was never
recorded, or the content was pushed to the registry some other way, it is
listed as out-of-band content with no publisher, the row stays pending, the
Hub logs
`cog_publish_pending_repository_holds_other_content`, and from then on only
a platform operator may publish there, until an operator sorts it out
(release the name, below, or publish). If the registry has no such
repository, that is an empty answer, not an error, and the row stays
pending. An upload alone never creates a row.

One organization may hold at most `cogs.publish.maxPendingRepositories`
(20 by default) pending names at once. Nothing pending expires, so this is
what bounds the names a publisher can leave behind while the registry is
failing; past it, a manifest for one more new name is refused with 429
`TOOMANYREQUESTS` before anything is forwarded. Publishing again to a name
that is already pending is never refused by it, and settling or releasing
one makes room. Of two organizations
publishing a new name at once, one holds it and the other's manifest is
refused before anything is written.

A name that is stuck pending, with nothing in the registry and nobody coming
back for it, is released by a platform operator with database access. There
is deliberately no API for it:

```sql
-- what is pending, whose, and since when
SELECT repository, owner_org_id, created_by, created_at
FROM collab_cog_repositories WHERE NOT committed;

-- release one name, after checking the registry holds nothing under it
DELETE FROM collab_cog_repositories WHERE repository = 'cogs/example' AND NOT committed;
```

After that a push to the repository needs the publish
permission *and* membership of the owning organization. Platform operators
are excepted from the ownership rule, not from the permission.

**A repository that was not published through the Hub** accepts pushes from
platform operators only. That is decided from the registry as well as the
catalog: before anything of a publisher's reaches a name nobody owns yet --
an upload being opened, a check for a blob already there, a manifest, which
is checked before its bundle is read back for validation -- the Hub asks the
publish source whether the repository already holds a tag (one request), so
a repository pushed to the registry directly is protected even before any
sweep has indexed it. A blob check there answers "not here". Only an answer that says so counts as
"nothing there": a missing repository, or a tag list for that repository
that is explicitly empty. If the registry cannot answer, or answers with
anything else (no `tags` field, another repository's name, not JSON), the
publish is refused with 503 and nothing is reserved or forwarded. So one
organization cannot overwrite another organization's Cog, or a tag of it,
nor content nobody published through the Hub.

**Checked on every request.** The permission, the caller's current
organization and roles, and the repository's ownership are checked on every
push request, before anything is sent to the registry: a role withdrawn or a
member removed a moment ago is refused on the next chunk. On a
membership-resolving deployment these are read from the Hub's tables each
time. Under claims-sourced auth the organization is the one the Hub session
named when the credential was exchanged, and the credential's lifetime is
the bound.

### What a client does

```
POST /v1/cogs/registry-credentials {"scope":"publish"}   201 {id, registry, username, secret, scope:"publish", expires_at}
GET  /v2/token?service=<hub>&scope=repository:<name>:pull,push   (Basic username:secret)   200 {token, ...}
POST  /v2/<name>/blobs/uploads/                    202  Location: /v2/<name>/blobs/uploads/<id>
PATCH /v2/<name>/blobs/uploads/<id>                202  Range: 0-<n>
PUT   /v2/<name>/blobs/uploads/<id>?digest=sha256:…  201  Location: /v2/<name>/blobs/sha256:…
PUT   /v2/<name>/manifests/<tag|digest>            201  Docker-Content-Digest: sha256:…
```

The exchange answers 403 `cog_publish_forbidden` when the caller does not
hold the permission. When no source is marked `publish`, `publish` is not a
scope at all: the exchange answers the same 422 `validation_error` it
answers for any unknown scope, exactly as before publishing existed. A publish credential has the same lifetime, revocation
and storage as a pull credential, and may also pull.

| Route | Answers |
| --- | --- |
| `POST /v2/<name>/blobs/uploads/` | 202 with the upload's `Location`, `Range: 0-0` and `Docker-Upload-UUID`. With `?digest=` and a body, the whole blob in one request: 201. A cross-repository mount request (`?mount=&from=`) is answered as an ordinary upload; nothing is mounted. |
| `PATCH /v2/<name>/blobs/uploads/<id>` | The next chunk, streamed: 202 with the new `Range`. A `Content-Range` is optional and checked in full: one that does not start where the upload left off is 416 with the `Range` it is at; one that is malformed, ends before it starts, or is not as long as the body is 400. |
| `PUT /v2/<name>/blobs/uploads/<id>?digest=…` | Closes the upload, optionally with the last (or only) bytes: 201. A `Content-Range` on it is checked the same way. |
| `GET /v2/<name>/blobs/uploads/<id>` | 204 with `Range`: where the upload is. |
| `DELETE /v2/<name>/blobs/uploads/<id>` | Cancels the upload: 204. |
| `HEAD /v2/<name>/blobs/<digest>` | For a caller who may push to `<name>`: 200 if the publish source already holds the blob there, so a client can skip an upload. It makes nothing pullable. |
| `PUT /v2/<name>/manifests/<reference>` | Validates, commits and indexes the manifest: 201. |
| `DELETE` of a manifest or a blob | 405 `UNSUPPORTED`. Nothing is deleted through the Hub. |

Push errors, in the registry format: `UNAUTHORIZED` (401, with a challenge
whose scope is `repository:<name>:pull,push`), `DENIED` (403: no publish
permission, another organization's repository, settled or still pending, or
a token that does not carry `push` for this repository),
`BLOB_UPLOAD_UNKNOWN` (404: no such upload, or one that ended after a write
whose outcome was unknown; start the blob again), `BLOB_UPLOAD_INVALID` (400: a `Content-Range`
that does not fit, or **another request is writing to this upload: retry**;
416 for a chunk out of order), `DIGEST_INVALID` (400: the registry found the
bytes do not hash to the digest), `SIZE_INVALID` (413: over `maxBlobBytes`, or
over the publish registry's own limit), `MANIFEST_INVALID` (400, or 413 for a
manifest over 5 MiB), `TOOMANYREQUESTS` (429: too many uploads open or still
being cleaned up, or the organization already has `maxPendingRepositories`
names pending), `UNSUPPORTED` (405), `UNAVAILABLE` (503, including when the
publish registry answers 429 or 408: try again; after a 429 the upload is
where it was).

One answer is neither a success nor a refusal: **the registry accepted the
manifest and the catalog does not list it**. It is 503 `UNAVAILABLE` when the
catalog write could not be made (put the manifest again, or wait for a
sweep), and 500 `UNKNOWN` when the catalog refused the row itself; in both
the message says the manifest is stored. It is never reported as a 201.

### What happens to a push

**Uploads are sessions the Hub owns.** The client sees only the Hub's own
upload id and paths. The backing registry's session URL stays in the
database (`collab_cog_upload_sessions`, migration 14), so any replica can
continue an upload, and no `Location`, `Range` or `Docker-Upload-UUID` a
client sees is the registry's. A session belongs to the user who opened it
and to one repository, expires after an hour with no chunk recorded (each
one renews it, and a single write may run for as long as `maxBlobSeconds`
allows), and a user holds at most 64.
Bytes are streamed to the registry as they arrive, counted against
`maxBlobBytes`, and never buffered; the registry verifies each blob against
its digest when the upload is closed. Writes go to the registry's own origin
only: an upload location on another origin is refused and a write is never
redirected.

The Hub's record and the registry's session are kept in step. The Hub takes
its slot *before* it asks the registry to open a session, so the cap holds
before anything exists upstream, and the slot stays its opener's until the
registry's session is attached: neither the cap nor the cleanup touches a
slot that is still opening. A session the Hub lets go of (past the cap,
expired, cancelled, or dead, below) is cancelled at the registry *before*
its record is deleted, a few per request; a record whose cancellation failed
is kept and tried again, for at most a day, and a user whose slots are all
waiting to be cleaned up is answered 429. A registry session the Hub has no
slot for and could not cancel is recorded all the same, so its location is
not lost.

**One request at a time writes to a session.** A `PATCH`, the closing `PUT`
and a `DELETE` each take the session's lease (a compare-and-set on its row,
so it holds across replicas, and no database connection is held while bytes
move). A second write while it is held is refused with
`BLOB_UPLOAD_INVALID` and told to retry, so two requests cannot both add to
the same byte count and pass `maxBlobBytes` between them. The lease is given
back only when the Hub knows what the registry took: after a write it
recorded, or after a definite refusal. **After any write whose outcome is
unknown** (a timeout, a lost response, a 5xx, a failure to record the
result) **the upload is over**: the Hub's byte count can no longer be
trusted, so the session is cancelled at the registry and every later request
for it is `BLOB_UPLOAD_UNKNOWN` (404). The client starts the blob again. A
lease is never taken over by another request; if the Hub cannot even record
that a session is dead, the lease running out says so.

**A manifest is validated before it is committed.** The layers were just
uploaded, so at manifest `PUT` the Hub reads the bundle with the catalog's
own reader, through the indexer's code path, before forwarding anything. A
bundle the catalog would not list is refused with `MANIFEST_INVALID` and the
reader's errors, one per entry: not a Cog, no id, a reader error such as bad
frontmatter or a missing manifest file, a layer that was never uploaded.
Nothing is written to the registry, nothing is listed and no repository is
claimed. A multi-platform index is refused: a Cog bundle is a single
manifest. If the registry cannot be read while validating, the answer is 503,
not a verdict on the bundle.

**An accepted manifest is indexed in the request.** Once the registry has
the manifest, the row the validation produced is stored through the
indexer's lock-less targeted path, so `GET /v1/cogs` lists the version
immediately, and the manifest's blobs are pullable through the Hub at once.
The authenticated publisher is recorded on the row as `published_by` (the
Hub user id) and `published_org`. These are separate from the card's
`publisher`, which stays whatever the bundle declares about itself. They
appear on catalog entries only on a Hub that accepts publishes, are omitted
for anonymous callers, and are null for a version pushed to the registry
directly. A later sweep reconciles the row like any other (tags moved at the
registry, removal) and leaves the publisher as recorded.

The row and its tag are one transaction, under the request's deadline. **A
tag put through the Hub is on exactly one digest** of its source and
repository: putting a manifest under a tag another digest holds moves the
tag, and putting a manifest by digest adds no tag and brings none back. If
that transaction fails, the answer says the manifest is stored and not
listed (above). A manifest with no tag stays listed on any kind of source: a
`static` source finds manifests through their tags and so cannot list one
with none, and a sweep asks the registry about each such row instead (one
`HEAD`), removing it only when the registry no longer has it.

**Who published a digest is only ever taken from an attempt the registry is
known to have accepted.** Each manifest `PUT` is recorded as an attempt of
its own (`collab_cog_publication_attempts`) before it is forwarded, and
marked accepted only after the registry has accepted that attempt's
manifest. Whichever write then lists the digest (the request itself, a
retried push, a later sweep) takes the publisher from the earliest accepted
attempt, and a publisher that is on a row stays there. An attempt that was
refused is deleted by its own request and no other; one whose outcome was
never known attributes nothing, to anyone, and is purged after a day. The
consequence is stated plainly: **if the registry accepted a manifest and the
Hub could not record that it had, the publisher of that digest is unknown**
(`published_by` is null, as for a version pushed to the registry directly)
until somebody publishes it through the Hub again. The Hub does not guess.

A sweep that is running while a publish lands does not undo it: its removal
step only touches rows last written before it began enumerating, so a
version published (or published again after having been removed) since then
is left for the next sweep to judge. The sweep's writes and the publish
write take their database locks in the same order.

**Deadlines and limits** are those of pulls: one aggregate deadline per
request (`maxBlobSeconds` for a request that carries a blob, thirty seconds
otherwise), store calls bounded by the request budget in the database, and
`maxBlobBytes` per blob.

### Rollout

1. **Give the Hub's registry credential push** on the publish source's
   project or namespace. The Hub writes with the same credential it reads
   with (`credentials.existingSecret` on that source); there is no second
   credential block. Until it can push, publishes answer 503 and the Hub
   logs `cog_publish_upstream_failed` with `OCIAuthError`.
2. Mark the source `publish: true`, with serving enabled, and name who may
   publish in `cogs.publish`. Chart and values land together, as always.
3. **Stop granting people membership of the registry project for
   publishing**, and remove what was granted: publishers need a Hub account
   with the publish permission, nothing else. Repositories that already
   exist in the registry were not published through the Hub, so they accept
   pushes from platform operators only; republish them under a new name, or
   have an operator publish the next version.

### Trying it

`scripts/cog-serve-e2e/run.sh distribution` (or `zot`) starts the Hub with a
private registry as its publish source and no repository list, publishes a
Cog to the Hub with `oras`, and pulls it back. CI runs both registries.
`scripts/cog-serve-e2e/conformance.sh` runs the OCI distribution-spec
conformance suite's pull and push workflows against the Hub. The suite's
fixtures are not Cog bundles, so it starts the Hub through a test-only
launcher that replaces the validation step in its own process; no setting
does that.

## How the rules stay in step

The refusals above exist in three layers: `values.schema.json` (structure
and grammar), `templates/cogs-validations.yaml` (cross-field rules, at render
time) and the API's `config.py` / `cogs/registry.py` (at startup). One
fixture keeps them from drifting: `scripts/testdata/chart/cogs-negative-cases.yaml`
holds every refused configuration once, in two forms — the chart values and
the `cogs` settings the chart renders from them — and two consumers read it.

- `scripts/chart_rules_parity.py`, run by `scripts/chart_render_tests.sh` in
  the lint workflow, renders each case and asserts the chart refuses it with
  the expected message; renders it again with the schema skipped and the
  validations template removed and asserts the Deployment's settings equal the
  fixture's settings form, so the two forms are proven to describe one
  configuration; and asserts every `fail` in the validations template fired
  for some case.
- `api/tests/test_config_cogs.py` feeds the settings form to `Config` and
  asserts the API refuses it with the expected message — or, for the rules
  that can only live in the chart (an empty `existingSecret`, ids that
  collide once derived into variable names, `extraEnv`, the CA key), that the
  API *accepts* what the chart would have rendered, with the fixture recording
  why the rule cannot be mirrored.

The fixture also lists **accepted** boundary configurations (ports, IPv6
literals, in-cluster hostnames, dotted project names) that the full chart
must render and the API must accept, so the schema cannot drift stricter than
the API unnoticed.

What this does and does not guarantee: every `fail` in the validations
template must fire for some case, so a template rule added without a case
fails the run. Schema and API rules have no automatic inventory — the fixture
enumerates them, and a new one needs a case added by hand; a case without the
rule in every layer fails that layer's test. Checks the chart cannot express
(credentials both-or-neither, a `*_env` name that is blank, unset, or
conflicts with an inline value) are tested directly in
`api/tests/test_config_cogs.py`.

Two asymmetries are deliberate, both in the direction of the chart refusing
what the API would take. Normalization: the chart validates values as
written; the API normalizes first (surrounding whitespace is stripped, and the
URL scheme is case-folded by the parser), so `projects: [" cogs "]` or
`url: HTTPS://…` fails the render but would be accepted by a bare process.
Host grammar: the API accepts whatever its URL parser does — internationalized
hostnames, IPvFuture literals, hostnames with characters outside
`[A-Za-z0-9._-]` — and the chart does not. A values file is authored, and the
render refuses what the API would have silently corrected or tolerated.

## Worked example: one Harbor source

Create the Secrets and the CA ConfigMap out of band (a secrets operator, a
sealed secret, or `kubectl`):

```sh
kubectl -n collab-hub create secret generic collab-hub-harbor-robot \
  --from-literal=username='robot$cogs+indexer' \
  --from-literal=password="$ROBOT_SECRET"
kubectl -n collab-hub create secret generic collab-hub-harbor-webhook \
  --from-literal=secret="$WEBHOOK_SECRET"
kubectl -n collab-hub create configmap collab-hub-cogs-ca --from-file=ca.crt=./gateway-ca.pem
```

Then in values:

```yaml
cogs:
  registry:
    sources:
      - id: harbor-main
        kind: harbor
        url: https://harbor.example.com
        apiUrl: http://harbor-core.harbor.svc.cluster.local:80
        tokenUrl: http://harbor-core.harbor.svc.cluster.local:80/service/token
        projects: [cogs]
        credentials:
          existingSecret: collab-hub-harbor-robot
        webhook:
          existingSecret: collab-hub-harbor-webhook
  caBundle:
    configMap: collab-hub-cogs-ca
  index:
    enabled: true
    intervalSeconds: 300
```

The rendered Deployment carries `COLLAB_HUB_API__COGS__REGISTRY_SOURCES` (JSON,
no secrets), `COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_{USERNAME,PASSWORD,WEBHOOK_SECRET}`
from the two Secrets, and the CA bundle at `/etc/collab-hub/cogs-ca/ca.crt`.
Nothing inside the pod needs editing. `helm/collab-hub/ci/cogs-values.yaml`
is this example plus a static source, and `scripts/chart_render_tests.sh`
renders it (and every refusal above) in the lint workflow. Nothing in the
manifest is trusted implicitly: a URL that embeds `user:password@` is refused
by the schema and the render, because the JSON lands in the pod spec and the
release history.

## Bare process

Without the chart, set the same settings directly. pydantic-settings reads
list-valued settings as JSON:

```sh
export COLLAB_HUB_API__COGS__INDEX__ENABLED=true
export COLLAB_HUB_API__COGS__REGISTRY_SOURCES='[{"id":"harbor-main","kind":"harbor",
  "url":"https://harbor.example.com","projects":["cogs"],
  "credentials":{"username_env":"HARBOR_USER","password_env":"HARBOR_PASSWORD"}}]'
export HARBOR_USER='robot$cogs+indexer' HARBOR_PASSWORD=...
```

Inline `"password": "..."` in the JSON is accepted too, for local runs; a
source that sets both the inline value and the `_env` name is refused as
ambiguous.
