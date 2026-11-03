# Deploying the Collab web client

The Collab web client is the Collab app served as a web page: users open a
URL, sign in through the hub's identity provider, and get chat (with active
Frames and connectors), Frames and Groups, the hub pages and usage, with
nothing installed. It is built from the same frontend as the desktop app, and
the desktop app is unchanged.

This page covers what a deployment needs. The Helm chart does not template
the web client yet: the pieces below are deployed alongside it.

## The pieces

| Piece | What it is | Talks to |
|---|---|---|
| Web app | Static files: the frontend built with `npm run build:web` | nothing; served by the gateway |
| Chat service | Ravnar with the Collab agent plugins (`ravnar_apollo`), one replica (below) | Postgres, the hub API, the AI gateway |
| Postgres | The chat service's threads, runs, messages and file records | |
| File store | The chat service's uploaded file contents (`storage.files.path`, any fsspec URL) | |
| Gateway | Signs users in, keeps their session, routes by path | Keycloak, the three upstreams |

All of it sits on **one host**, in the same Keycloak realm as the hub:

| Path | Upstream |
|---|---|
| `/` | the web app's files (unknown paths answer `index.html`) |
| `/api/*` | the chat service, prefix kept |
| `/hub-api/*`, `/frames-api/*`, `/connectors-api/*` | the hub API, prefix stripped |

## What the gateway must do

The browser never holds a bearer token, and a session cookie must not be
usable from another site. The gateway is what makes that true, so these are
requirements, not suggestions:

1. **Sign the user in and keep the session server-side.** The browser gets a
   session cookie (`HttpOnly`, `Secure`, `SameSite=Lax`, ideally with the
   `__Host-` prefix); the tokens stay in the gateway's session store. End the
   identity provider's session on sign-out, and keep sessions to a working
   day.
2. **Drop everything the browser sends that carries or claims an identity**,
   on every route: `Authorization`, `X-Forwarded-Access-Token`,
   `X-Forwarded-User`, `X-Forwarded-Email` and all `X-Auth-Request-*`
   headers.
3. **Set `Authorization: Bearer <the user's access token>`** on requests to
   the hub API and the chat service, refreshing the token before it expires.
4. **Do not forward the browser's cookies** to the hub API or the chat
   service. The hub reads an `IdToken-*` cookie before the bearer, so a
   cookie a sibling site planted would decide which account a request acts
   as.
5. **Refuse writes from other sites.** Answer any request that is not `GET`,
   `HEAD` or `OPTIONS` and does not carry `Sec-Fetch-Site: same-origin` with
   `403`. `SameSite=Lax` does not cover sibling subdomains, which on Nebari
   include user-run JupyterLab and apps.
6. **Serve uploaded files as downloads that cannot run script.** On
   `/api/files/*/content` set `Content-Disposition: attachment` and
   `Content-Security-Policy: sandbox; default-src 'none'`, after the chat
   service's own headers. Otherwise a user's HTML or SVG file runs in the
   app's origin with their session.
7. **Bound request bodies** (40 MB fits the largest attachment) and rate-limit
   per user.
8. Answer a page load without a session with a redirect to sign in, and an
   API call without one with `401` carrying `X-Collab-Session: none`. The web
   app sends the browser to sign in on that `401` only; a `401` a service
   sends itself is shown as its error, so a misconfiguration cannot loop the
   browser through sign-in.
9. **Keep the session's details from the page.** If the sign-in component
   answers with the user's token in response headers (oauth2-proxy does on
   `/oauth2/auth`, `/oauth2/userinfo` and every path it does not know), let
   the browser reach only signing in, the callback and signing out (`404`
   for anything else), strip those headers from its responses, and have the
   gateway call the session check directly.
10. **Sign out only from this site**: refuse `/oauth2/sign_out` (or its
    equivalent) unless `Sec-Fetch-Site` is `same-origin` or `none`.
11. **Keep Keycloak's admin console off the users' route.**

Pick one audience for the hub's tokens and set it everywhere: on the hub
(`FRAMES_BEARER_AUDIENCE`; the chart's default is empty, which turns the hub's
audience check off), on the chat service (`APOLLO_WEB_OIDC_AUDIENCE`), and as
an audience mapper on the gateway's Keycloak client so its access tokens
carry it. The hub then accepts the token exactly as it accepts the
desktop's.

