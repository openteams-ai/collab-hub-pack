# Deploying the Collab web client

The Collab web client is the Collab app served as a web page: users open a
URL, sign in through the hub's identity provider, and get chat (with active
Frames and connectors), Frames and Groups, the hub pages and usage, with
nothing installed. It is built from the same frontend as the desktop app, and
the desktop app is unchanged.

## What the chart deploys

`web.enabled: true` adds, beside the hub:

| Piece | What it is |
|---|---|
| NebariApp | the web client's own host, every path behind sign-in |
| `front` container | the app's files, and the chat service under the paths the app calls |
| `chat` container | Ravnar with the Collab agent plugins (`ravnar_apollo.web`) |
| PersistentVolumeClaim | uploaded files (ReadWriteOnce, so one replica) |
| ConfigMap | one chat agent per hub model |

Both images come from the Collab repository's `deploy/web/image`, published
as `ghcr.io/openteams-ai/apollo-desktop/web-front` and `.../web-chat` at the
same `sha-<short>` tag. The chat image is built for amd64 only, so the pod
runs on amd64 nodes.

```yaml
web:
  enabled: true
  image:
    tag: sha-1234567
  nebariapp:
    hostname: collab.example.com
  hubUrl: https://example.com            # the issuer's host without "keycloak."
  llmBaseUrl: https://llm-internal.example.com/v1
  models:
    - id: qwen
      displayName: Qwen
      supportsTools: true
  database:
    existingSecret: collab-web-chat-app  # e.g. a CloudNativePG cluster's app Secret
    databaseUrlKey: uri
```

The issuer, its keys and the hub's audience come from `frames.auth.bearer`
unless `web.oidc` and `web.hubAudience` say otherwise; the hub's audience must
be set (it is what the hub checks). `hubUrl` must be what the web client
derives from the issuer (`https://keycloak.example.com/realms/x` gives
`https://example.com`): it lists only agents whose hub matches it.

## How a request flows

| Path | Goes to |
|---|---|
| `/` | the app's files (unknown paths answer `index.html`) |
| `/api/*` | the chat service |
| `/hub-api/*`, `/frames-api/*`, `/connectors-api/*` | the hub, through the chat service |

The gateway signs the user in, keeps the tokens in its encrypted session, and
forwards the user's access token as `Authorization: Bearer` (the NebariApp's
`forwardAccessToken`). The browser never holds a token.

That token is issued to the web client's own Keycloak client, which the
operator names `<namespace>-<NebariApp name>` (the NebariApp is the chart's
full name plus `-web`, so release `nebari-nexus-pack` in namespace
`nebari-nexus` gives `nebari-nexus-nebari-nexus-pack-collab-hub-web`), and
the hub does not accept it: its
audience is the client's own, not the hub's. The chat service exchanges it at
Keycloak for a token issued for the hub, for the same user (OAuth 2.0 Token
Exchange, RFC 8693), and uses that for its agents' Frames, connector and model
requests and for the browser's hub calls, which it passes on without the
browser's cookies. Each user's hub token is reused until shortly before it or
the token it came from expires.

The front, in the same pod, does what the gateway does not:

- Refuses writes from other sites: anything but `GET`, `HEAD` and `OPTIONS`
  without `Sec-Fetch-Site: same-origin` gets `403`. `SameSite=Lax` does not
  cover sibling subdomains, which on Nebari include user-run JupyterLab and
  apps.
- Serves uploaded files (`/api/files/*/content`) as downloads that cannot run
  script: `Content-Disposition: attachment` and `Content-Security-Policy:
  sandbox; default-src 'none'`.
- Bounds request bodies at 40 MB (the largest attachment, base64-encoded).
- Drops the browser's cookies on the way to the chat service.
- Marks every answer it relays (`X-Collab-Served`). A `401` without the mark
  is the gateway's own (the session ended): the app reloads the page and the
  gateway starts sign-in. A `401` with it is a service's error, shown as
  such, so a misconfiguration cannot loop the browser through sign-in. The
  NebariApp's `denyRedirect` on `Sec-Fetch-Mode: cors` makes the gateway
  answer the app's requests with that `401` rather than a redirect it cannot
  follow.

Sign-out is the gateway's `/logout`.

## Before the first install

1. **Keycloak features.** The realm must allow token exchange:
   `KC_FEATURES=token-exchange,admin-fine-grained-authz:v1`.
