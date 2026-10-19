# Local development

Run the Collab Hub API on your machine — from a bare process with no
dependencies at all, up to real datastores, a real identity provider, the
browser pages, and the Helm chart on Kubernetes. Nothing here touches a
deployed hub.

Everything is driven from this directory:

```sh
cd dev
make help
```

**New here?** [Prerequisites](#prerequisites) → [Quick start](#quick-start) →
[The four levels](#the-four-levels). Then jump to whichever level covers what
you are changing. [Keycloak, step by step](#keycloak-step-by-step) is the long
one, and only matters once you reach level 3.

---

## Prerequisites

| Tool | Needed for | Check |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | every level | `uv --version` |
| Docker with Compose v2 | levels 2–4 (**not** level 1) | `docker compose version` |
| [kind](https://kind.sigs.k8s.io/), `helm`, `kubectl` | level 4 only | `kind version` |
| [kubeconform](https://github.com/yannh/kubeconform) | `make lint` only | `kubeconform -v` |
| [pixi](https://pixi.sh) | `make controller` (unless `ENVIRONMENT=host`) and `make op LOCATION=local` only | `pixi --version` |

You do **not** need a local Python: `uv` provisions the interpreter pinned in
`api/.python-version` (3.14, the one the image runs) on first run. The API
supports Python 3.13 and later; prefix any target with `UV_PYTHON=3.13` to run
it on 3.13, the version CI also tests.

One thing is not a tool. Connecting the Collab desktop client needs three
hostnames pointed at your own machine, once per machine:

```sh
echo '127.0.0.1 keycloak.localhost frames.localhost llm-internal.localhost' \
  | sudo tee -a /etc/hosts
```

Nothing else here needs it, and `make hosts-check` tells you whether it is
done. [Why those names](#why-one-port-and-why-those-hostnames) is in the
desktop section.

---

## Quick start

```sh
cd dev

# 1. The API on its own — no Docker, nothing to start first.
#    Frames land in dev/.local/frames.
make api

# 2. In another shell — dev auth needs no token at all.
curl -s localhost:8000/v1/frames
curl -s -X POST localhost:8000/v1/frames \
  -H 'Content-Type: application/json' \
  -d '{"name":"hello","body":"# Hello from local dev"}'
```

Nothing to tear down: `make api` started no containers. Once you move to
level 2 or above, `make down` stops the supporting containers (data survives)
and `make destroy` also deletes their volumes.

---

## The four levels

Each is independently useful. Start at the lowest one that covers what you are
changing, and only pay for the next when you need it.

| Level | Command | You get | Docker | Cost |
|---|---|---|---|---|
| 1 | `make api` | The API alone: dev auth, frames on local disk, everything else in memory | **none** | ~10 s |
| 2 | `make api-pg` | + Postgres, so history, groups, orgs, invitations and tasks work | 2 containers | ~30 s |
| 3 | `make api-oidc` | + Keycloak: real bearer tokens, the web surface, connector brokering | 3 containers | ~2 min first run |
| 4 | `make kind-up` | The Helm chart on a real Kubernetes cluster | a kind cluster | ~5 min first run |

Plus two cross-cutting targets: **`make api-desktop`** puts every service behind
a single port so the [Collab desktop client can connect](#connecting-the-collab-desktop-client)
— Hub address `http://localhost:9080` — and **`make api-desktop-fakes`** does the same
with the fake connectors wired in, so Collab shows them as connected.

### Do I need to start anything first?

**No.** Every `api-*` target starts the containers it needs, and each one is
idempotent — already-running containers are left alone. You never run
`docker compose` by hand, and you never need `make up` before `make api-pg`.

`make api` is the exception in the other direction: it starts **nothing at
all**. The API is a plain process on your machine at every level except 4;
Docker only ever supplies the things around it.

| Command | Postgres | S3 | Keycloak | Fake providers | Front door |
|---|:--:|:--:|:--:|:--:|:--:|
| `make api` | – | – | – | – | – |
| `make api-watch` | – | – | – | – | – |
| `make api-pg` | ✅ | – | – | – | – |
| `make api-oidc` | ✅ | – | ✅ | – | – |
| `make api-fakes` | ✅ | – | ✅ | ✅ | – |
| `make api-full` | ✅ | ✅ | ✅ | – | – |
| `make api-membership` | ✅ | – | ✅ | – | – |
| `make api-desktop` | ✅ | ✅ | ✅ | – | ✅ |
| `make api-desktop-fakes` | ✅ | ✅ | ✅ | ✅ | ✅ |

Only `make api-full` stores frames in S3, so only it starts the S3 store
(`make services`); the others start Postgres alone (`make postgres`), and keep
frames on the local filesystem. The desktop targets start both, through the
front door.

Containers left running from a previous level are **not** wired in by a lower
one: `make api-pg` then Ctrl-C then `make api` leaves Postgres up but running
in memory, because `make api` does not pass it to the process. Stop them with
`make down` when you want the machine quiet.


### Is Keycloak required?

Two different questions, with two different answers.

**Can the Collab desktop client connect to a hub with no Keycloak? No.** Its
sign-in is OAuth-only — it asks for a Hub address, derives
`keycloak.<host>/realms/nebari` from it, and runs an authorization-code flow.
There is no API-key field, no token to paste, and no bypass: `internal/hubconfig`
explicitly drops any legacy `apiKey` from its config file. So there is no Hub
address you can type that reaches `make api`. Use
[`make api-desktop`](#connecting-the-collab-desktop-client), which brings up
Keycloak and the front door for exactly this reason.

(The Collab *app* still runs without any hub — local capabilities, local agents,
its own bundled Intelligence Hub. It just has no Frames, no connectors, and no
hub-backed inference until it signs in somewhere.)

**Can you develop the hub with no Keycloak? Yes — that is the point of level 1.**
The dev-auth shortcut makes every request an authenticated user, so the REST
API and the MCP server work with no token at all:

| Target | REST `/v1/*`, `/docs` | MCP on `/mcp` | Connectors | Web surface | Collab client |
|---|:--:|:--:|:--:|:--:|:--:|
| `make api`, `api-watch` | ✅ no token | ✅ no token | – | – | – |
| `make api-pg` | ✅ no token | ✅ no token | – | – | – |
| `make api-oidc` | ✅ bearer | ✅ bearer | – | – | – |
| `make api-fakes` | ✅ bearer | ✅ bearer | ✅ fakes | – | – |
| `make api-full` | ✅ bearer | ✅ bearer | ✅ brokered | ✅ | – |
| `make api-membership` | ✅ bearer | ✅ bearer | – | ✅ | – |
| `make api-desktop` | ✅ bearer | ✅ bearer | – | – | ✅ |
| `make api-desktop-fakes` | ✅ bearer | ✅ bearer | ✅ fakes | – | ✅ |

A dash means the surface answers, but says it is unavailable — connectors report
`not_connected` when no broker URL is configured, and the web routes are a 404
rather than a 401. Nothing is silently broken.

Three of those deserve a sentence each:

- **Connectors need two things, not one:** a broker URL (only `api-fakes` and
  `api-full` set one) *and* a real bearer to forward. The hub hands *the
  caller's* token to Keycloak to fetch that user's provider token, and the
  dev-auth shortcut authenticates a subject without ever producing a token to
  forward. `make api-fakes` is the fastest way to work on connectors.
- **The web surface is not merely unauthenticated at level 1 — it is not
  mounted.** Setting `COLLAB_HUB_API__WEB__CLIENT_ID` is what registers the
  routes, and that means a confidential Keycloak client. Without it
  `/web/signin` is a 404, not a 401.
- **MCP needs no token at level 1.** Point any MCP client at
  `http://localhost:8000/mcp` and `tools/list` returns `list_frames`,
  `get_frame` and `get_active_frames` straight away — useful when the change
  you are making is to a tool rather than to auth.

So: reach for level 1 for routers, stores, models and MCP tools; reach for
Keycloak when the change touches authentication, connectors, the browser pages,
or the desktop client.

---

## How the pieces fit together

```mermaid
flowchart LR
  dev["you<br/>curl · Apollo Desktop"]

  subgraph host["your machine"]
    api["<b>collab_hub_api</b><br/>uvicorn :8000<br/><i>uv run python -m collab_hub_api</i>"]
    frames[("dev/.local/frames<br/><i>frame bodies</i>")]
  end

  subgraph compose["docker compose (dev/compose.yaml)"]
    proxy["<b>front door</b> :9080<br/><i>make hub-proxy</i><br/>routes by Host"]
    kc["<b>Keycloak</b> :8080<br/>realm 'nebari'"]
    pg[("<b>Postgres</b> :5432<br/>orgs · history · tasks")]
    s3[("<b>SeaweedFS</b> :9000<br/>S3 frame store")]
    fg["fake Google :8081"]
    fs["fake Slack :8082"]
    fg2["fake GitHub :8083"]
  end

  dev -->|"Bearer token"| api
  api -->|"verify signature (JWKS)"| kc
  api -->|"exchange bearer for<br/>the user's provider token"| kc
  api --> pg
  api --> frames
  api -.->|"frames.storage.backend=s3"| s3
  api -->|"provider API calls"| fg
  api --> fs
  api --> fg2
  dev -->|"sign in"| kc
  dev -.->|"Collab client:<br/>Hub address http://localhost:9080"| proxy
  proxy -.-> kc
  proxy -.-> api

  style api fill:#f3e8fc,stroke:#c840e9,color:#6b21a8
  style kc fill:#fef0db,stroke:#e8952c,color:#7c4a03
  style compose fill:#eeeef3,stroke:#4a4a6a,color:#1a1a2e
  style host fill:#e8faf8,stroke:#20aaa1,color:#0d5d57
  style proxy fill:#f3e8fc,stroke:#c840e9,color:#6b21a8
```

---

## The components

### The API — `collab_hub_api`

A FastAPI application started by `api/src/collab_hub_api/__main__.py`. It runs
directly on your machine (not in a container) so that an edit-and-restart cycle
is a second, not an image build.

Configuration is entirely environment-driven through `pydantic-settings`, with
two naming conventions that coexist:

- `COLLAB_HUB_API__<SECTION>__<FIELD>` — the nested settings model, e.g.
  `COLLAB_HUB_API__FRAMES__POSTGRES__URL`.
- `FRAMES_*` — the authentication settings, e.g. `FRAMES_BEARER_JWKS_URL`.

`dev/Makefile` groups these into named blocks (`BASE_ENV`, `DEV_AUTH_ENV`,
`OIDC_ENV`, `PG_ENV`, `S3_ENV`, `WEB_ENV`, …) and passes exactly the ones each
target needs. Read the top of the Makefile to see what any level actually sets.

`make api-watch` runs the same process under `watchfiles`, restarting it on
every change under `api/src`.

### Postgres

Backs frame **history**, frame **groups**, **organizations**, **invitations**,
**platform roles**, the **audit log**, **tasks**, and **usage**. Without it
those endpoints answer `503`, which is easy to mistake for a bug in the code
you just wrote — so if an endpoint returns 503, check you are on level 2 or
above.

`COLLAB_HUB_API__FRAMES__POSTGRES__AUTO_MIGRATE=true` creates the `collab_`
tables at startup; the dev targets set it.

```sh
make psql          # a psql shell on the dev database
```

### SeaweedFS — the S3 frame store

Frame **bodies** default to the local filesystem (`dev/.local/frames`). The S3
store is only needed to exercise the S3 code path, which `make api-full` does.
Its keys are `devaccesskey` / `devsecretkey` (`dev/s3/s3.json`), the API is on
<http://localhost:9000>, and the server's status page is at
<http://localhost:9333>. `make services` creates the `frames` bucket with the
AWS CLI, which speaks to any S3.

This was MinIO until its images left Docker Hub (#114, #115) and then quay.io,
which no account can pull; SeaweedFS is maintained, Apache-2.0, and serves the
part of S3 the frame store uses, conditional writes included. Nothing outside
`dev/` and `scripts/smoke_frames_s3.sh` knew which server it was: the API
speaks S3 through boto3 either way.

### Keycloak

The identity provider. It issues the bearer tokens the API verifies, hosts the
confidential client the web surface signs users in with, and — for connectors —
**brokers** each user's Google/Slack/GitHub token. See
[Keycloak, step by step](#keycloak-step-by-step) below.

It is **optional** for levels 1–2 and **required** for the connectors, the web
surface, and the Collab desktop client — see
[Is Keycloak required?](#is-keycloak-required). `make keycloak-admin` prints
the console URL and checks the admin credentials still work.

**The issuer follows the way in.** `compose.yaml` pins no `KC_HOSTNAME`; it
sets `KC_PROXY_HEADERS: xforwarded` instead, so the realm reports
`http://localhost:8080/realms/nebari` when reached directly and
`http://keycloak.localhost:9080/realms/nebari` when reached through the desktop
front door — each self-consistent. The API compares `iss` exactly, so the
target you run has to name the same entry point the token was minted on. That
is the whole reason `api-oidc` and `api-desktop` set different
`FRAMES_BEARER_ISSUER` values.

**Its data persists.** The realm, the identity providers you configure and the
account links people make live in a named volume, so they survive `make down`.
`make destroy` is what resets them, and `make realm-import` resets just the
realm.

### The fake Google, Slack and GitHub providers

`scripts/testdata/fake_google_drive_app.py`, `fake_slack_app.py` and
`fake_github_app.py`. Each serves both a `/broker/token` endpoint (standing in
for Keycloak's broker) and the provider API surface, so the whole connector
path can be exercised with no real OAuth app anywhere.

They are stdlib-only, so there is no image to build. The first two are the same
files the kind smoke tests use.

The GitHub fake answers the REST calls the connector makes — `/user` (carrying
the granted scopes in `X-OAuth-Scopes`, which is where the connector reads a
token's *real* grant), `/user/repos`, `/search/issues`, issue and pull reads
with their comments and reviews, and `/contents/…` — plus the GraphQL endpoint
behind the Projects V2 boards.

### The front door (`make hub-proxy`)

A Caddy container on port 9080 that routes by `Host` header, so Keycloak and
the hub appear on one port. Only the Collab desktop client needs it — see
[Connecting the Collab desktop client](#connecting-the-collab-desktop-client).

### kind and the Helm chart

Level 4 builds `api/Dockerfile`, loads it into a kind cluster, and installs
`helm/collab-hub` with `dev/values/kind.yaml`. See
[Level 4](#level-4--the-chart-on-kind).

### What persists, and what resets

| | `make down` | `make destroy` | `make clean` |
|---|:--:|:--:|:--:|
| Postgres rows — frames, orgs, roles, audit | kept | **erased** | kept |
| S3 objects | kept | **erased** | kept |
| Keycloak realm, identity providers, account links | kept | **erased** | kept |
| Frame bodies on disk (`dev/.local/frames`) | kept | kept | **erased** |
| The kind cluster | kept | kept | kept |

`make down` stops containers without losing anything, so it is safe to run
whenever you want the machine quiet. `make destroy` is the reset — reach for it
when a schema change has left old rows behind, and know that it also discards
any Google or GitHub identity provider you configured by hand. The kind cluster
is on its own switch, `make kind-down`.

To start completely over:

```sh
make destroy      # containers and their data
make clean        # frame bodies and scratch state
make kind-down    # only if you got as far as level 4
```

---

## Level 1 — the API alone

```sh
make api
```

**Starts no containers**, Keycloak included. Just `uv run python -m
collab_hub_api` with frames on local disk and every other store in memory —
nothing to wait for, nothing to clean up.

With no identity provider, **the Collab desktop client cannot connect to this**
(its sign-in is OAuth-only), and the `/web` pages are not mounted. Everything
else works: curl, the OpenAPI docs, and any MCP client, all with no token. See
[Is Keycloak required?](#is-keycloak-required).

Authentication uses the **dev-auth shortcut**, which needs all three of these —
this is the single most common local-setup mistake:

```sh
FRAMES_UNSAFE_AUTH_ENABLED=true   # opens the door
DEV_AUTH_ENABLED=true             # turns the shortcut on
DEV_AUTH_USER=dev-user            # names the subject
```

Setting only `DEV_AUTH_USER` authenticates nothing and every route answers 401.
`make api` sets all three, plus `DEV_AUTH_ORG=dev-org` and
`DEV_AUTH_WORKSPACE=default`.

Every request is then treated as `dev-user`, with no token:

```sh
curl -s localhost:8000/v1/frames
curl -s localhost:8000/health
curl -s localhost:8000/v1/cogs                    # the Cog catalog, empty here
curl -s localhost:8000/v1/cogs/catalog.v1.json    # {"schemaVersion":1,"repositories":[]}
```

These switches are a local-development affordance and nothing else. The Helm
chart **refuses to render** them on a deployment with `api.ingress.enabled:
true` — on a routed host they are an authentication bypass. See
[docs/standalone-deployment.md](../docs/standalone-deployment.md).

---

## Level 2 — with Postgres

```sh
make api-pg      # starts Postgres, then the API
```

Starts the two containers first (skipping any already up), waits for them to be
healthy, then runs the API against them. Same dev auth, but frame history,
groups, tasks and the org tables now exist.

Confirm you are really on level 2 — note that `/health/db` answers **200 either
way**, so read the body, not the status code:

```sh
curl -s localhost:8000/health/db
# level 1: {"status":"not_configured"}
# level 2: {"status":"ok","pools":[{"pool":"postgres-1","status":"ok"}]}

echo '\dt' | make -s psql        # the collab_ tables
```

The endpoints that need it answer `503` until you are on level 2:

```sh
curl -s localhost:8000/v1/frame-groups
# {"error":{"code":"groups_unavailable", ...}}
```

---

## Level 3 — with Keycloak

```sh
make api-oidc    # starts Postgres and Keycloak, then the API
```

Three containers, started for you and waited on before the API launches. The
first run pulls the Keycloak image and imports the realm, which is where the
~2 minutes goes; later runs reuse the running container.

The dev-auth shortcut is **off**. The API verifies real RS256 tokens against
the realm's JWKS, checking issuer and audience:

```sh
FRAMES_UNSAFE_AUTH_ENABLED=true
FRAMES_BEARER_JWKS_URL=http://localhost:8080/realms/nebari/protocol/openid-connect/certs
FRAMES_BEARER_ISSUER=http://localhost:8080/realms/nebari
FRAMES_BEARER_AUDIENCE=apollo-desktop
FRAMES_AUTH_IDENTITY_CLAIM=sub
```

`FRAMES_UNSAFE_AUTH_ENABLED=true` is there only because the local Keycloak's
JWKS URL is plain `http`, which the API otherwise refuses at startup — JWKS
URLs must be `https` outside local development (issue #77). It does not turn
the dev-auth shortcut on (that also needs `DEV_AUTH_ENABLED`), and it does not
accept unsigned tokens (that also needs `FRAMES_BEARER_ALLOW_UNSIGNED`).

Get a token and use it:

```sh
make token                                   # prints an access token for dev/dev
TOKEN=$(make -s token)
curl -s -H "Authorization: Bearer $TOKEN" localhost:8000/v1/frames
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/v1/frames   # 401 without one
```

`FRAMES_AUTH_IDENTITY_CLAIM=sub` matters more than it looks. It pins the ACL
principal to the OIDC subject, which is what a real deployment does. Leave it
unset and the legacy precedence applies (`preferred_username`, then `email`,
then `sub`), so frames end up owned by a mutable username and the membership
rows you seed by subject match nobody.

```sh
make sub        # the OIDC subject for the same user
```

### Signing in from the CLI

The `collab-hub` CLI signs in the way the Collab desktop does: the realm's
`apollo-desktop` client, the authorization code flow with PKCE, and a redirect
back to a listener on `127.0.0.1`. `make cli` installs it into `cli/.venv`.

```sh
make cli
../cli/.venv/bin/collab-hub --hub http://127.0.0.1:8000 login
```

`login` opens the realm's sign-in page in your browser. **Sign in as `dev`,
password `dev`** — or `owner` / `owner`, the realm's second user (see
[What is in the realm](#2-what-is-in-the-realm)). The page then sends the
browser back to the CLI's listener, which says you can close the tab, and the
CLI prints who the hub signed you in as. Leave `login` running until then: the
listener lives only as long as the command, so a sign-in finished after it
stopped lands on a closed port.

```sh
../cli/.venv/bin/collab-hub whoami           # dev, its organization, and when the token expires
../cli/.venv/bin/collab-hub cog list         # the Cogs in the catalog
../cli/.venv/bin/collab-hub logout           # ends the realm session, revokes the token, deletes it here
```

With no browser on this machine, take a token from `make token` instead:
`make -s token | ../cli/.venv/bin/collab-hub --hub http://127.0.0.1:8000 login --with-token`.
At level 1 there is no realm: `login` says the hub runs dev auth, and `whoami`
says the session is unauthenticated. [`cli/README.md`](../cli/README.md) has
the rest.

Other level-3 targets:

| Target | Adds |
|---|---|
| `make api-fakes` | Connector routes wired to the fake Google, Slack and GitHub providers |
| `make api-full` | S3 frame storage, the web surface, and Keycloak-brokered connectors |
| `make api-membership` | Multi-tenant mode: organization resolved from `collab_org_members` |
| `make api-desktop` | Every origin on one port, so the [Collab client can sign in](#connecting-the-collab-desktop-client) |
| `make api-desktop-fakes` | The same, plus the fake connectors, so Collab shows them connected |

---

## Level 4 — the chart on kind

Levels 1–3 run the API as a process. Level 4 runs the **published artifact**:
the image built from `api/Dockerfile`, installed through `helm/collab-hub`. Use
it when your change touches the chart, the container, or anything that only
shows up across the Kubernetes boundary.

```sh
make kind-up        # create the cluster, build + load the image, helm install
make kind-forward   # in another shell — http://127.0.0.1:18080
make kind-logs
make kind-redeploy  # after an edit: rebuild, reload, roll the Deployment
make kind-down
```

`dev/values/kind.yaml` deliberately sets **neither** `api.nebariapp.enabled`
nor `api.ingress.enabled`. That keeps the chart's protection map unenforced,
which is what makes the dev-auth switches legal in `extraEnv`; with
`api.ingress.enabled: true` the chart refuses to render them at all. Reach the
API by port-forward rather than through a route.

`dev/values/kind.yaml` also sets the dev-auth trio, so the port-forwarded API
takes no token — the same as level 1:

```sh
make kind-forward                                   # in one shell
curl -s localhost:18080/v1/frames                   # no Authorization header
```

To exercise the connector and task paths against the chart boundary, with
Postgres and the fakes deployed into their own namespace:

```sh
make kind-smoke
```

`kind-smoke` installs its own release in its own namespace, so it neither
touches nor needs the one `make kind-up` created.

It also cannot escape the kind cluster. The script it runs calls plain
`kubectl` and `helm`, which would otherwise follow whatever context your
kubeconfig points at — and it creates namespaces, secrets and a Helm release
whose values turn on `FRAMES_UNSAFE_AUTH_ENABLED`. The target hands it a
kubeconfig holding only the kind context, so a shell left pointing at a shared
cluster cannot be deployed into by accident. If the cluster does not exist yet,
the target says so rather than falling back to your current context.

---

That is all four levels. Everything below is reference material for one topic
at a time — reach for a section when you hit it, not front to back.

---

## Keycloak, step by step

### 1. Start it

```sh
make keycloak
```

First run pulls the image and imports `dev/keycloak/realm-nebari.json`. When it
reports healthy:

- Realm: `nebari` — the name is **not** arbitrary: Apollo Desktop hardcodes it
- Users: `dev` / `dev` and `owner` / `owner`

To reset the realm to the checked-in definition after experimenting:

```sh
make realm-import     # deletes and recreates the realm
make broker-role      # re-run this afterwards, see step 3
```

### 1b. Sign in to the admin console

Most of what this guide needs is a `make` target, but the console is where you
go to look at a realm rather than change it — to see why a token lacks a claim,
what scopes an identity provider actually requested, or whether a user's
account link survived.

```sh
make keycloak-admin
```

It prints the URL and credentials and then proves them, by asking Keycloak for
an admin token rather than trusting that the defaults are still in force:

```
Admin console  http://localhost:8080
Username       admin
Password       admin
Sign-in        OK
```

Open <http://localhost:8080/admin/> and sign in with those. The realm switcher
is top-left; `nebari` is the one this pack uses, `master` only holds the admin
account itself.

While the [desktop front door](#connecting-the-collab-desktop-client) is up the
console is also reachable at <http://keycloak.localhost:9080/admin/>, which is
useful when you want to see the realm exactly as Collab reaches it — same
Keycloak, same session, different issuer.

**Changing the credentials.** `KC_ADMIN` and `KC_ADMIN_PW` set both what the
console expects and what `kcadm` sends:

```sh
make keycloak KC_ADMIN=root KC_ADMIN_PW=s3cret
```

With one catch worth knowing before you try it: Keycloak reads
`KC_BOOTSTRAP_ADMIN_*` **only against an empty database**. Since the realm now
lives in a volume, changing them on a Keycloak that has already started does
nothing until `make destroy` — which also discards every identity provider you
configured. `make keycloak-admin` fails with that explanation rather than
leaving you guessing at a login screen.

### 2. What is in the realm

| Client | Type | Purpose |
|---|---|---|
| `apollo-desktop` | public, PKCE S256, direct access grants on | The bearer-token client. The id is **baked into the Apollo Desktop binary** and cannot be renamed. Direct access grants are on only so `make token` works. |
| `collab-web` | confidential, secret `collab-web-dev-secret` | The authorization-code client for the server-rendered `/web` and `/admin` pages. |

`apollo-desktop` carries an **audience mapper** so its access tokens contain
`"aud": ["apollo-desktop", …]`, which is what `FRAMES_BEARER_AUDIENCE` checks.
The `collab-hub` CLI signs in with the same client and the same flow as the
desktop, so it needs no client of its own.

| User | Password | Email | Used for |
|---|---|---|---|
| `dev` | `dev` | `dev@example.com` | Every sign-in in this guide: the desktop, `collab-hub login`, `make token` (`KC_USER`/`KC_PASS` default to it) |
| `owner` | `owner` | `owner@example.com` | A second person, for sharing and membership: `make token KC_USER=owner KC_PASS=owner` |

These are development credentials, imported with the realm and good only on
this local Keycloak.

Both clients keep the realm-default client scopes, **including `basic`**. That
scope carries the `sub` mapper in Keycloak 25+; a client whose default scopes
omit it issues tokens with no `sub` claim at all, and the hub then authenticates
nobody once identity is pinned to `sub`. If you hand-edit client scopes in the
console, leave `basic` in place.

### 3. Grant the broker `read-token` role — required for every connector

Connectors work by the hub asking Keycloak for *this user's* stored provider
token. Keycloak refuses that unless the user holds the `broker` client's
`read-token` role.

```sh
make broker-role
```

This adds `read-token` to `default-roles-nebari`, so every realm user has it.
Skip it and every connector reports `unavailable` with a message naming the
missing role — and re-running provider consent will not fix it.

### 4. How connector brokering actually works

```mermaid
sequenceDiagram
    autonumber
    participant C as caller (curl / Apollo)
    participant H as Collab Hub API
    participant K as Keycloak broker
    participant P as Google / Slack / GitHub

    C->>H: GET /v1/connectors/gmail/search<br/>Authorization: Bearer <hub token>
    H->>K: GET /realms/nebari/broker/google/token<br/>(forwards the caller's bearer)
    K-->>H: the caller's stored Google access token
    H->>P: read-only provider call
    P-->>H: provider data
    H-->>C: bounded, normalized response
    Note over C,H: the provider token never leaves the hub
```

Two consequences worth internalizing:

- **The dev-auth shortcut cannot reach connectors.** It authenticates a subject
  without ever producing a token to forward, so the broker call has nothing to
  send. Connector work needs level 3. `make api-fakes` is set up that way.
- **A connector reporting `connected` proves the provider call worked**, not
  merely that Keycloak returned something. The Gmail, Calendar and Slack status
  routes make a real bounded provider request.

### 5. Google — Drive, Gmail and Calendar

One identity provider serves all three connectors.

**In Google Cloud Console:**

1. Create (or pick) a project and enable the APIs you need:
   `drive.googleapis.com`, `gmail.googleapis.com`, `calendar-json.googleapis.com`.
   Adding a scope in Keycloak is *not* sufficient when the API is disabled for
   the project.
2. Configure the OAuth consent screen. While it is in **Testing**, add yourself
   under **Test users** or consent will be refused.
3. Create an **OAuth client ID** of type *Web application*.
4. Add the authorized redirect URI — Google accepts `http` for `localhost`:

   ```
   http://localhost:8080/realms/nebari/broker/google/endpoint
   ```

5. Copy the client ID and secret.

**In Keycloak:**

```sh
make idp-google \
  GOOGLE_CLIENT_ID=<id> \
  GOOGLE_CLIENT_SECRET=<secret>
```

That creates an identity provider with `alias=google`, `storeToken=true`,
`offlineAccess=true`, `prompt=consent`, and the three read-only scopes:

```
https://www.googleapis.com/auth/drive.readonly
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/calendar.readonly
```

`storeToken` is what makes `/broker/google/token` return anything; offline
access and the consent prompt are what make Google issue a refresh token.

**Link a user.** Sign in to the account console as `dev`, open **Account
security → Linked accounts**, and link Google:

<http://localhost:8080/realms/nebari/account/#/account-security/linked-accounts>

**Verify:**

```sh
make api-full      # in one shell
TOKEN=$(make -s token)
for c in google-drive gmail google-calendar; do
  curl -s -H "Authorization: Bearer $TOKEN" localhost:8000/v1/connectors/$c/status
  echo
done
```

Expect `"state":"connected"`. `reconnect_required` means the stored token
cannot make that provider call — usually a missing scope, or a user who linked
before the scope was added. **Adding a scope does not re-prompt already-linked
users**: they must unlink and relink.

### 6. GitHub

**On GitHub** (*Settings → Developer settings → OAuth Apps → New OAuth App*):

1. Authorization callback URL:

   ```
   http://localhost:8080/realms/nebari/broker/github/endpoint
   ```

2. Copy the client ID and generate a client secret.

**In Keycloak:**

```sh
make idp-github \
  GITHUB_CLIENT_ID=<id> \
  GITHUB_CLIENT_SECRET=<secret>
```

Scopes: `user:email repo read:org read:project`. Two things to know:

- Keycloak's **Scopes** field *replaces* the provider defaults rather than
  appending to them, so `user:email` must be listed explicitly or the account
  email mapper breaks.
- GitHub has **no read-only scope for private repositories** — `repo` is the
  floor and it is read *and* write capable. Read-only is enforced in Collab Hub's
  client code, not by the token. If you only need public repositories, drop
  `repo` and the token has no write capability by construction.

The IdP is created with `hideOnLogin=true` so nobody can *sign in* with GitHub
(which would create a GitHub-primary account through first-broker-login).
GitHub is only ever linked as a secondary identity.

For an org with OAuth App access restrictions, an org owner must approve the
app before its repositories are visible — until then `/status` is green and org
repositories return 404.

### 7. Slack

Slack is the one connector without a straightforward local path, for two
reasons worth knowing before you start:

1. **Keycloak has no built-in Slack identity provider**, and the connector needs
   a specific, non-standard behavior: Slack's `oauth.v2.access` returns the user
   token nested under `authed_user.access_token` (an `xoxp-…` value), not at the
   top level. A stock OIDC provider stores the top-level identity token instead,
   linking appears to succeed, and then every read fails with `invalid_auth`.
2. **Slack requires HTTPS redirect URLs**, so a `http://localhost:8080` broker
   endpoint cannot be registered without putting a public HTTPS tunnel in front
   of your Keycloak.

So for local work, use one of the two alternatives below. Reserve real Slack
linking for a deployed hub, and validate it there with
[docs/slack-connector.md](../docs/slack-connector.md#verifying-the-brokered-token).

### 8. Alternative — the fake providers

The fastest way to exercise every connector route, with no OAuth app anywhere:

```sh
make api-fakes
TOKEN=$(make -s token)

for c in google-drive gmail google-calendar slack github; do
  printf '%-17s ' "$c"
  curl -s -H "Authorization: Bearer $TOKEN" localhost:8000/v1/connectors/$c/status
  echo
done
```

All five report `"state":"connected"` with realistic scope lists. The fakes
serve fixed fixtures, so searches and reads return stable data — which is what
makes them useful in tests.

Every GitHub route answers, not only `status`:

```sh
B=localhost:8000/v1/connectors/github
post() { curl -s -X POST -H "Authorization: Bearer $TOKEN" \
           -H 'Content-Type: application/json' -d "$2" "$B/$1"; }

post search            '{"query":"orbits"}'
post items/42/read     '{"repo":"nebari-dev/collab-hub-pack"}'   # an issue
post items/43/read     '{"repo":"nebari-dev/collab-hub-pack"}'   # a pull request
post files/read        '{"repo":"nebari-dev/collab-hub-pack","path":"README.md"}'
post projects/list     '{"owner":"nebari-dev"}'
post projects/7/read   '{"owner":"nebari-dev"}'
```

### 9. Alternative — a static access token

Every connector accepts a static provider token, bypassing brokering entirely.
Useful for pointing at a *real* provider with a personal token:

No target sets these, so run the API by hand with the one you want — the
`env` line is what `make api-oidc` uses, plus your token:

```sh
cd ../api && env \
  COLLAB_HUB_API__SERVER__PORT=8000 \
  FRAMES_UNSAFE_AUTH_ENABLED=true \
  FRAMES_BEARER_JWKS_URL=http://localhost:8080/realms/nebari/protocol/openid-connect/certs \
  FRAMES_BEARER_ISSUER=http://localhost:8080/realms/nebari \
  FRAMES_BEARER_AUDIENCE=apollo-desktop \
  FRAMES_AUTH_IDENTITY_CLAIM=sub \
  FRAMES_AUTH_DEFAULT_ORG=dev-org \
  COLLAB_HUB_API__CONNECTORS__GITHUB__STATIC_ACCESS_TOKEN=ghp_… \
  uv run python -m collab_hub_api
```

The other two are `…__CONNECTORS__SLACK__STATIC_ACCESS_TOKEN` (`xoxp-…`) and
`…__CONNECTORS__GOOGLE__STATIC_ACCESS_TOKEN` (`ya29.…`). Reach for this when
you want a connector pointed at the *real* provider; the fakes cover all five
otherwise.

A static token takes precedence over the broker URL when both are set. That is exactly why
it must never appear in deployed values: it would silently make every user of
the deployment act as the token's owner.

---

## Organizations, membership and roles

The API resolves a caller's organization one of two ways.

### `claims` (the default here)

`FRAMES_AUTH_DEFAULT_ORG` / `FRAMES_AUTH_DEFAULT_WORKSPACE` put every
authenticated user in one organization. No database needed. **Every target
except `make api-membership` uses this**, so it is what you get unless you go
looking for the other one.

### `membership` — the multi-tenant model

`FRAMES_AUTH_ORG_SOURCE=membership` makes the server own the model: the
caller's row in `collab_org_members` decides their organization and role, org
claims in the token are ignored, and an authenticated user with **no active
membership is answered 403 `no_organization`** rather than being placed
somewhere. It requires Postgres, `FRAMES_AUTH_IDENTITY_CLAIM=sub`, and both
defaults left empty — the API refuses to start otherwise.

```sh
make api-membership                                    # in one shell
make seed-org SUB=$(make -s sub) EMAIL=dev@example.com # in another
```

`seed-org` runs `dev/sql/bootstrap.sql`, which in one transaction creates
`dev-org`, makes that subject its **owner**, grants it the platform
**`operator`** role, and writes the matching `operator.manual` audit row.

Platform roles are deliberately **not** Keycloak roles: Keycloak authenticates,
this server authorizes, and the grant lives in `collab_platform_roles`. That is
why becoming an operator locally is an `INSERT`, not a realm edit.

Verify the split:

```sh
TOKEN=$(make -s token)                                  # dev — seeded
curl -s -H "Authorization: Bearer $TOKEN" localhost:8000/v1/frames

T2=$(make -s token KC_USER=owner KC_PASS=owner)         # owner — not seeded
curl -s -H "Authorization: Bearer $T2" localhost:8000/v1/frames
# {"error":{"code":"no_organization", …}}  HTTP 403
```

---

## The web surface

The server-rendered pages under `/web` and `/admin` authenticate people in
browsers on a **separate axis** from the API. A web session cookie presented to
`/v1/*` answers 401, and a bearer token presented to a page does not sign the
browser in. Neither substitutes for the other.

```sh
make api-full
open http://localhost:8000/web/signin
```

You are redirected to Keycloak for the authorization-code flow with the
confidential `collab-web` client, and the app issues its own session cookie
after verifying the ID token. Sign in as `dev` / `dev`.

This works on a non-default `API_PORT` too. The realm lists both
`http://localhost:8000/web/oidc/callback` and a bare `http://localhost:*`,
because Keycloak honours a wildcard only at the **end** of a redirect URI —
`http://localhost:*/web/oidc/callback` is rejected outright with
`Invalid parameter: redirect_uri`. The wide entry is a local-dev realm
convenience and has no counterpart in a deployed one, where the callback is a
single exact URI.

`/admin/invitations` additionally requires the platform `operator` role — see
[`make seed-org`](#membership--the-multi-tenant-model) above.

The dev session secret in the Makefile is a local value. The real constraint is
enforced at startup: at least 32 characters and at least 16 distinct ones.

---

## Connecting the Collab desktop client

### The Hub address for each target

Collab asks for one thing — a **Hub address** — and derives everything else
from it. Only one target here is set up to answer at all four derived origins:

| Target | Hub address to enter | |
|---|---|---|
| `make api-desktop` | **`http://localhost:9080`** | ✅ the one to use |
| `make api-desktop-fakes` | **`http://localhost:9080`** | ✅ same, plus working (fake) connectors |
| `make api` | — | No Keycloak running, and Collab's sign-in is OAuth-only: no API key, no token to paste, no bypass |
| `make api-pg` | — | Same |
| `make api-oidc` | — | Keycloak runs, but on a different port from the hub |
| `make api-fakes` | — | Same |
| `make api-full` | — | Same |
| `make api-membership` | — | Same |
| `make kind-up` | — | Port-forward only, and no Keycloak in the cluster |

So:

```sh
# once per machine
echo '127.0.0.1 keycloak.localhost frames.localhost llm-internal.localhost' \
  | sudo tee -a /etc/hosts

make api-desktop        # starts Postgres, Keycloak, the front door, and the API
```

Then in Collab: **Hub address → `http://localhost:9080` → Sign in**, as
`dev` / `dev`.

**Type the `http://` — it is not optional.** Collab prepends `https://` to an
address given without a scheme, and for a local host it then keeps whatever
scheme it ended up with. So both `localhost:9080` and `https://localhost:9080`
build an `https://keycloak.localhost:9080/…` issuer, and the front door speaks
plaintext HTTP, so sign-in fails with `OIDC discovery: discovery request
failed`. Only an explicit `http://` reaches it. Use `localhost`, not
`127.0.0.1`: the client prefixes the IdP label, and `keycloak.127.0.0.1` is not
a resolvable name.

Verify the plumbing before you blame the client:

```sh
make desktop-check
```

### Why one port, and why those hostnames

Collab never asks for the issuer, the Frames URL, or the inference URL. It
derives all of them from what you type, by surgery on the hostname
(`frontend/src/api/hub.ts` → `internal/hubauth/proxy_policy.go`):

```
Hub address you type      http://localhost:9080
        ↓  buildHubIssuerUrl: prefix "keycloak.", append /realms/nebari
issuer                    http://keycloak.localhost:9080/realms/nebari
        ↓  ProxyRequestPolicy.TargetURL: strip "keycloak.", apply each prefix
hub API                   http://localhost:9080
Frames + connectors       http://frames.localhost:9080
internal LLM              http://llm-internal.localhost:9080
```

Three consequences, each of which decided something in this setup:

- **The port is shared.** Every derived origin keeps the issuer's port, so
  Keycloak and the hub cannot sit on 8080 and 8000 as they do at levels 1–3.
  `make hub-proxy` runs a Caddy container ([`proxy/Caddyfile`](proxy/Caddyfile))
  that routes by `Host` on port 9080: `keycloak.localhost` to Keycloak,
  everything else to the API on your machine. It also rewrites `/healthz` to
  `/health`, which is the path the client's health probe asks for.
- **The realm name is fixed.** `HUB_OIDC_REALM` is `nebari` in the client, so
  the dev realm is `nebari` — not a naming preference.
- **The client id is fixed.** `apollo-desktop` is baked into the binary and is
  not configurable from the frontend or from per-hub configuration.

`http` is accepted only because the client treats `localhost` **and any
`*.localhost` name** as loopback (`isLoopbackHost`). Any other hostname would
be forced to `https`.

### The trap: entering `localhost:8080`

Typing Keycloak's own port looks like it should work, and sign-in may even
succeed — `keycloak.localhost:8080` reaches Keycloak. But the client then
derives the hub API as `http://localhost:8080` and Frames as
`http://frames.localhost:8080`, which are *also* Keycloak. Every Frames and
connector call lands on the identity provider and 404s.

Enter `http://localhost:9080`, the front door.

### What `api-desktop` does differently

| | Levels 1–3 | `make api-desktop` |
|---|---|---|
| Bind address | `127.0.0.1` | `0.0.0.0` — the proxy container reaches your machine through the docker gateway, which a loopback-only socket will not answer on |
| `FRAMES_BEARER_ISSUER` | `http://localhost:8080/realms/nebari` | `http://keycloak.localhost:9080/realms/nebari` |
| Keycloak issuer | derived from the request | same — the realm answers correctly on both entry points |

The issuer must be the origin the token was **actually minted on**. Collab
mints through `keycloak.localhost:9080`, so that is the `iss` its tokens carry,
and the API compares it exactly. `make token` still works against
`localhost:8080`, but those tokens carry the other issuer and `api-desktop`
will refuse them — use `make token KC_URL=http://keycloak.localhost:9080`
if you want a matching one on the command line.

`make hub-proxy-down` stops the front door; Keycloak stays on `localhost:8080`
for the other levels.

### What works once connected

Frames and the user directory work immediately. Three things do not:

- **Connectors are unconfigured** by `api-desktop`, so Collab shows them as not
  connected. Use `make api-desktop-fakes` instead to have all five — Drive,
  Gmail, Calendar, Slack and GitHub — report `connected` against the fake
  providers, or configure real identity providers per
  [Keycloak, step by step](#keycloak-step-by-step).
- **Internal LLM inference** (`llm-internal.localhost:9080`) has nothing behind
  it; this pack does not serve models. That is the `llm-serving-pack`'s job.
- **Slack linking** needs an HTTPS broker endpoint — see
  [Slack](#7-slack) above.

---

## Running Cogs and Ops

A Cog runs through the hub at level 1: `make api` serves the run API
(`/v1/runs`, over the SQLite Track in `.local/`), `make controller` advances
the runs it accepts, and the `collab-hub` CLI launches them. This section is where the rest arrives, one target at a time, under the rule
[ADR-0002](../docs/adr/0002-lifecycle-runner-durability-and-placement.md) D9
sets for every Cog execution change. In the same PR, what a change adds is:

1. **runnable from here**, at the lowest level that can host it, through a
   `make` target;
2. **documented in this section** — the target, the level, what persists, and a
   [troubleshooting](#troubleshooting) row for its first failure mode;
3. **asserted in CI at that level** — no containers at level 1, no large image
   downloads, and real clusters and heavy engines in their own workflows, never
   in `dev-env.yaml` (see [What CI checks](#what-ci-checks)).

Two things already run outside the hub. They predate this rule, so they are
run directly rather than through `make`, and CI covers them in
`test-execution.yaml` and `test-execution-e2e.yaml`:

| What | Command | Needs |
|---|---|---|
| The execution package's tests | `cd execution && uv run --group test pytest` | nothing — the Postgres Track tests skip unless `TEST_POSTGRES_URL` is set |
| A gated two-step Op on real worker pods, through `KubernetesCogExecutor` | `scripts/cog-execution-e2e/run.sh`, from the repository root | Docker, kind, `kubectl` |

**Point `TEST_POSTGRES_URL` at a database of its own.** The Postgres Track tests
drop and recreate `collab_track_events`. With level 2 up, `make psql`, run
`CREATE DATABASE execution_test;`, then:

```sh
cd execution
TEST_POSTGRES_URL=postgresql://collab:collab@127.0.0.1:5432/execution_test \
  uv run --group test pytest
```

**The E2E script brings its own cluster.** It creates a kind cluster named
`cog-e2e` — not level 4's `collab-hub-dev` — builds and loads the test image,
runs the Op, and deletes the cluster afterwards unless `KEEP=1` is set.

### A Cog launched from the CLI — `make api`, `make controller`

Level 1, no container. Two processes over one SQLite Track,
`.local/track.sqlite`:

```sh
make api          # terminal 1: the API, with /v1/runs on
make controller   # terminal 2: starts each submitted run's worker as a local process (needs pixi)
cd ../cli && uv run collab-hub --hub http://localhost:8000 cog launch hello --input '{"name": "Ada"}' --watch
```

The API records the submission and nothing else; the controller reads it from
the Track, runs the Cog package's `serve` task in its own pixi environment, and
records what happened; the CLI reads the run back through the API. `run list`,
`run show` and `run terminate` are the other commands.
`make api-oidc` serves the run API too, with real sign-in through Keycloak, on
the hub's own Postgres, so its controller is `make controller
TRACK=postgresql://collab:collab@127.0.0.1:5432/collab`. [`examples/cog-local`](../examples/cog-local/README.md)
walks a Cog through its whole life on them — sign in, launch, list, talk to it
from Toad over ACP, stop it — and `make -C ../examples/cog-local demo` runs it
end to end and checks it.

The controller sets no limit on how long an interaction takes, since a Cog
holding a session stays in one for as long as someone talks to it. A worker
that hangs is stopped by terminating its run.

Cog packages are found in `COGS`, a `:`-separated list that defaults to
`dev/cogs` (the fake Cogs), `cogs` at the root (`hermes`, the Hermes harness
Cog) and `examples/cog-local/cogs` (`hello`); both targets read it, so set it
on both to add a directory of your own.

**The Hermes Cog's model.** `make controller` first writes the hub's models
block, `.local/models.toml` (`make models`), from `COLLAB_MODEL_PROVIDER`
(`openai-compatible`, or `anthropic` for Claude), `COLLAB_MODEL_BASE_URL`,
`COLLAB_MODEL_NAME` and `COLLAB_MODEL_API_KEY`, and binds the Hermes Cog to it;
the block names the variable the key is in, never the key. The controller hands
that model to the Hermes Cog's workers, and no other's
([runs.md](../docs/cog-execution/runs.md#configuration-health-and-models)). By default it is `make fake-model`, an OpenAI-compatible
endpoint on port 8090 that answers `The fake model heard: ...` with no account
and no network ([`fake-model/fake_model.py`](fake-model/fake_model.py)). Set
the three in your environment to use a real one. Hermes's environment is about
400 MB, installed by pixi on its first launch, or ahead of time with
`make -C ../examples/cog-local env`. A worker's
output is under `.local/runs/`.

**What persists.** The Track file, until `make clean`. The controller keeps
nothing else: stop it mid-run, `kill -9` included, and its next start records
that run `interrupted`.

### The run API and run pickup — `make api`, `make controller`, `make submit`

Level 1, no container; level 2 for Postgres. `make submit OP=<name>` launches an
Op from `ops/` through the run API (`POST /v1/runs` on `make api`), as any
client does, then follows it until it ends or waits at a Gate, prints its
events, and says which controller picked it up. `make controller` picks it up,
and `make run-events RUN=` streams a run's events as they land
(`GET /v1/runs/{id}/events`, server-sent events) until it ends:

```sh
make api                                            # terminal 1: the run API, over .local/track.sqlite
make controller ENVIRONMENT=host HEALTH_PORT=8770   # terminal 2; ENVIRONMENT=host needs no pixi
make submit OP=echo                                 # terminal 3: "picked up by: dev", COMPLETED
make run-events RUN=run-...                         # every event of the run, then `end`
curl -s localhost:8770/readyz                       # {"controller": "dev", "live": true, "ready": true, ...}
```

At level 2, `make api-pg` serves the run API on the hub's own Postgres, as a
deployed hub does: its Track's tables come from the hub's migrations. Run the
controller on the same database, `make controller TRACK=postgresql://collab:collab@127.0.0.1:5432/collab`.

| Variable | Default | What |
|---|---|---|
| `TRACK` | `.local/track.sqlite` | the controller's Track: the SQLite file `make api` uses, or `postgresql://...`, the database `make api-pg` uses (its migrations create the Track's tables) |
| `CONTROLLER_ID` | `dev` | the controller's name, held while it runs, recorded on each run it picks up |
| `ENVIRONMENT` | `pixi` | `host` runs a package's `serve` task directly, without its pixi environment |
| `HEALTH_PORT` | none | serves `/healthz` and `/readyz` |

Several controllers share one Track, each under its own `CONTROLLER_ID`, and
each run is picked up by one of them:

```sh
make controller ENVIRONMENT=host CONTROLLER_ID=one &
make controller ENVIRONMENT=host CONTROLLER_ID=two &
for n in 1 2 3 4; do make submit OP=echo & done; wait   # "picked up by: one" or "two", once each
```

A controller owns the runs it picked up: it alone advances them, and delivers
their cancels, turns and Gate decisions. Kill one mid-run (`make submit OP=slow`,
then `kill -9` it), and its run stays `RUNNING` — another controller leaves it
alone — until that controller starts again under the same id, which records it
`interrupted` and reaps any of its workers left alive. A second controller under
an id already running refuses to start. `spender` declares a budget, which is the
runner's, so it runs with `make op` only.

### Fake Ops — `make op`

Level 1, no container. `make op OP=<name>` runs the Op in `ops/<name>.yaml` on
the lifecycle runner with the `none` durability backend, and prints its Track
and its status. Each step names a fake Cog under `cogs/`: a Cog package, with a
`pixi.toml` that declares a `serve` task and a `serve.py` that holds the Cog's
`handle` function. Without a location, `handle` answers in the same process:

| Op | Fake Cog | Ends |
|---|---|---|
| `echo` (the default) | `echo` answers with its input | `COMPLETED`, two steps |
| `needs-review` | `needs-review` answers with an `error` problem, so the step's default Gate escalates | `WAITING_AT_GATE` |
| `fails` | `fails` answers `ok: false` | `FAILED`, with the code on `step_failed` |
| `spender` | `spender` reports 100 tokens per step, under a 150-token budget | `BUDGET_EXCEEDED` after the second step |
| `slow` | `slow` takes 30 seconds | stop it with Ctrl-C: the next `make op` reports it `interrupted` |

```sh
make op OP=needs-review
make op OP=slow        # Ctrl-C during the step
make op                # "interrupted slow-…: a previous `make op` stopped before it ended"
```

**As real worker processes — `LOCATION=local`.** With `make op OP=<name>
LOCATION=local`, each step's worker is a process: the local executor runs the
package's `serve` task in the package's own pixi environment, on a loopback
port it chose, waits for `/healthz`, calls `/invoke` with the worker's run
token, and kills the worker's process group when the step ends. This is the
`local` agent location ([runs.md](../docs/cog-execution/runs.md#agent-locations));
it needs [pixi](https://pixi.sh), and nothing else here does. The first run of
a Cog installs its environment (a Python, a few seconds); later runs reuse it.

```sh
make op OP=echo LOCATION=local    # worker_started … location=local pid=…, then worker_stopped
make op OP=slow LOCATION=local &  # then kill -9 the `op.py` process: no worker is left behind
```

The Track gains `worker_started` and `worker_stopped` around each interaction.
A worker's stdout and stderr are under `.local/runs/`, in the directory its
`worker_started` names as `logs` (`<run>-<digest>/<step>_<attempt>-<digest>/`).
Stopping the host any way at all, `kill -9` included, leaves no worker: the
launcher that holds each worker kills it when its pipe to the host closes.

**What persists.** The Track is a SQLite file, `.local/track.sqlite`, kept
across runs until `make clean`. Every `make op` starts the way a controller
does, as the controller `make-op`: a run a previous one left running or waiting
at a Gate is recorded `interrupted`, since `none` keeps nothing across a restart
([runs.md](../docs/cog-execution/runs.md)), and the runs of any other
controller on the Track are left alone. That includes a `needs-review` run
still waiting at its Gate: nothing here decides it yet (the run API, #103, does).

**One `make op` at a time.** Each is the controller `make-op`, which holds its
name while it runs, so a second one refuses to start while the first runs. Run
`make op` with no `make controller` on the same Track: a controller passing over
it could pick up the run `make op` just recorded; `make submit` is the way to
hand a controller an Op. `BACKEND=dbos` and
`BACKEND=temporal` are refused until those backends are built, and so is
`LOCATION=remote` until Phase 21 puts the cluster executor behind the switch.
The fake Cogs' pixi environments, `cogs/<name>/.pixi/`, are git-ignored and
survive `make clean`; delete them to install afresh.

### The Cog catalog read API

The read API (`/v1/cogs`, [docs/cog-registry.md](../docs/cog-registry.md#read-api))
runs at **level 1**: `make api` keeps the catalog in process memory
(`COLLAB_HUB_API__COGS__CATALOG__BACKEND=memory`), so every route answers,
over an empty catalog that resets when the process stops. From level 2 up the
catalog is the `collab_cog_artifacts` table in Postgres and survives restarts
(`make destroy` clears it). Nothing indexes into it unless a registry source
and `cogs.index.enabled` are configured — see
[docs/cog-registry.md](../docs/cog-registry.md#bare-process).

```sh
make api
curl -s localhost:8000/v1/cogs                    # {"items":[],"limit":50,"offset":0,"next_offset":null}
curl -s 'localhost:8000/v1/cogs?kind=model&q=small'
curl -s localhost:8000/v1/cogs/catalog.v1.json
```

CI asserts both empty answers at level 1, and at level 2 runs the filtered
list against Postgres.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Every route answers 401 on level 1 | Only `DEV_AUTH_USER` is set | All three switches are required — use `make api` |
| 401 with a token that looks fine | Issuer mismatch, or the token has no `sub` claim | Check `iss` matches `FRAMES_BEARER_ISSUER` exactly; check the client kept the `basic` client scope |
| `403 no_organization` | Membership mode with no row for that subject | `make seed-org SUB=$(make -s sub)` |
| `503` from history / groups / invitations | No Postgres — `make api` starts none | Use `make api-pg` or above |
| `503 cog_catalog_unavailable` from `/v1/cogs` | The API was started by hand with neither Postgres nor the level-1 memory override | Use `make api`, or set `COLLAB_HUB_API__COGS__CATALOG__BACKEND=memory` |
| `/web/signin` returns 404 | The web surface is not mounted without a Keycloak client id | Use `make api-full` |
| `/health/db` says 200 but nothing persists | It answers 200 either way; the body says `not_configured` | Read the body, and use level 2 or above |
| Connector says `unavailable`, names a missing role | The broker `read-token` role was never granted | `make broker-role` |
| `make op LOCATION=local` says to install pixi | The `local` location runs each Cog in its own pixi environment | Install [pixi](https://pixi.sh), or drop `LOCATION` to run the fake Cogs in process |
| `make op LOCATION=local` fails a step with `WorkerStartFailed` | The worker exited or never answered `/healthz`; the reason on the Track ends with its last lines of stderr | Read `stderr.log` in the directory the run's `worker_started` names as `logs`, under `.local/runs/` |
| `collab-hub cog launch` answers 503 `runs_unavailable` | The API has no Track: no database and no `runs.track_path` | `make api`, which sets the SQLite one, or `make api-pg`, which uses Postgres |
| `collab-hub cog launch` is refused, naming no Cog it can launch | The API was given no Cog package directories (`runs.packages`) | `make api` and `make api-pg` give it `COGS`; another target needs `COLLAB_HUB_API__RUNS__PACKAGES` |
| A launched run stays `SUBMITTED` | No controller is watching the Track | `make controller` in another terminal |
| `make controller` says a controller named `dev` is running | One controller per id on a Track | Stop the other one, or give this one its own `CONTROLLER_ID` |
| A run stays `RUNNING` after its controller was killed | Its controller owns it, and records it `interrupted` only when it starts again | Start that controller again, under the same `CONTROLLER_ID` |
| `make controller TRACK=postgresql://...` says this Postgres has no Track | The Track's tables come from the hub's migrations | Run `make api-pg` once against that database |
| `make submit OP=spender` refuses | A budget is the runner's, not the Op's | `make op OP=spender` |
| `make op` says another `make op` is running | A previous one is still running on the same Track, possibly in another terminal | Let it finish or stop it; one host per Track until run pickup (#121) |
| Connector says `reconnect_required` | Stored token cannot make that provider call | Add the scope to the IdP, then **unlink and relink** the user |
| Connector status needs "a Hub bearer token" | Called with dev auth | Connectors need level 3 — use `make api-fakes` or `make api-oidc` |
| Keycloak healthy but the realm is missing | Import only runs on first start | `make realm-import`, then `make broker-role` |
| Admin console rejects `admin` / `admin` | The credentials were changed after the database existed, so the new ones were never seeded | `make keycloak-admin` says so; `make destroy` re-seeds, at the cost of every configured identity provider |
| Port already in use | Something else on 8000/8080/8081/8082/8083/5432/9000/9080 | Override, e.g. `make api API_PORT=8010` |
| `Invalid parameter: redirect_uri` signing in to `/web` | The realm was edited and lost its `http://localhost:*` entry | `make realm-import`, then `make broker-role` |
| Collab has no Hub address that works | You are on `make api`/`api-pg`/`api-oidc`; the client needs all origins on one port | `make api-desktop`, then Hub address `http://localhost:9080` |
| Collab: "Could not complete sign-in" | `/etc/hosts` entries missing, or the front door is down | `make hosts-check`, then `make desktop-check` |
| Collab signs in but Frames are empty or error | Hub address was `localhost:8080` — every call landed on Keycloak | Enter `http://localhost:9080` |
| Collab: `OIDC discovery: discovery request failed` | The Hub address had no scheme, or `https://` — either builds an `https://` issuer the plaintext front door cannot answer | Enter `http://localhost:9080`, scheme included |
| `api-desktop` answers 401 to a `make token` token | That token's `iss` is `localhost:8080` | `make token KC_URL=http://keycloak.localhost:9080` |
| Front door returns 502 | The API is not running, or is bound to loopback only | `make api-desktop` binds `0.0.0.0` for exactly this |
| Stale data after schema changes | Every volume survives `make down` | `make destroy` — see [What persists](#what-persists-and-what-resets) |
| A hand-configured identity provider vanished | `make destroy` erases the Keycloak volume too | Re-run `make idp-google` / `make idp-github`; prefer `make down` |
| `make lint` fails with `kubeconform: not found` | It is a separate binary | Install [kubeconform](https://github.com/yannh/kubeconform) |

Useful when something is off:

```sh
make ps       # what is running
make logs     # follow every supporting service
make psql     # inspect the database directly
```

---

## Checks before you push

```sh
make test     # the API test suite (pytest, with coverage)
make lint     # helm lint + kubeconform on the rendered manifests
```

---

## What CI checks

Nothing used to check that `dev/` still runs the pack. The test workflow is
pytest in `api/` and the lint workflow renders the chart, so a broken
local-development setup reached contributors before it reached CI — the review
of the change that introduced this directory turned up five such regressions.
[`.github/workflows/dev-env.yaml`](../.github/workflows/dev-env.yaml) closes
that gap.

| Level | Where | What it asserts |
|---|---|---|
| 1 | Linux **and macOS** | `hosts-check` both ways, then a frame written and read back with no token, and the empty Cog catalog (`/v1/cogs`, `/v1/cogs/catalog.v1.json`); every fake Op of `make op` ends as it should, and a run whose host is killed mid-step is reported `interrupted` by the next; `make submit` hands the fake Ops to `make controller`, another process, which picks each up and ends it as it should, and a controller killed with `SIGKILL` mid-run records the run `interrupted` when it starts again; with `LOCATION=local`, `echo` runs as real worker processes in its pixi environment, and a host killed with `SIGKILL` mid-step leaves no worker |
| 2 | Linux | `/health/db` reports a real database, `/v1/frame-groups` answers 200 instead of 503, and a `/v1/cogs` list with every filter answers 200 from Postgres; two controllers on one Postgres Track complete eight runs submitted at once, and none is picked up twice |
| 3 | Linux | 401 without a bearer, 200 with one, and the token carries a `sub` |
| 4 | Linux | Rendered only — the chart, the dev-auth switches, the `IMAGE` override and the port overrides |

The examples have a workflow of their own,
[`.github/workflows/examples.yaml`](../.github/workflows/examples.yaml), run on
pull requests that touch what they use: `examples/cog-local`'s `make demo`
starts Postgres and Keycloak, signs in with the CLI, launches the Hermes Cog
against the fake model, talks to it over ACP and with `run say`, and
terminates it with no worker left. It is apart from this one because Hermes's
environment is about 400 MB.

**Coverage relaxes as the levels get more expensive**, which is how the levels
are meant to be used in the first place.

**Level 1 runs on macOS as well as Linux.** GitHub's macOS runners have no
Docker, and level 1 is the level that needs none — so the split is not a
compromise. It is also the right place to spend the macOS minutes, because
macOS is where the portability bugs live: `getent` is glibc-only, and a
`hosts-check` that relied on it reported every hostname missing there.

**Levels 2 and 3 share one Linux runner**, so level 3 reuses level 2's
containers rather than pulling them again.

**Level 4 is rendered rather than run.** A kind cluster pulls a ~1 GB node
image and builds the API image before it can report anything, while the
failures that actually recur there — values drift, an image override that never
reaches the install — are visible in the rendered manifests in seconds.

**The assertions are the contracts, not smoke.** Each one is a behaviour this
directory promises and has broken at least once: `/v1/frame-groups` answers 200
instead of 503 once Postgres is up; an unauthenticated request is 401 and an
authenticated one 200; the token carries a `sub`; the `IMAGE` override reaches
the Deployment; `DESKTOP_PORT` and `API_PORT` move together; and `hosts-check`
still refuses a name that does not resolve, so it cannot quietly degrade into a
check that always passes.

---

## Make target reference

Run `make help` for the current list. Common overrides — `API_PORT` and
`DESKTOP_PORT` move every part that derives from them (the API, the front
door's listener and published port, its upstream, and the issuer the desktop
client is told about), so `make api-desktop DESKTOP_PORT=9090 API_PORT=8010`
works as a set:

| Variable | Default | Meaning |
|---|---|---|
| `API_PORT` | `8000` | Port the API binds — also moves the front door's upstream |
| `KC_URL` | `http://localhost:8080` | Keycloak base URL |
| `REALM` | `nebari` | Realm name — Apollo Desktop hardcodes it |
| `KC_USER` / `KC_PASS` | `dev` / `dev` | Whose token `make token` prints |
| `PG_URL` | `postgresql://collab:collab@127.0.0.1:5432/collab` | Dev database |
| `KC_ADMIN` / `KC_ADMIN_PW` | `admin` / `admin` | Admin console **and** `kcadm`; only seeded against an empty database |
| `S3_ENDPOINT` | `http://127.0.0.1:9000` | The S3 frame store |
| `CLUSTER_NAME` | `collab-hub-dev` | kind cluster name |
| `NAMESPACE` / `RELEASE` | `collab-hub` | Namespace and Helm release for `make kind-up` |
| `IMAGE` | `collab-hub-api:dev` | Image `make kind-image` builds and loads, and `make kind-up` deploys |
| `FORWARD_PORT` | `18080` | Local port for `make kind-forward` |
| `DESKTOP_PORT` | `9080` | Single-port front door for the Collab client — also its listener and published port |
| `OP` | `echo` | The Op `make op` runs: a file under `ops/`, without `.yaml` |
| `BACKEND` | `none` | The durability backend `make op` runs on; only `none` is built |
| `COGS` | `dev/cogs:cogs:examples/cog-local/cogs` | The directories `make api` and `make controller` find Cog packages in, `:`-separated |
| `LOCATION` | *(empty)* | Where `make op` runs each step's worker: empty for in process, `local` for a real process per worker (needs pixi) |

## Files in this directory

| Path | What it is |
|---|---|
| `Makefile` | Every target described above |
| `compose.yaml` | Postgres, the S3 store, Keycloak, the two fake providers, and the desktop front door |
| `s3/s3.json` | The dev credentials the S3 store reads |
| `keycloak/realm-nebari.json` | The `nebari` realm: clients, mappers, users |
| `sql/bootstrap.sql` | Org + owner membership + operator grant, in one transaction |
| `proxy/Caddyfile` | Host-routing front door for the desktop client |
| `values/kind.yaml` | Helm values for the kind install |
| `op.py`, `ops/`, `cogs/` | `make op`: the script, the fake Ops, and the fake Cog packages they name (`cogs/fake_worker.py` serves the seam for each) |
| `.local/` | Frame bodies, the `make op` Track, the output of local workers (`runs/`), and scratch state (git-ignored, `make clean` removes it) |