On Nebari, check the gateway's OIDC support does all of the above. Where it
cannot, put [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/)
behind a reverse proxy that does, with a Redis session store. The reference
setup below does all of it except the per-user rate limit, the
`Secure`/`__Host-` cookie (it runs on plain http) and point 11 (it is the
developer's own Keycloak). `make smoke` there checks points 2, 4, 5, 6, 8, 9
and 10, and the body limit, against it.

## The chat service

It runs from the `web` environment of the agent runtime's Pixi manifest
(`internal/intelligencehub/pixi.toml` in the Collab repository), which is the
desktop's locked environment plus a Postgres driver:

```sh
RAVNARPATH=<checkout>/internal/intelligencehub \
RAVNAR_CONFIG=chat-service.yaml \
pixi run --manifest-path <checkout>/internal/intelligencehub/pixi.toml -e web \
  python -m ravnar serve
```

| Setting | Value |
|---|---|
| `RAVNAR_SERVER__HOSTNAME`, `RAVNAR_SERVER__PORT` | where it listens; only the gateway may reach it (a NetworkPolicy) |
| `RAVNAR_STORAGE__DATABASE__DSN` | `postgresql+psycopg://USER:PASSWORD@HOST/DB` |
| `RAVNAR_STORAGE__FILES__PATH` | where file contents live |
| `APOLLO_WEB_OIDC_ISSUER` | the realm's issuer, exactly as it appears in tokens |
| `APOLLO_WEB_OIDC_AUDIENCE` | required: the audience tokens must carry (the hub's) |
| `APOLLO_WEB_OIDC_JWKS_URL` | optional: the realm's keys at an in-cluster address |

It refuses to start without the issuer and audience. It verifies the
forwarded token on every request (signature, issuer, audience, expiry) and
stores each account's threads and files under the same opaque account key the
desktop uses, so one user can never list or read another's. Users cannot
register agents. If the realm's keys cannot be fetched it answers `503`, not
`401`, so an identity provider outage does not sign everyone out.

**Logs.** Every refused request is logged as `web_auth_refused` with its
reason (`missing`, `expired`, `wrong_issuer`, `wrong_audience`, `invalid`,
`keys_unavailable`) and the token's unverified issuer and client, never the
token. Accepted requests log `web_auth_ok` with the account key and subject at
debug level, and every run logs `web_run_start` and `web_run_end` with its
account and outcome (`finished`, `error`, `failed`, `abandoned`), and
`cleanup_timed_out=true` when a stopped run did not finish closing within 10
seconds (it may still be working as its user). Values that come from outside
(a refused token's claims, thread and run ids) are logged double-quoted,
escaped and cut to 128 characters. Alert on bursts of refusals and on
`web_run_refused`.

**Limits to plan for.**

- Run one replica. Ravnar creates its tables at startup rather than through
  migrations, so replicas starting together can race.
- A run uses the access token it started with. Make the realm's access token
  lifespan longer than the longest run. Each read from the model may wait up
  to 10 minutes, and a run with tool calls makes several requests.
- Ravnar does not bound thread names, page sizes or concurrent runs per user;
  the gateway's body limit and rate limit are the bound.

Each hub model is a static agent in `chat-service.yaml`:

```yaml
security:
  authenticator: ravnar_apollo.web:GatewayIdentityAuthenticator
agents:
  dynamic:
    enabled: false
  static:
    "hub:<hub host>:<model id>":
      cls_or_fn: ravnar_apollo.web.WebChatAgent
      params:
        model_id: <model id>
        model_base_url: <the AI gateway's OpenAI-compatible URL, in-cluster>
        display_name: <name shown to users>
        provider: hub
        hub_url: https://<hub host>   # the issuer's host without "keycloak.":
                                      # the web client lists only agents
                                      # whose hub_url matches it
        frames_api_url: <the hub API, in-cluster>
        connectors_api_url: <the hub API, in-cluster>
        owned_by: self-hosted         # or the provider the gateway proxies to
        supports_tools: true          # false for a model without tool calling
```

There are no tokens in this file. Every request a chat run sends (Frames,
connectors and inference) carries the access token of the user whose run it
is, so the AI gateway attributes each completion to that user, as it does for
the desktop.

## Not in the web client

Local capabilities and the launcher, desktop agents (Hermes, Docker
sandboxes), voice input, self-update, local models, OpenRouter, chat history
backups, hubspaces and the QA API page are desktop features; the web client
leaves them out.

Not built yet, and left out rather than shown broken:

- Scheduled tasks: they run on the user's computer, and the chat service
  does not run them yet.
- Connecting a connector account from the browser: link accounts from the
  desktop app for now.
- PDF attachments: the desktop reads them with a local OCR sidecar.
- Charts in chat: the chart tool stores its picture through the desktop's
  private file lane.
- Opening or revealing a chat file with the computer's apps: files download
  instead.

## Running it locally

`deploy/web/dev/` in the Collab repository runs the whole thing on one
machine against this pack's API and dev realm: `make up`, `make run`, then
open `http://collab.localhost:9490`; `make smoke` checks the gateway
requirements above. Its README has the details.