2. **Allow the web client, and only it, to exchange for the hub's audience.**
   Once the NebariApp has provisioned its client, as a realm admin:

   ```sh
   kc=/opt/keycloak/bin/kcadm.sh; realm=nebari
   web=<namespace>-<NebariApp name>          # the web client's id (above)
   hub=apollo-desktop                         # frames.auth.bearer.audience's client
   uuid() { $kc get clients -r $realm -q "clientId=$1" --fields id --format csv --noquotes; }
   perm=$($kc update "clients/$(uuid $hub)/management/permissions" -r $realm -s enabled=true -o |
     sed -n 's/.*"token-exchange" *: *"\([^"]*\)".*/\1/p')
   rm=$(uuid realm-management)
   $kc create "clients/$rm/authz/resource-server/policy/client" -r $realm \
     -s name="$web-may-exchange" -s logic=POSITIVE -s "clients=[\"$(uuid $web)\"]"
   policy=$($kc get "clients/$rm/authz/resource-server/policy" -r $realm -q name="$web-may-exchange" \
     --fields id --format csv --noquotes)
   $kc get "clients/$rm/authz/resource-server/permission/scope/$perm" -r $realm > /tmp/perm.json
   $kc update "clients/$rm/authz/resource-server/permission/scope/$perm" -r $realm -f /tmp/perm.json \
     -s "policies=[\"$policy\"]" -s decisionStrategy=UNANIMOUS
   ```

   Until this is done every request is refused with `401`, and the chat
   service logs `web_auth_refused reason=exchange_refused_403` (or `_400`).
3. **A Postgres database** for the chat service, and a Secret with its URL
   (`postgresql://user:password@host/db`; the service opens it with psycopg).
4. **The images' tag**: the `sha-<short>` the Collab repository's "Web client
   images" workflow reports for the commit to deploy.

## The chat service

| Setting | Value |
|---|---|
| `APOLLO_WEB_OIDC_ISSUER` | the realm's issuer, exactly as it appears in tokens |
| `APOLLO_WEB_OIDC_AUDIENCE`, `APOLLO_WEB_OIDC_CLIENT_SECRET` | the web client's id and secret (the operator's `<NebariApp>-oidc-client` Secret) |
| `APOLLO_WEB_HUB_AUDIENCE` | the audience to exchange for: the hub's |
| `APOLLO_WEB_HUB_URL` | the hub API, in-cluster |
| `APOLLO_WEB_OIDC_JWKS_URL`, `APOLLO_WEB_OIDC_TOKEN_URL` | optional: the realm's keys and token endpoint, otherwise found by discovery |
| `RAVNAR_STORAGE__DATABASE__DSN`, `RAVNAR_STORAGE__FILES__PATH` | its database and file store |

It refuses to start without any of the required ones. It verifies the
forwarded token on every request (signature, issuer, audience, expiry) and
stores each account's threads and files under the same opaque account key the
desktop uses, so one user can never list or read another's. Users cannot
register agents. If the realm's keys or token endpoint cannot be reached it
answers `503`, not `401`, so an identity provider outage does not sign
everyone out.

**Logs.** Every refused request is logged as `web_auth_refused` with its
reason (`missing`, `expired`, `wrong_issuer`, `wrong_audience`, `invalid`,
`keys_unavailable`, `exchange_refused_<status>`, `exchange_unavailable`,
`exchange_unreadable`) and the token's unverified issuer and client, never
the token. Every run logs `web_run_start` and `web_run_end` with its account
and outcome (`finished`, `error`, `failed`, `abandoned`), and
`cleanup_timed_out=true` when a stopped run did not finish closing within 10
seconds (it may still be working as its user). Values that come from outside
are logged double-quoted, escaped and cut to 128 characters. Alert on bursts
of refusals and on `web_run_refused`.

**Limits to plan for.**

- One replica: the files are on a ReadWriteOnce volume, and Ravnar creates its
  tables at startup rather than through migrations.
- A run uses the hub token it started with. Make the realm's access token
  lifespan longer than the longest run. Each read from the model may wait up
  to 10 minutes, and a run with tool calls makes several requests.
- Ravnar does not bound thread names, page sizes or concurrent runs per user;
  the front's body limit is the only bound. Rate-limit per user at the
  gateway where it can.
- The gateway's `/logout` does not check where the navigation came from, so
  another site can sign a user out (not in).

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
machine against this pack's API and dev realm, with oauth2-proxy standing in
for the gateway and the same front configuration the image ships: `make up`,
`make run`, then open `http://collab.localhost:9490`; `make smoke` checks
sign-in, the front's rules above and the token exchange. Its README has the
details.
