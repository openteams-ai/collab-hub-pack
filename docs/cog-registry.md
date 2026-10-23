# Cog registry

The hub indexes Cogs from OCI registries. This page covers the `cogs:` block
of the Helm chart and the matching application settings: which registries are
read (**sources**), how the hub authenticates to them (**credentials**), how a
private CA is trusted (**CA bundle**), and how often the index is rebuilt
(**indexer**). The adapters themselves — what "Harbor" and "static" mean and
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
| `indexUrl` | static | URL of a `catalog.v1.json` listing repositories. A static source needs `repositories`, `indexUrl`, or both. |
| `caBundlePath` | all | Per-source CA bundle path inside the pod. Defaults to the shared bundle below when that is configured. |
| `requestTimeoutSeconds` | all | HTTP timeout, default 10, at most 60. |
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

### What the credential must be allowed to do

The Secret says *where* the credential lives; the registry decides what it
may do, and the indexer needs more than a pull-only credential. Harbor's
`repository:list` is a separate permission from `repository:pull`, and the
sweep's very first call — listing a project's repositories — needs it. A robot
account created with Harbor's usual pull-only set fails every sweep at that
call with a bare `403 forbidden`, before any artifact is read:

```
GET /api/v2.0/projects/cogs/repositories -> 403
cog_index_source_failed source=harbor-dev reason="list_repositories: RegistrySourceAuthError"
```

| Kind | Grant the credential | Why |
| --- | --- | --- |
| `harbor` | Project-scoped `repository:list`, `repository:pull`, `artifact:read`, `artifact:list`, `tag:list` on each project in `projects` | `repository:list` enumerates the project's repositories, `artifact:list` and `tag:list` enumerate what is in each one, and `artifact:read` with `repository:pull` fetch the manifests and the `COG.md` blobs. |
| `static` | Pull on each repository in `repositories` (and in the index document, if `indexUrl` is set) | A static source enumerates through the OCI distribution API alone, so tag listing, manifest reads and blob reads are all covered by the registry's pull right. |

The indexer only reads, none of these needs push, delete, or project
admin. A project-scoped robot account in Harbor (*Project → Robot Accounts*)
with exactly the five permissions above is enough. A system-scoped robot works
too but grants more than the indexer uses.

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
never contacts a registry and never proxies blobs, so an install goes client
→ registry with the pinned reference it hands out. The catalog is
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
stay: a client must know where to pull from. An anonymous `source_id` filter
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
