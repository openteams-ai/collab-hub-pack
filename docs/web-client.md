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
| Web app | Static files: the frontend built with `vite build --mode web` | nothing; served by the gateway |
| Chat service | Ravnar with the Collab agent plugins (`ravnar_apollo`), stateless | Postgres, the hub API, the AI gateway |
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

The browser never holds a bearer token. The gateway is what makes that true,
so these are requirements, not suggestions:

1. **Sign the user in and keep the session server-side.** The browser gets a
   session cookie (`HttpOnly`, `Secure`, `SameSite=Lax`); the tokens stay in
   the gateway's session store.
2. **Drop any `Authorization` header the browser sends**, on every route.
3. **Set `Authorization: Bearer <the user's access token>`** on requests to
   the hub API and the chat service, refreshing the token before it expires.
4. Answer a page load without a session with a redirect to sign in, and an
   API call without one with `401`. The web app sends the browser to sign in
   on a `401`.

The access token must carry the hub's audience (`FRAMES_BEARER_AUDIENCE`,
`apollo-desktop` by default): give the gateway's Keycloak client an audience
mapper for it. The hub then accepts the token exactly as it accepts the
desktop's.

On Nebari, the gateway's OIDC support provides this; check that it forwards
the access token to the upstream and strips a client-supplied one. Where it
cannot, put [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) in
front with a Redis session store; the reference setup below does exactly
that.

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
| `RAVNAR_SERVER__HOSTNAME`, `RAVNAR_SERVER__PORT` | where it listens; only the gateway should reach it |
| `RAVNAR_STORAGE__DATABASE__DSN` | `postgresql+psycopg://USER:PASSWORD@HOST/DB` |
| `RAVNAR_STORAGE__FILES__PATH` | where file contents live |
| `APOLLO_WEB_OIDC_ISSUER` | the realm's issuer, exactly as it appears in tokens |
| `APOLLO_WEB_OIDC_JWKS_URL` | optional: the realm's keys at an in-cluster address |
| `APOLLO_WEB_OIDC_AUDIENCE` | optional, recommended: the audience tokens must carry |

It verifies the forwarded token on every request (signature, issuer, expiry,
and audience when set) and stores each account's threads and files under the
same opaque account key the desktop uses, so one user can never list or read
another's. Users cannot register agents.

Each hub model is a static agent in `chat-service.yaml`:

```yaml
security:
  authenticator: ravnar_apollo.web:GatewayIdentityAuthenticator
agents:
  dynamic:
    enabled: false
  static:
    "hub:<hub host>:<model id>":
      cls_or_fn: ravnar_apollo.web.web_chat_agent
      params:
        model_id: <model id>
        model_base_url: <the AI gateway's OpenAI-compatible URL, in-cluster>
        display_name: <name shown to users>
        provider: hub
        hub_url: https://<hub host>   # the issuer's host without "keycloak."
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
leaves them out. Scheduled tasks are left out too for now: they run on the
user's computer, and the chat service does not run them yet.

## Running it locally

`deploy/web/dev/` in the Collab repository runs the whole thing on one
machine against this pack's API and dev realm: `make up`, `make run`, then
open `http://collab.localhost:9490`. Its README has the details.
